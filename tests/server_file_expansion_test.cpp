#include "q4t/server/file_expansion.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/files.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
BinaryAccess Access() { return {"client:expand", Clock::now() + 30s, {}}; }
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions objects;
  FileExpansionOptions options;
  GrammarControl control;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(out ? out : ".q4t-work/file-expansion-cpu");
    fs::create_directories(root);
    auto path = (root / "file-expansion-XXXXXX").string();
    if (!mkdtemp(path.data()))
      throw std::runtime_error("file expansion fixture");
    objects.directory = path + "/objects";
    objects.restart_backoff_ms = 0;
    options.extraction.legacy.work_directory = path + "/codec";
    options.hold_seconds = 60;
    control.counters = std::make_shared<GrammarWorkerCounters>();
  }
};
Json Inline(std::string data = "b3JpZ2luYWwgQWxwaGEgNDIK",
            std::string name = "facts.txt") {
  return Object({{"type", String("input_file")},
                 {"file_data", String(data)},
                 {"filename", String(name)}});
}
Json Item(Json part, std::string id = "msg_file") {
  return Object({{"type", String("message")},
                 {"id", String(id)},
                 {"role", String("user")},
                 {"status", String("completed")},
                 {"content", Array({std::move(part)})}});
}
Json Stored(std::string id) {
  return Object({{"type", String("input_file")},
                 {"file_id", String(id)},
                 {"detail", String("low")}});
}
std::string Read(const char* name) {
  std::ifstream in(
      fs::path(__FILE__).parent_path() / "fixtures/file_nested" / name,
      std::ios::binary);
  if (!in) throw std::runtime_error("file fixture");
  return std::string(std::istreambuf_iterator<char>(in), {});
}
std::string Upload(ObjectStore& objects, std::string_view bytes,
                   std::string name) {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=expand");
  const auto prefix =
      "--expand\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nuser_data\r\n"
      "--expand\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" +
      name +
      "\"\r\n"
      "Content-Type: application/octet-stream\r\n\r\n";
  if (!upload.Feed(prefix, Access().deadline).ok() ||
      !upload.Feed(bytes, Access().deadline).ok() ||
      !upload.Feed("\r\n--expand--\r\n", Access().deadline).ok())
    throw std::runtime_error("upload");
  auto result = upload.Finish();
  if (!result.ok()) throw std::runtime_error(result.message);
  return result.value.GetString("id");
}
bool Retain(ObjectStore& objects, const Json& items, const Json& bindings) {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.scope = Access().scope;
  request.kind = ObjectKind::kConversationItem;
  request.id = items.array[0].GetString("id");
  request.data =
      Object({{"item", items.array[0]}, {"input_snapshots", bindings}});
  return Ok(objects.Execute(request, Access().deadline));
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu-test-step";
  config.structured = true;
  config.max_len = 8192;
  return config;
}
Json Csv() {
  // name,value / alpha,42 / beta,7, literal original bytes.
  return Inline("bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK", "data.csv");
}
constexpr std::string_view kFixtureSummary =
    R"({"summary":"CPU callback fixture: alpha=42, beta=7","headers":["name: label","value: quantity"]})";
}  // namespace

Q4T_TEST(
    file_expansion_sheet_total_budget_tracks_actual_receipts_without_refill) {
  Fixture fixture;
  fixture.options.summary_output_tokens = 64;
  fixture.options.total_summary_output_tokens = 100;
  FileExpansion expansion(nullptr, fixture.options);
  const auto items = Array({Item(Csv()), Item(Csv(), "msg_second")});
  Q4T_CHECK(Ok(expansion.Prepare(items, FileInputProfile::kResponsesStored,
                                 Json(), Access())));
  Q4T_CHECK(expansion.PendingOutputBudget() == 100);
  FileSheetStep first, second;
  Q4T_CHECK(Ok(expansion.NextSheet(Config(), &first)));
  Q4T_CHECK(first.request.GetInt("max_completion_tokens") == 64);
  // Explicit CPU receipts exercise the protocol, not actual model quality.
  Q4T_CHECK(
      Ok(expansion.CompleteSheet(first, kFixtureSummary, "stop", 100, 50)));
  Q4T_CHECK(expansion.PendingOutputBudget() == 50);
  Q4T_CHECK(Ok(expansion.NextSheet(Config(), &second)));
  Q4T_CHECK(second.request.GetInt("max_completion_tokens") == 50);
  Q4T_CHECK(
      expansion.CompleteSheet(second, kFixtureSummary, "stop", 100, 51).code ==
      "file_augmentation_incomplete");
  Q4T_CHECK(expansion.Usage().model_output_tokens == 50);
  Q4T_CHECK(
      Ok(expansion.CompleteSheet(second, kFixtureSummary, "stop", 101, 50)));
  Q4T_CHECK(!expansion.PendingSheets() && expansion.PendingOutputBudget() == 0);
  Q4T_CHECK(expansion.Usage().model_input_tokens == 201 &&
            expansion.Usage().model_output_tokens == 100 &&
            expansion.Usage().model_steps == 2);
  Json rendered;
  Q4T_CHECK(Ok(expansion.Render(items, &rendered)));
  return true;
}

