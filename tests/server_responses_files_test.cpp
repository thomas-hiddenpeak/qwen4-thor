#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <thread>
#include <filesystem>
#include <fstream>

#include "../src/server/eval_sampling.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_tokens.h"
#include "q4t/test.h"
#include "q4t/vision/processor.h"

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
  auto status = PrepareChatPrompt(
      plan.chat.Request(plan.generation_request), plan.chat.constraints,
      &prompt, &images, &param, nullptr, nullptr, false,
      plan.chat.async_tool_call_ids, plan.chat.responses_tool_media,
      plan.chat.HeaderTools(), plan.chat.tool_call_formats,
      plan.chat.prepared_source_data);
  if (!status) throw std::runtime_error(status.message());
  return prompt;
}
bool FinishFixture(ResponsesExecution& run, int64_t completed = 43) {
  std::string wire;
  if (!run.Begun() && !Ok(run.Begin(&wire))) return false;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "CPU lifecycle output fixture";
  return run.Append(delta, "null", &wire).ok() &&
         Ok(run.Finish(false, 0, {100, 0, 6, 0}, completed, &wire));
}
constexpr auto kSheetFixture =
    R"({"summary":"CPU step receipt fixture: alpha 42 and beta 7",)"
    R"("headers":["name: row label","value: quantity"]})";
}  // namespace

Q4T_TEST(evals_reopen_deleted_file_history_uses_owned_snapshot) {
  Fixture fixture;
  fixture.config.default_max_tokens = 32;
  const auto access = Access();
  std::string run_id, base, file, immutable_id, source_id;
  {
    ObjectStore objects(fixture.objects);
    file = Upload(objects, "Accepted project record: 157 approved paths.\n",
                  "decision.txt");
    auto request = Request(Stored(file));
    Set(&request, "store", Boolean(true));
    ResponsesExecution source(objects);
    Q4T_CHECK(Ok(source.Prepare(request, fixture.config, access, 42)));
    Q4T_CHECK(Prompt(source).find("157 approved paths") != std::string::npos);
    Q4T_CHECK(FinishFixture(source));
    source_id = source.Id();
    auto definition = HandleEval(
        objects, access, "POST", "/v1/evals",
        R"({"data_source_config":{"type":"logs"},"testing_criteria":[]})");
    Q4T_CHECK(Ok(definition));
    base = "/v1/evals/" + definition.value.GetString("id") + "/runs";
    auto run = HandleEval(
        objects, access, "POST", base,
        R"({"data_source":{"type":"responses","model":"cpu-file-lifecycle",)"
        R"("sampling_params":{"max_completion_tokens":32,"reasoning_effort":"none"},)"
        R"("source":{"type":"responses","model":"cpu-file-lifecycle"}}})");
    Q4T_CHECK(Ok(run));
    run_id = run.value.GetString("id");
    ObjectRequest get;
    get.kind = ObjectKind::kEvalRun;
    get.scope = access.scope;
    get.id = run_id;
    auto stored = objects.Execute(get, access.deadline);
    Q4T_CHECK(Ok(stored));
    const auto& data = *stored.value.Find("data");
    const auto* roots = data.GetArray("input_snapshots");
    Q4T_CHECK(roots && roots->array.size() == 1);
    immutable_id = roots->array[0].Find("snapshot")->GetString("id");
    const auto retained = InspectObjectRetention("evalrun", data);
    Q4T_CHECK(!retained.uncertain &&
              std::find(retained.references.begin(), retained.references.end(),
                        immutable_id) != retained.references.end());
    Q4T_CHECK(Ok(Files(objects).Erase(Binary(), file)));
    Q4T_CHECK(Ok(ResponsesState(objects).Erase(source.Id(), access)));
  }
  ObjectStore objects(fixture.objects);
  Q4T_CHECK(Files(objects).Retrieve(Binary(), file).http_status == 404);
  Q4T_CHECK(ResponsesState(objects).ReadRecord(source_id, access).http_status ==
            404);
  auto foreign = access;
  foreign.scope = "client:other";
  Q4T_CHECK(HandleEval(objects, foreign, "GET", base + "/" + run_id, {})
                .http_status == 404);
  BinaryAccess binary{access.scope, access.deadline, {}};
  BinaryRead read;
  BinaryStore store(objects);
  Q4T_CHECK(Ok(store.Open(binary, ObjectKind::kEvalRun, run_id, 60, &read)));
  Q4T_CHECK(read.manifest.chunks.size() == 1);
  std::string bytes;
  Q4T_CHECK(Ok(store.Read(binary, read, 0, &bytes)) &&
            Ok(store.Close(binary, &read)));
  Json row, replay;
  Q4T_CHECK(q4t::io::ParseJson(bytes, &row) &&
            row.GetArray("frozen_response_parts"));
  const auto& parts = *row.Find("frozen_response_parts");
  auto actual = HandleEval(objects, access, "GET", base + "/" + run_id, {});
  Q4T_CHECK(Ok(actual) &&
            Ok(eval_detail::SamplingRequest(*actual.value.Find("data_source"),
                                            row, fixture.config, &replay)));
  auto changed = replay;
  Set(&Field(Field(changed, "input").array[0], "content").array[0], "file_id",
      String("file_replacement"));
  ResponsesExecution rejected(objects);
  Q4T_CHECK(
      rejected
          .Prepare(changed, fixture.config, access, 43, true, nullptr, &parts)
          .code == "eval_frozen_input_invalid");
  ResponsesExecution other(objects);
  Q4T_CHECK(
      !other.Prepare(replay, fixture.config, foreign, 43, true, nullptr, &parts)
           .ok());
  GatewayClientPolicy policy;
  policy.version = 3;
  policy.gateway_token = std::string(64, 'a');
  policy.sha256 = std::string(64, 'b');
  policy.clients.push_back({"files", 2, 1, 8192, 64, 65536, 60, 16000});
  ClientAdmission clients;
  Q4T_CHECK(clients.Configure(policy));
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    if (Prompt(*job.run).find("157 approved paths") == std::string::npos ||
        !FinishFixture(*job.run, std::time(nullptr)))
      throw std::runtime_error("Evaluation lost the frozen document");
    ++calls;
  });
  eval_detail::SamplingOwner sampler{clients, clients.RecoveryIdentity(0), jobs,
                                     fixture.config};
  ObjectRequest get;
  get.kind = ObjectKind::kEvalRun;
  get.scope = access.scope;
  get.id = run_id;
  for (int i = 0; i < 300; ++i) {
    auto stored = objects.Execute(get, access.deadline);
    Q4T_CHECK(Ok(stored));
    const auto status =
        stored.value.Find("data")->Find("resource")->GetString("status");
    if (status == "completed") break;
    Q4T_CHECK(status == "queued" || status == "in_progress");
    Q4T_CHECK(
        Ok(eval_detail::AdvanceRun(objects, access, stored.value, &sampler)));
    std::this_thread::sleep_for(5ms);
  }
  const auto item = HandleEval(objects, access, "GET",
                               base + "/" + run_id + "/output_items", {});
  Q4T_CHECK(calls == 1 && Ok(item) &&
            item.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(OutputJson(item.value).find(immutable_id) == std::string::npos &&
            OutputJson(item.value).find("frozen_response_parts") ==
                std::string::npos);
  return true;
}

