#include "q4t/server/responses_execution.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  ObjectStoreOptions objects;
  ChatPlanConfig config;
  Fixture() {
    const char* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto directory = fs::absolute(root ? root : ".q4t-work/responses-files");
    fs::create_directories(directory);
    auto path = (directory / "responses-files-XXXXXX").string();
    if (!mkdtemp(path.data()))
      throw std::runtime_error("file execution fixture");
    objects.directory = path + "/objects";
    objects.restart_backoff_ms = 0;
    config.model = "cpu-file-lifecycle";
    config.structured = true;
    config.allow_media = true;
    auto files = std::make_shared<FileExpansionOptions>();
    files->extraction.legacy.work_directory = path + "/codec";
    config.file_inputs = files;
  }
};
StateAccess Access() { return {"client:files", Clock::now() + 30s, {}}; }
BinaryAccess Binary() {
  auto access = Access();
  return {access.scope, access.deadline, access.cancelled};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s %s: %s\n", reply.code.c_str(),
                 reply.value.GetString("param").c_str(), reply.message.c_str());
  return reply.ok();
}
Json Inline(bool csv = false) {
  return Object(
      {{"type", String("input_file")},
       {"filename", String(csv ? "facts.csv" : "facts.txt")},
       {"file_data", String(csv ? "bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK"
                                : "b3JpZ2luYWwgQWxwaGEgNDIK")}});
}
Json Request(Json part) {
  return Object(
      {{"input", Array({Object({{"role", String("user")},
                                {"content", Array({std::move(part)})}})})},
       {"reasoning", Object({{"effort", String("none")}})}});
}
Json Stored(const std::string& id) {
  return Object({{"type", String("input_file")}, {"file_id", String(id)}});
}
std::string Upload(ObjectStore& objects, std::string_view bytes,
                   std::string_view name) {
  FileUpload upload(objects, Binary(), "multipart/form-data; boundary=file");
  const std::string prefix =
      "--file\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nuser_data\r\n"
      "--file\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"" +
      std::string(name) + "\"\r\n\r\n";
  if (!upload.Feed(prefix, Binary().deadline).ok() ||
      !upload.Feed(bytes, Binary().deadline).ok() ||
      !upload.Feed("\r\n--file--\r\n", Binary().deadline).ok())
    throw std::runtime_error("file upload fixture");
  auto reply = upload.Finish();
  if (!reply.ok()) throw std::runtime_error(reply.message);
  return reply.value.GetString("id");
}
std::string Bytes(const char* path) {
  std::ifstream in(fs::path(__FILE__).parent_path() / "fixtures" / path,
                   std::ios::binary);
  if (!in) throw std::runtime_error("original fixture");
  return std::string(std::istreambuf_iterator<char>(in), {});
}
std::string Prompt(const ResponsesExecution& run) {
  std::string prompt, param;
  std::vector<VisionItem> images;
  const auto& plan = run.Generation();
  auto status =
      PrepareChatPrompt(plan.chat.Request(plan.generation_request),
                        plan.chat.constraints, &prompt, &images, &param);
  if (!status) throw std::runtime_error(status.message());
  return prompt;
}
bool FinishFixture(ResponsesExecution& run) {
  std::string wire;
  if (!run.Begun() && !Ok(run.Begin(&wire))) return false;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "CPU lifecycle output fixture";
  return run.Append(delta, "null", &wire).ok() &&
         Ok(run.Finish(false, 0, {100, 0, 6, 0}, 43, &wire));
}
constexpr auto kSheetFixture =
    R"({"summary":"CPU step receipt fixture: alpha 42 and beta 7",)"
    R"("headers":["name: row label","value: quantity"]})";
}  // namespace