Q4T_TEST(file_expansion_exhausted_preparation_cannot_issue_another_sheet) {
  Fixture fixture;
  fixture.options.summary_output_tokens = 64;
  fixture.options.total_summary_output_tokens = 64;
  FileExpansion expansion(nullptr, fixture.options);
  const auto items = Array({Item(Csv()), Item(Csv(), "msg_second")});
  Q4T_CHECK(Ok(expansion.Prepare(items, FileInputProfile::kResponsesStored,
                                 Json(), Access())));
  FileSheetStep first, next;
  Q4T_CHECK(Ok(expansion.NextSheet(Config(), &first)));
  Q4T_CHECK(
      Ok(expansion.CompleteSheet(first, kFixtureSummary, "stop", 100, 64)));
  Q4T_CHECK(expansion.PendingSheets() && expansion.PendingOutputBudget() == 0);
  const auto reply = expansion.NextSheet(Config(), &next);
  Q4T_CHECK(reply.http_status == 413 &&
            reply.code == "file_preparation_output_exhausted" &&
            next.request.IsNull());
  Json rendered;
  Q4T_CHECK(!expansion.Render(items, &rendered).ok());
  return true;
}

Q4T_TEST(file_expansion_all_source_validation_precedes_external_io) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  auto bad = Inline();
  Set(&bad, "file_id", String("file_missing"));
  FileExpansion expansion(&objects, fixture.options);
  const auto reply = expansion.Prepare(
      Array({Item(Stored("file_missing")), Item(bad, "msg_bad")}),
      FileInputProfile::kResponsesStored, Json(), Access(), false,
      fixture.control);
  Q4T_CHECK(reply.http_status == 400 && objects.Health().workers_started == 0 &&
            fixture.control.counters->started == 0);
  Json result;
  Q4T_CHECK(!expansion.Render(Array(), &result).ok() && result.IsNull());
  return true;
}

Q4T_TEST(
    file_expansion_real_text_preserves_original_tool_paths_and_cache_boundary) {
  Fixture fixture;
  auto part = Inline();
  Set(&part, "prompt_cache_breakpoint",
      Object({{"mode", String("explicit")}, {"ttl", String("30m")}}));
  auto tool = Object({{"type", String("function_call_output")},
                      {"id", String("output_file")},
                      {"call_id", String("call_1")},
                      {"output", Array({part})}});
  Set(&tool, "metadata", Object({{"content", Array({Inline("not base64")})}}));
  const auto original = Array({tool});
  const auto before = OutputJson(original);
  FileExpansion expansion(nullptr, fixture.options);
  Q4T_CHECK(Ok(expansion.Prepare(original, FileInputProfile::kResponsesStored,
                                 Json(), Access(), false, fixture.control)));
  Json rendered;
  Q4T_CHECK(Ok(expansion.Render(original, &rendered)));
  const auto& content = rendered.array[0].GetArray("output")->array;
  Q4T_CHECK(
      content.size() == 1 && content[0].GetString("type") == "input_text" &&
      content[0].GetString("text").find("original Alpha 42") !=
          std::string::npos &&
      content[0].Has("prompt_cache_breakpoint") &&
      OutputJson(original) == before &&
      OutputJson(*rendered.array[0].Find("metadata")) ==
          OutputJson(*tool.Find("metadata")) &&
      expansion.Usage().files == 1 && expansion.Usage().source_bytes == 18);
  auto changed = original;
  Set(&Field(changed.array[0], "output").array[0], "filename",
      String("changed.txt"));
  Q4T_CHECK(!expansion.Render(changed, &rendered).ok() && rendered.IsNull());
  return true;
}