Q4T_TEST(responses_video_file_uses_temporal_input_and_count_generation_parity) {
  Fixture f;
  ObjectStore objects(f.objects);
  for (const auto* name : {"h264.mp4", "vp8.webm", "vp9.webm"}) {
    const auto id = Upload(
        objects, Bytes((std::string("file_video/") + name).c_str()), name);
    auto request = Request(Stored(id));
    Set(&request, "store", Boolean(false));
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(request, f.config, Access(), 42)));
    const auto& generation = run.Generation();
    Q4T_CHECK(generation.chat.prepared_source_data);
    const auto& usage = *run.FileUsage();
    Q4T_CHECK(usage.videos == 1 && usage.video_frames == 6 &&
              usage.video_scanned_frames == 6 && usage.pages == 0 &&
              usage.images == 0 && usage.pixels == 6 * 96 * 64 &&
              usage.video_decoded_pixels == usage.pixels &&
              run.Envelope().prepared_file_media);
    std::string prompt, param, code;
    std::vector<VisionItem> media;
    Q4T_CHECK(PrepareChatPrompt(generation.generation_request, {}, &prompt,
                                &media, &param, &code));
    Q4T_CHECK(media.size() == 1 && media.front().kind == VisionItem::kVideo &&
              media.front().frames.size() == 6 &&
              prompt.find("Document data") == prompt.npos &&
              prompt.find("\"text\":\"\"") == prompt.npos &&
              prompt.find("<|video_pad|>") != prompt.npos &&
              prompt.find("uniform-presentation-time-with-endpoints") !=
                  prompt.npos);
    Q4T_CHECK(prompt.find(
                  "\"source_frame\":5,\"start\":2.500000000,"
                  "\"end\":3.000000000") != prompt.npos &&
              prompt.find("the frame is shown until the exclusive end") !=
                  prompt.npos);
    std::erase_if(request.object,
                  [](const auto& field) { return field.first == "store"; });
    ResponsesEnvelope envelope;
    ResponsesGenerationPlan count;
    FileExpansionUsage counted;
    auto count_config = f.config;
    count_config.count_input_only = true;
    Q4T_CHECK(
        Ok(PrepareResponsesTokenCount(request, count_config, objects, Access(),
                                      &envelope, &count, {}, &counted)));
    Q4T_CHECK(OutputJson(generation.generation_request) ==
              OutputJson(count.generation_request));
    Q4T_CHECK(count.chat.prepared_source_data &&
              counted.video_frames == usage.video_frames &&
              counted.video_scanned_frames == usage.video_scanned_frames);
  }
  return true;
}