Q4T_TEST(responses_files_inline_reaches_actual_template_without_state_io) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  auto request = Request(Inline());
  Set(&request, "store", Boolean(false));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, fixture.config, Access(), 42)));
  Q4T_CHECK(run.Prepared() && run.GenerationReady() &&
            !run.PendingFileSheets());
  const auto prompt = Prompt(run);
  Q4T_CHECK(prompt.find("original Alpha 42") != prompt.npos &&
            prompt.find("b3JpZ2luYWwgQWxwaGEgNDIK") == prompt.npos);
  Q4T_CHECK(OutputJson(run.Envelope().request) == OutputJson(request));
  Q4T_CHECK(run.FileUsage()->files == 1 && FinishFixture(run));
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(responses_files_previous_restores_deleted_source_and_original_items) {
  Fixture fixture;
  std::string id, source;
  {
    ObjectStore objects(fixture.objects);
    source = Upload(objects, "retained fact: station violet", "facts.txt");
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(Request(Stored(source)), fixture.config, Access(),
                             42)) &&
              FinishFixture(run));
    id = run.Id();
    const auto record = ResponsesState(objects).ReadRecord(id, Access());
    Q4T_CHECK(Ok(record) &&
              record.value.GetArray("input_snapshots")->array.size() == 1);
    Q4T_CHECK(OutputJson(*record.value.Find("history")).find(source) !=
              std::string::npos);
    Q4T_CHECK(OutputJson(record.value).find("retained fact: station violet") ==
              std::string::npos);
    Q4T_CHECK(Ok(Files(objects).Erase(Binary(), source)));
  }
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  ResponsesExecution next(objects);
  auto request = Object({{"input", String("repeat the fact")},
                         {"previous_response_id", String(id)},
                         {"store", Boolean(false)}});
  Q4T_CHECK(Ok(next.Prepare(request, fixture.config, Access(), 44)));
  const auto prompt = Prompt(next);
  Q4T_CHECK(prompt.find("station violet") != prompt.npos &&
            prompt.find("repeat the fact") != prompt.npos);
  auto wrong = Access();
  wrong.scope = "client:other";
  ResponsesExecution denied(objects);
  Q4T_CHECK(denied.Prepare(request, fixture.config, wrong, 44).http_status ==
            404);
  return true;
}

Q4T_TEST(responses_files_pdf_and_webp_use_actual_media_planner) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto pdf = Upload(objects, Bytes("file_nested/page.pdf"), "page.pdf");
  const auto image =
      Upload(objects, Bytes("file_images/webp/alpha.webp"), "image.webp");
  auto request = Request(Stored(pdf));
  auto& parts = Field(Field(request, "input").array[0], "content");
  parts.array.push_back(Object({{"type", String("input_image")},
                                {"file_id", String(image)},
                                {"detail", String("original")}}));
  Set(&request, "store", Boolean(false));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, fixture.config, Access(), 42)));
  std::string prompt, param;
  std::vector<VisionItem> images;
  const auto& generation = run.Generation();
  Q4T_CHECK(PrepareChatPrompt(generation.generation_request,
                              generation.chat.constraints, &prompt, &images,
                              &param));
  Q4T_CHECK(images.size() == 2 && run.FileUsage()->pages == 1 &&
            images.back().detail == q4t::vision::ImageDetail::kOriginal);
  Q4T_CHECK(run.FileUsage()->files == 2 &&
            prompt.find("<|image_pad|>") != prompt.npos);
  return true;
}