Q4T_TEST(file_expansion_combined_source_text_and_rendered_budgets_are_atomic) {
  Fixture fixture;
  const auto items = Array({Item(Inline()), Item(Inline(), "msg_two")});
  fixture.options.max_source_bytes = 30;
  FileExpansion source(nullptr, fixture.options);
  Q4T_CHECK(source.Prepare(items, FileInputProfile::kResponsesStored, Json(),
                           Access())
                    .http_status == 413 &&
            source.Usage().files == 0);
  fixture.options.max_source_bytes = kFileInputBytes;
  fixture.options.extraction.document.max_text_bytes = 30;
  FileExpansion text(nullptr, fixture.options);
  Q4T_CHECK(
      text.Prepare(items, FileInputProfile::kResponsesStored, Json(), Access())
              .http_status == 413 &&
      text.Usage().files == 0);
  fixture.options.extraction.document.max_text_bytes = 4096;
  fixture.options.max_rendered_bytes = 100;
  FileExpansion render(nullptr, fixture.options);
  Q4T_CHECK(Ok(render.Prepare(items, FileInputProfile::kResponsesStored, Json(),
                              Access())));
  Json result = Object();
  Q4T_CHECK(render.Render(items, &result).http_status == 413 &&
            result.IsNull());
  return true;
}

Q4T_TEST(file_expansion_combined_retained_payload_is_atomic) {
  Fixture fixture;
  const auto one = Array({Item(Inline())});
  FileExpansion measured(nullptr, fixture.options);
  Q4T_CHECK(Ok(measured.Prepare(one, FileInputProfile::kResponsesStored, Json(),
                                Access())));
  fixture.options.max_retained_bytes = measured.RetainedBytes();
  FileExpansion fits(nullptr, fixture.options);
  Q4T_CHECK(Ok(
      fits.Prepare(one, FileInputProfile::kResponsesStored, Json(), Access())));
  FileExpansion combined(nullptr, fixture.options);
  const auto two = Array({Item(Inline()), Item(Inline(), "msg_second")});
  Q4T_CHECK(
      combined
          .Prepare(two, FileInputProfile::kResponsesStored, Json(), Access())
          .http_status == 413);
  Q4T_CHECK(combined.Usage().files == 0 && combined.RetainedBytes() == 0);
  Json output;
  Q4T_CHECK(!combined.Render(two, &output).ok() && output.IsNull());
  return true;
}

Q4T_TEST(file_expansion_frozen_inputs_obey_new_retained_budget) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto items = Array({Item(Inline())});
  FileExpansion original(&objects, fixture.options);
  Q4T_CHECK(Ok(original.Prepare(items, FileInputProfile::kResponsesStored,
                                Json(), Access())));
  Q4T_CHECK(Ok(original.Persist(Access())));
  fixture.options.max_retained_bytes = original.RetainedBytes() - 1;
  FileExpansion limited(&objects, fixture.options);
  Q4T_CHECK(limited
                .Prepare(items, FileInputProfile::kResponsesStored,
                         original.Bindings(), Access(), true)
                .http_status == 413);
  Q4T_CHECK(limited.RetainedBytes() == 0 && limited.Usage().files == 0);
  fixture.options.max_retained_bytes = original.RetainedBytes();
  FileExpansion fits(&objects, fixture.options);
  Q4T_CHECK(Ok(fits.Prepare(items, FileInputProfile::kResponsesStored,
                            original.Bindings(), Access(), true)));
  Q4T_CHECK(Ok(original.Release(Access())));
  return true;
}