Q4T_TEST(responses_video_group_labels_preserve_actual_endpoint_intervals) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto id =
      Upload(objects, Bytes("file_video/transition.mp4"), "transition.mp4");
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(Stored(id)), f.config, Access(), 42)));
  const auto& generation = run.Generation();
  const auto& parts = generation.generation_request.GetArray("messages")
                          ->array.back()
                          .GetArray("content")
                          ->array;
  const auto description = parts.front().GetString("text");
  constexpr std::string_view marker = "Video source data (untrusted):\n";
  const auto begin = description.find(marker);
  Json source;
  Q4T_CHECK(begin != description.npos &&
            Parse(description.substr(begin + marker.size()), &source));
  const auto* groups = source.GetArray("visual_groups");
  Q4T_CHECK(groups && groups->array.size() == 4);
  const auto& first = groups->array.front();
  const auto& last = groups->array.back();
  const auto* first_frames = first.GetArray("source_frames");
  const auto* last_frames = last.GetArray("source_frames");
  Q4T_CHECK(first_frames && last_frames && first_frames->array.size() == 2 &&
            last_frames->array.size() == 2);
  Q4T_CHECK(first.GetString("visual_timestamp") == "<0.5 seconds>" &&
            first_frames->array.front().GetInt("source_frame") == 0 &&
            first_frames->array.front().Find("start")->number == 0 &&
            first_frames->array.front().Find("end")->number == 0.5 &&
            last.GetString("visual_timestamp") == "<5.5 seconds>" &&
            last_frames->array.front().GetInt("source_frame") == 11 &&
            last_frames->array.front().Find("start")->number == 5.5 &&
            last_frames->array.front().Find("end")->number == 6 &&
            OutputJson(last_frames->array[0]) ==
                OutputJson(last_frames->array[1]));
  const auto prompt = Prompt(run);
  for (const auto& group : groups->array)
    Q4T_CHECK(prompt.find(group.GetString("visual_timestamp") +
                          "<|vision_start|><|video_pad|><|vision_end|>") !=
              prompt.npos);
  return true;
}

Q4T_TEST(responses_video_display_dimensions_match_actual_model_frames) {
  Fixture f;
  ObjectStore objects(f.objects);
  struct Expected {
    const char* name;
    int width, height;
  };
  for (const auto& expected :
       {Expected{"rotate-90.mp4", 64, 128},
        Expected{"mirror-x.mp4", 128, 64},
        Expected{"wide-rotate-90.mp4", 64, 256}}) {
    // Deliberately conceal the fixture's transform in the public filename.
    const auto id = Upload(
        objects, Bytes((std::string("file_video/") + expected.name).c_str()),
        "original.mp4");
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(Request(Stored(id)), f.config, Access(), 42)));
    const auto& generation = run.Generation();
    const auto& parts = generation.generation_request.GetArray("messages")
                            ->array.back()
                            .GetArray("content")
                            ->array;
    const auto description = parts.front().GetString("text");
    constexpr std::string_view marker = "Video source data (untrusted):\n";
    const auto begin = description.find(marker);
    Json source;
    Q4T_CHECK(begin != description.npos &&
              Parse(description.substr(begin + marker.size()), &source));
    const auto* shape = source.Find("displayed_frame");
    Q4T_CHECK(shape && shape->IsObject() &&
              shape->GetInt("width") == expected.width &&
              shape->GetInt("height") == expected.height);
    std::string prompt, param;
    std::vector<VisionItem> media;
    Q4T_CHECK(PrepareChatPrompt(generation.generation_request, {}, &prompt,
                                &media, &param));
    Q4T_CHECK(media.size() == 1 && media.front().frames.size() == 4);
    std::vector<const uint8_t*> frames;
    std::vector<size_t> sizes;
    for (const auto& frame : media.front().frames) {
      frames.push_back(reinterpret_cast<const uint8_t*>(frame.data()));
      sizes.push_back(frame.size());
    }
    q4t::vision::ProcessorConfig config;
    config.min_pixels = 4096;
    config.max_pixels = 25165824;
    q4t::vision::ProcessedVideo processed;
    Q4T_CHECK(q4t::vision::ProcessVideo(frames, sizes, config, &processed,
                                      &param));
    Q4T_CHECK(processed.grid_w * config.patch_size == expected.width &&
              processed.grid_h * config.patch_size == expected.height &&
              processed.grid_t == 2);
  }
  return true;
}