Q4T_TEST(
    responses_files_queue_freezes_raw_sheet_and_checkpoints_completed_step) {
  Fixture fixture;
  std::string id, raw_reference;
  {
    ObjectStore objects(fixture.objects);
    auto request = Request(Inline(true));
    Set(&request, "background", Boolean(true));
    ResponsesExecution queued(objects);
    Q4T_CHECK(Ok(queued.Prepare(request, fixture.config, Access(), 42, true)));
    Q4T_CHECK(queued.Prepared() && queued.PendingFileSheets() &&
              !queued.GenerationReady());
    bool rejected = false;
    try {
      (void)queued.Generation();
    } catch (const std::logic_error&) {
      rejected = true;
    }
    Q4T_CHECK(rejected);
    std::string wire;
    Q4T_CHECK(Ok(queued.Begin(&wire)));
    id = queued.Id();
    const auto record = ResponsesState(objects).ReadRecord(id, Access());
    Q4T_CHECK(Ok(record));
    raw_reference = OutputJson(*record.value.Find("input_snapshots"));
    Q4T_CHECK(record.value.Find("response")->GetString("status") == "queued");
    Q4T_CHECK(!queued.Finish(false, 0, {}, 43, &wire).ok());
  }
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  ResponsesExecution restored(objects);
  Q4T_CHECK(Ok(restored.RestoreQueued(id, fixture.config, Access())) &&
            restored.PendingFileSheets() && !restored.GenerationReady());
  std::string wire;
  Q4T_CHECK(Ok(restored.StartRunning(&wire)));
  FileSheetStep step;
  Q4T_CHECK(Ok(restored.NextFileSheet(fixture.config, &step)));
  // Explicit protocol callback fixture. Real model execution is a separate
  // required business check, not established by these synthetic token counts.
  Q4T_CHECK(Ok(restored.CompleteFileSheet(step, kSheetFixture, "stop", 321, 24,
                                          fixture.config)));
  Q4T_CHECK(restored.GenerationReady() && !restored.PendingFileSheets());
  Q4T_CHECK(Prompt(restored).find("CPU step receipt fixture") !=
            std::string::npos);
  const auto record = ResponsesState(objects).ReadRecord(id, Access());
  Q4T_CHECK(Ok(record) &&
            OutputJson(*record.value.Find("input_snapshots")) != raw_reference);
  ResponsesExecution replay(objects);
  Q4T_CHECK(replay.RestoreQueued(id, fixture.config, Access()).code ==
            "response_not_queued");
  Q4T_CHECK(FinishFixture(restored));
  ResponsesExecution next(objects);
  Q4T_CHECK(Ok(next.Prepare(Object({{"input", String("use summary")},
                                    {"previous_response_id", String(id)},
                                    {"store", Boolean(false)}}),
                            fixture.config, Access(), 45)));
  Q4T_CHECK(!next.PendingFileSheets() &&
            Prompt(next).find("CPU step receipt fixture") != std::string::npos);
  return true;
}

Q4T_TEST(
    responses_files_invalid_sources_tools_and_output_fail_before_acquisition) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  for (int kind = 0; kind < 3; ++kind) {
    auto request = Request(Stored("file_missing"));
    if (kind == 0) {
      auto bad = Inline();
      Set(&bad, "file_id", String("file_other"));
      Field(Field(request, "input").array[0], "content").array.push_back(bad);
    } else if (kind == 1) {
      Set(&request, "tool_choice",
          Object({{"type", String("function")}, {"name", String("absent")}}));
    } else {
      Set(&request, "text",
          Object({{"format",
                   Object({{"type", String("json_schema")},
                           {"name", String("bad")},
                           {"strict", Boolean(true)},
                           {"schema",
                            Object({{"type", String("nonsense")}})}})}}));
    }
    ResponsesExecution run(objects);
    Q4T_CHECK(run.Prepare(request, fixture.config, Access(), 42).http_status ==
              400);
    Q4T_CHECK(objects.Health().workers_started == 0 && !run.Prepared());
  }
  return true;
}

Q4T_TEST(responses_files_conversation_retains_snapshots_with_store_false) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto conversation =
      ConversationState(objects).Create(Object(), Array(), Access());
  Q4T_CHECK(Ok(conversation));
  const auto id = conversation.value.GetString("id");
  auto request = Request(Inline());
  Set(&request, "conversation", String(id));
  Set(&request, "store", Boolean(false));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, fixture.config, Access(), 42)) &&
            FinishFixture(run));
  Q4T_CHECK(ResponsesState(objects).Retrieve(run.Id(), Access()).http_status ==
            404);
  ResponsesExecution next(objects);
  Q4T_CHECK(Ok(next.Prepare(Object({{"input", String("remember")},
                                    {"conversation", String(id)},
                                    {"store", Boolean(false)}}),
                            fixture.config, Access(), 44)));
  Q4T_CHECK(Prompt(next).find("original Alpha 42") != std::string::npos);
  return true;
}

Q4T_TEST(responses_files_unknown_snapshot_commit_is_not_retried_or_released) {
  Fixture fixture;
  fixture.objects.io_timeout_ms = 250;
  ObjectWorkerEntryForTesting("--q4t-object-worker-snapshot-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  {
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(Request(Inline()), fixture.config, Access(), 42)));
    std::string wire;
    const auto failed = run.Begin(&wire);
    Q4T_CHECK(failed.commit_unknown && run.CommitUnknown() && !run.Begun());
    Q4T_CHECK(!run.Begin(&wire).ok() && wire.empty());
  }
  size_t records = 0;
  for (const auto& entry : fs::directory_iterator(fixture.objects.directory))
    records += entry.path().extension() == ".json";
  Q4T_CHECK(records == 4 && objects.Health().workers_started == 1);
  return true;
}