Q4T_TEST(file_expansion_summary_cannot_exceed_retained_payload_budget) {
  Fixture fixture;
  const auto items = Array({Item(Csv())});
  FileExpansion measured(nullptr, fixture.options);
  Q4T_CHECK(Ok(measured.Prepare(items, FileInputProfile::kResponsesStored,
                                Json(), Access())));
  fixture.options.max_retained_bytes = measured.RetainedBytes() + 4;
  FileExpansion limited(nullptr, fixture.options);
  Q4T_CHECK(Ok(limited.Prepare(items, FileInputProfile::kResponsesStored,
                               Json(), Access())));
  FileSheetStep step;
  Q4T_CHECK(Ok(limited.NextSheet(Config(), &step)));
  // CPU receipt only. The execution owner accounts failed actual model work.
  Q4T_CHECK(limited.CompleteSheet(step, kFixtureSummary, "stop", 100, 50)
                .http_status == 413);
  Q4T_CHECK(limited.PendingSheets() &&
            limited.Usage().augmentation_bytes == 0 &&
            limited.RetainedBytes() == measured.RetainedBytes());
  return true;
}

Q4T_TEST(file_expansion_actual_pdf_page_and_chat_profile_are_complete) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto id = Upload(objects, Read("page.pdf"), "page.pdf");
  const auto items = Array({Item(Stored(id))});
  FileExpansion response(&objects, fixture.options);
  Q4T_CHECK(Ok(response.Prepare(items, FileInputProfile::kResponsesStored,
                                Json(), Access())));
  Json result;
  Q4T_CHECK(Ok(response.Render(items, &result)) && response.Usage().pages == 1);
  const auto& parts = result.array[0].GetArray("content")->array;
  Q4T_CHECK(parts.size() == 3 &&
            parts[1].GetString("text").find("\"page\":1") != std::string::npos);
  std::string raw;
  const char* code = nullptr;
  Q4T_CHECK(DecodeMediaUrl(parts[2].GetString("image_url"), &raw, &code));
  q4t::vision::MediaHeader header;
  Q4T_CHECK(
      q4t::vision::ReadMediaHeader(
          std::span(reinterpret_cast<const uint8_t*>(raw.data()), raw.size()),
          &header) &&
      header.width <= 512 && header.height <= 512);
  auto chat =
      Array({Item(Object({{"type", String("file")},
                          {"file", Object({{"file_id", String(id)}})}}))});
  FileExpansion chat_files(&objects, fixture.options);
  Q4T_CHECK(
      Ok(chat_files.Prepare(chat, FileInputProfile::kChat, Json(), Access())) &&
      Ok(chat_files.Render(chat, &result)));
  Q4T_CHECK(result.array[0].GetArray("content")->array[2].GetString("type") ==
            "image_url");
  FileExpansion no_text(nullptr, fixture.options);
  auto non_pdf = Array(
      {Item(Object({{"type", String("file")},
                    {"file", Object({{"filename", String("x.txt")},
                                     {"file_data", String("eA==")}})}}))});
  Q4T_CHECK(no_text.Prepare(non_pdf, FileInputProfile::kChat, Json(), Access())
                .code == "invalid_file_format");
  return true;
}

Q4T_TEST(
    file_expansion_frozen_history_survives_delete_restart_and_codec_outage) {
  Fixture fixture;
  Json items, bindings, expected;
  {
    ObjectStore objects(fixture.objects);
    const auto id = Upload(objects, "stable old file", "original.txt");
    items = Array({Item(Stored(id))});
    FileExpansion prepared(&objects, fixture.options);
    Q4T_CHECK(Ok(prepared.Prepare(items, FileInputProfile::kResponsesStored,
                                  Json(), Access())) &&
              Ok(prepared.Persist(Access())) &&
              Ok(prepared.Render(items, &expected)));
    bindings = prepared.Bindings();
    Q4T_CHECK(Retain(objects, items, bindings) &&
              Ok(prepared.Release(Access())) &&
              Ok(Files(objects).Erase(Access(), id)));
  }
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  FileExpansion restored(&objects, fixture.options);
  Q4T_CHECK(Ok(restored.Prepare(items, FileInputProfile::kResponsesStored,
                                bindings, Access(), true, fixture.control)));
  Json result;
  Q4T_CHECK(Ok(restored.Render(items, &result)) &&
            OutputJson(result) == OutputJson(expected) &&
            fixture.control.counters->started == 0);
  FileExpansion absent(&objects, fixture.options);
  Q4T_CHECK(absent
                .Prepare(items, FileInputProfile::kResponsesStored, Json(),
                         Access(), true)
                .code == "file_expansion_invalid");
  auto foreign = Access();
  foreign.scope = "client:other";
  FileExpansion scoped(&objects, fixture.options);
  Q4T_CHECK(scoped
                .Prepare(items, FileInputProfile::kResponsesStored, bindings,
                         foreign, true)
                .http_status == 404);
  return true;
}