Q4T_TEST(responses_video_snapshot_survives_restart_delete_and_missing_codec) {
  Fixture f;
  std::string id, original;
  std::vector<std::string> expected_frames;
  {
    ObjectStore objects(f.objects);
    original = Upload(objects, Bytes("file_video/h264.mp4"), "clip.mp4");
    ResponsesExecution run(objects);
    Q4T_CHECK(
        Ok(run.Prepare(Request(Stored(original)), f.config, Access(), 42)));
    std::string prompt, param;
    std::vector<VisionItem> media;
    Q4T_CHECK(PrepareChatPrompt(run.Generation().generation_request, {},
                                &prompt, &media, &param));
    expected_frames = media.at(0).frames;
    Q4T_CHECK(FinishFixture(run));
    id = run.Id();
    const auto record = ResponsesState(objects).ReadRecord(id, Access());
    Q4T_CHECK(
        Ok(record) &&
        record.value.GetArray("input_snapshots")->array.size() == 1 &&
        OutputJson(*record.value.Find("history")).find(original) !=
            std::string::npos &&
        OutputJson(*record.value.Find("history")).find("input_video_frames") ==
            std::string::npos);
    Q4T_CHECK(Ok(Files(objects).Erase(Binary(), original)));
  }
  detail::SetVideoWorkerEntryForTesting("--file-video-missing-worker");
  ScopeExit reset([] { detail::SetVideoWorkerEntryForTesting(""); });
  ObjectStore objects(f.objects);
  ResponsesExecution next(objects);
  auto request = Object({{"input", String("describe the colors again")},
                         {"previous_response_id", String(id)},
                         {"store", Boolean(false)}});
  Q4T_CHECK(Ok(next.Prepare(request, f.config, Access(), 44)) &&
            next.FileUsage()->videos == 1 &&
            next.FileUsage()->video_frames == 6);
  const auto prompt = Prompt(next);
  Q4T_CHECK(prompt.find("<|video_pad|>") != prompt.npos &&
            prompt.find("source_frames") != prompt.npos &&
            prompt.find("\"displayed_frame\":{\"width\":96,\"height\":64}") !=
                prompt.npos);
  std::string rendered, param;
  std::vector<VisionItem> restored;
  Q4T_CHECK(PrepareChatPrompt(next.Generation().generation_request, {},
                              &rendered, &restored, &param));
  Q4T_CHECK(restored.size() == 1 && restored[0].frames == expected_frames);
  auto wrong = Access();
  wrong.scope = "client:another";
  ResponsesExecution denied(objects);
  Q4T_CHECK(denied.Prepare(request, f.config, wrong, 45).http_status == 404);
  return true;
}