Q4T_TEST(
    file_expansion_spreadsheet_step_requires_complete_exact_model_callback) {
  Fixture fixture;
  FileExpansion expansion(nullptr, fixture.options);
  const auto items = Array({Item(Csv())});
  Q4T_CHECK(Ok(expansion.Prepare(items, FileInputProfile::kResponsesStored,
                                 Json(), Access())) &&
            expansion.PendingSheets());
  Json rendered;
  Q4T_CHECK(expansion.Render(items, &rendered).code ==
                "file_augmentation_required" &&
            rendered.IsNull());
  FileSheetStep step;
  Q4T_CHECK(Ok(expansion.NextSheet(Config(), &step)) &&
            step.chat.constraints.json_output &&
            !step.request.GetBool("enable_thinking", true) &&
            !step.request.Has("tools"));
  const auto text =
      step.request.GetArray("messages")->array[1].GetString("content");
  Q4T_CHECK(text.find("alpha") != std::string::npos &&
            text.find("42") != std::string::npos &&
            text.find("\"source_rows\":3") != std::string::npos);
  Q4T_CHECK(
      expansion.CompleteSheet(step, kFixtureSummary, "length", 37, 11).code ==
      "file_augmentation_incomplete");
  auto wrong = step;
  Set(&wrong.request, "model", String("wrong"));
  Q4T_CHECK(InputValueIdentity(wrong.request, &wrong.prompt_sha256));
  Q4T_CHECK(
      !expansion.CompleteSheet(wrong, kFixtureSummary, "stop", 37, 11).ok());
  for (const auto* bad :
       {R"({"summary":"x","headers":[]})",
        R"({"summary":"","headers":["x","y"]})",
        R"({"summary":"x","summary":"y","headers":["x","y"]})"})
    Q4T_CHECK(!expansion.CompleteSheet(step, bad, "stop", 37, 11).ok() &&
              expansion.PendingSheets());
  // This supplies the executor callback contract; no model execution is
  // claimed by a CPU fixture. Real output/usage acceptance remains required.
  Q4T_CHECK(
      Ok(expansion.CompleteSheet(step, kFixtureSummary, "stop", 37, 11)) &&
      !expansion.PendingSheets() && Ok(expansion.Render(items, &rendered)) &&
      !expansion.CompleteSheet(step, kFixtureSummary, "stop", 37, 11).ok());
  const auto wire = OutputJson(rendered);
  Q4T_CHECK(wire.find("CPU callback fixture") != wire.npos &&
            wire.find("cpu-test-step") != wire.npos &&
            wire.find("alpha") != wire.npos && wire.find("beta") != wire.npos);
  return true;
}

Q4T_TEST(
    file_expansion_pending_and_augmented_snapshots_are_separate_immutable_versions) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto items = Array({Item(Csv())});
  FileExpansion expansion(&objects, fixture.options);
  Q4T_CHECK(Ok(expansion.Prepare(items, FileInputProfile::kResponsesStored,
                                 Json(), Access())) &&
            Ok(expansion.Persist(Access())));
  const auto pending = expansion.Bindings();
  FileSheetStep step;
  Q4T_CHECK(
      Ok(expansion.NextSheet(Config(), &step)) &&
      Ok(expansion.CompleteSheet(step, kFixtureSummary, "stop", 37, 11)) &&
      Ok(expansion.Persist(Access())) &&
      OutputJson(pending) != OutputJson(expansion.Bindings()));
  FileExpansion raw(&objects, fixture.options),
      ready(&objects, fixture.options);
  Q4T_CHECK(Ok(raw.Prepare(items, FileInputProfile::kResponsesStored, pending,
                           Access(), true)) &&
            raw.PendingSheets() &&
            Ok(ready.Prepare(items, FileInputProfile::kResponsesStored,
                             expansion.Bindings(), Access(), true)) &&
            !ready.PendingSheets());
  const auto& binding = expansion.Bindings().array[0];
  InputSnapshotRef reference;
  Q4T_CHECK(ParseInputSnapshotReference(*binding.Find("snapshot"), &reference));
  InputSnapshotStore store(objects);
  FileInputSnapshot file;
  Q4T_CHECK(Ok(ReadFileInputSnapshot(store, Access(), reference,
                                     binding.GetString("binding_sha256"),
                                     fixture.options.extraction, {}, &file)) &&
            file.augmentations.size() == 1 &&
            file.augmentations[0].input_tokens == 37 &&
            file.augmentations[0].output_tokens == 11 &&
            file.augmentations[0].prompt_sha256 == step.prompt_sha256);
  Q4T_CHECK(Ok(expansion.Release(Access())));
  return true;
}