Q4T_TEST(responses_video_tool_result_reaches_template_without_public_bypass) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto id = Upload(objects, Bytes("file_video/vp8.webm"), "clip.webm");
  auto request =
      Object({{"store", Boolean(false)},
              {"input", Array({Object({{"type", String("function_call")},
                                       {"call_id", String("call_video")},
                                       {"name", String("read_clip")},
                                       {"arguments", String("{}")}}),
                               Object({{"type", String("function_call_output")},
                                       {"call_id", String("call_video")},
                                       {"output", Array({Stored(id)})}})})}});
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, f.config, Access(), 42)));
  const auto& generation = run.Generation();
  std::string prompt, param, code;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(generation.generation_request, {}, &prompt,
                              &media, &param, &code, nullptr, false, {}, true));
  Q4T_CHECK(media.size() == 1 && media[0].kind == VisionItem::kVideo);
  const auto tool_frames = media[0].frames;
  const auto begin = prompt.find("<tool_response>");
  const auto end = prompt.find("</tool_response>");
  const auto visuals = prompt.find("<|video_pad|>");
  Q4T_CHECK(begin != prompt.npos && begin < visuals && visuals < end &&
            prompt.find("including the first and last decoded frames") <
                visuals &&
            prompt.find("Frames between samples are not shown.") < visuals &&
            prompt.find("<0.2 seconds>") < visuals);
  ResponsesExecution direct(objects);
  auto direct_request = Request(Stored(id));
  Set(&direct_request, "store", Boolean(false));
  Q4T_CHECK(Ok(direct.Prepare(direct_request, f.config, Access(), 42)));
  Q4T_CHECK(PrepareChatPrompt(direct.Generation().generation_request, {},
                              &prompt, &media, &param, &code));
  Q4T_CHECK(media.size() == 1 && media[0].frames == tool_frames);
  Q4T_CHECK(!PrepareChatPrompt(generation.generation_request, {}, &prompt,
                               &media, &param, &code));
  const auto& messages = *generation.generation_request.GetArray("messages");
  auto part = messages.array.back().GetArray("content")->array.back();
  Set(&part, "type", String("input_video_frames"));
  ResponsesExecution forged(objects);
  auto public_request = Request(part);
  Set(&public_request, "store", Boolean(false));
  Q4T_CHECK(!forged.Prepare(public_request, f.config, Access(), 43).ok());
  return true;
}

Q4T_TEST(responses_video_combined_scan_budget_does_not_refill_per_file) {
  Fixture f;
  auto options = std::make_shared<FileExpansionOptions>(*f.config.file_inputs);
  options->extraction.video.max_scanned_frames = 11;
  f.config.file_inputs = options;
  ObjectStore objects(f.objects);
  const auto id = Upload(objects, Bytes("file_video/vp8.webm"), "clip.webm");
  auto request = Request(Stored(id));
  Field(Field(request, "input").array[0], "content")
      .array.push_back(Stored(id));
  Set(&request, "store", Boolean(false));
  ResponsesExecution run(objects);
  Q4T_CHECK(run.Prepare(request, f.config, Access(), 42).http_status == 413 &&
            !run.GenerationReady());
  return true;
}

Q4T_TEST(responses_program_video_carrier_is_expanded_without_losing_frames) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto id = Upload(objects, Bytes("file_video/vp9.webm"), "clip.webm");
  const auto content = Array({Stored(id)});
  std::string encoded;
  Json decoded;
  Q4T_CHECK(EncodeResponsesProgramOutput(content, &encoded) &&
            DecodeResponsesProgramOutput(encoded, &decoded) &&
            OutputJson(decoded) == OutputJson(content));
  const auto items = Array({Object({{"id", String("po_video_fixture")},
                                    {"type", String("program_output")},
                                    {"result", String(encoded)}})});
  FileExpansion prepared(&objects, *f.config.file_inputs);
  Q4T_CHECK(Ok(prepared.Prepare(items, FileInputProfile::kResponsesStored,
                                Array(), Binary())));
  Json rendered;
  Q4T_CHECK(Ok(prepared.Render(items, &rendered)) &&
            DecodeResponsesProgramOutput(rendered.array[0].GetString("result"),
                                         &decoded));
  Q4T_CHECK(decoded.array.back().GetString("type") == "input_video_frames" &&
            decoded.array.back().GetArray("video_frames")->array.size() == 6);
  Json mapped;
  Q4T_CHECK(MapPreparedVideoContent(decoded.array.back(), &mapped));
  Q4T_CHECK(mapped.GetArray("video_timestamps")->array[5].number == 2.5);
  auto malformed = decoded;
  Field(malformed.array.back(), "video_timestamps").array.pop_back();
  Q4T_CHECK(!EncodeResponsesProgramOutput(malformed, &encoded));
  // A file still uses ordinary source validation inside the private carrier.
  Q4T_CHECK(!EncodeResponsesProgramOutput(
      Array({Object({{"type", String("input_file")},
                     {"file_url", String("file:///etc/passwd")}})}),
      &encoded));
  return true;
}

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