Q4T_TEST(file_expansion_combined_pdf_pages_and_history_budgets_are_enforced) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  auto id = Upload(objects, Read("page.pdf"), "page.pdf");
  const auto items = Array({Item(Stored(id)), Item(Stored(id), "msg_two")});
  FileExpansion initial(&objects, fixture.options);
  Q4T_CHECK(Ok(initial.Prepare(items, FileInputProfile::kResponsesStored,
                               Json(), Access())) &&
            Ok(initial.Persist(Access())) && initial.Usage().pages == 2);
  fixture.options.extraction.pdf.max_pages = 1;
  FileExpansion lowered(&objects, fixture.options);
  Q4T_CHECK(lowered.Prepare(items, FileInputProfile::kResponsesStored,
                            initial.Bindings(), Access(), true)
                    .http_status == 413 &&
            lowered.Usage().files == 0);
  Q4T_CHECK(Ok(initial.Release(Access())));
  return true;
}

Q4T_TEST(
    file_expansion_unknown_persistence_never_replays_or_erases_owned_state) {
  Fixture fixture;
  fixture.objects.io_timeout_ms = 250;
  ObjectWorkerEntryForTesting("--q4t-object-worker-snapshot-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  ObjectReply failed;
  {
    FileExpansion expansion(&objects, fixture.options);
    Q4T_CHECK(Ok(expansion.Prepare(Array({Item(Inline())}),
                                   FileInputProfile::kResponsesStored, Json(),
                                   Access())));
    failed = expansion.Persist(Access());
    Q4T_CHECK(failed.commit_unknown && expansion.CommitUnknown() &&
              expansion.Bindings().array.empty());
    auto repeated = expansion.Persist(Access());
    Q4T_CHECK(repeated.commit_unknown &&
              OutputJson(repeated.value) == OutputJson(failed.value) &&
              expansion.Release(Access()).commit_unknown);
  }
  size_t records = 0;
  for (const auto& item : fs::directory_iterator(fixture.objects.directory))
    records += item.path().extension() == ".json";
  Q4T_CHECK(records == 4 && objects.Health().workers_started == 1);
  return true;
}

Q4T_TEST(
    file_expansion_plain_requests_cancelled_and_missing_store_remain_isolated) {
  Fixture fixture;
  const auto plain = Array({Object({{"type", String("message")},
                                    {"role", String("user")},
                                    {"content", String("plain")}})});
  FileExpansion no_store(nullptr, fixture.options);
  Json rendered;
  Q4T_CHECK(Ok(no_store.Prepare(plain, FileInputProfile::kResponsesStored,
                                Json(), Access())) &&
            Ok(no_store.Persist(Access())) &&
            Ok(no_store.Render(plain, &rendered)) &&
            OutputJson(rendered) == OutputJson(plain));
  FileExpansion cancelled(nullptr, fixture.options);
  auto access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(cancelled
                .Prepare(Array({Item(Inline())}),
                         FileInputProfile::kResponsesStored, Json(), access)
                .http_status == 499);
  FileExpansion expired(nullptr, fixture.options);
  access = Access();
  access.deadline = Clock::now() - 1ms;
  Q4T_CHECK(expired
                .Prepare(Array({Item(Inline())}),
                         FileInputProfile::kResponsesStored, Json(), access)
                .http_status == 408);
  FileExpansion missing(nullptr, fixture.options);
  Q4T_CHECK(missing
                .Prepare(Array({Item(Stored("file_missing"))}),
                         FileInputProfile::kResponsesStored, Json(), Access())
                .http_status == 503);
  Q4T_CHECK(fixture.control.counters->started == 0);
  return true;
}
