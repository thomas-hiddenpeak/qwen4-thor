#include "q4t/server/realtime_file_inputs.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
BinaryAccess Access() {
  return {"client:realtime-files", Clock::now() + 5s, {}};
}
Json Message(Json part) {
  return Object({{"role", String("user")},
                 {"type", String("message")},
                 {"content", Array({std::move(part)})}});
}
Json Text(std::string text) {
  return Object({{"type", String("input_text")}, {"text", String(text)}});
}
Json Inline(bool csv = false) {
  return Object(
      {{"type", String("input_file")},
       {"filename", String(csv ? "facts.csv" : "facts.txt")},
       {"file_data", String(csv ? "bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK"
                                : "b3JpZ2luYWwgQWxwaGEgNDIK")}});
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok() && !reply.commit_unknown;
}
struct Fixture {
  ObjectStoreOptions options;
  ChatPlanConfig config;
  RealtimeChatPlan plan;
  ChatPlanError error;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(output ? output : ".q4t-work/realtime-files");
    fs::create_directories(root);
    auto path = (root / "realtime-files-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("file fixture path");
    options.directory = path + "/objects";
    config.model = "cpu-file-lifecycle";
    config.max_len = 8192;
    config.structured = config.allow_media = true;
    auto files = std::make_shared<FileExpansionOptions>();
    files->extraction.legacy.work_directory = path + "/codec";
    config.file_inputs = files;
  }
  bool Plan(Json part) {
    auto settings = DefaultRealtimeChatSettings(config, "");
    const auto reference =
        Object({{"id", String("prompt_files")}, {"version", String("1")}});
    const PromptSnapshot snapshot{
        PromptApi::kRealtime, Access().scope, reference,
        Object({{"input", Array({Message(std::move(part))})}})};
    Set(&settings, "prompt", reference);
    Set(&settings, "max_output_tokens", Number(32));
    const auto status = PlanRealtimeChat(
        settings, Object(), Array({Message(Text("read my source"))}), config,
        &plan, &error, &snapshot);
    if (!status)
      std::fprintf(stderr, "%s: %s\n", error.code.c_str(),
                   status.message().c_str());
    return status.ok();
  }
};
std::string Upload(ObjectStore& objects, std::string_view bytes,
                   std::string_view name) {
  const auto access = Access();
  FileUpload upload(objects, access, "multipart/form-data; boundary=file");
  const auto prefix =
      "--file\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
      "user_data\r\n--file\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"" +
      std::string(name) + "\"\r\n\r\n";
  if (!upload.Feed(prefix, access.deadline).ok() ||
      !upload.Feed(bytes, access.deadline).ok() ||
      !upload.Feed("\r\n--file--\r\n", access.deadline).ok())
    throw std::runtime_error("File upload fixture");
  const auto reply = upload.Finish();
  if (!reply.ok()) throw std::runtime_error(reply.message);
  return reply.value.GetString("id");
}
}  // namespace

Q4T_TEST(
    realtime_file_prefix_resolves_actual_content_before_context_measurement) {
  Fixture f;
  Q4T_CHECK(f.Plan(Inline()) && !f.plan.input_ready);
  int measured = 0;
  Q4T_CHECK(!FitRealtimeChatContext(
      f.config,
      [&](const auto&, size_t*, auto*) {
        ++measured;
        return q4t::Status{};
      },
      &f.plan, &f.error));
  Q4T_CHECK(measured == 0 &&
            f.error.code == "realtime_input_preparation_pending");
  const auto original = OutputJson(f.plan.envelope.request);
  RealtimeFileInputs owner;
  Q4T_CHECK(Ok(owner.Prepare(f.plan, f.config, nullptr, Access())));
  Q4T_CHECK(owner.Files() && !owner.Files()->PendingSheets());
  Q4T_CHECK(Ok(owner.Finish(f.config, &f.plan)) && f.plan.input_ready);
  const auto prepared = OutputJson(f.plan.generation.generation_request);
  Q4T_CHECK(prepared.find("original Alpha 42") != std::string::npos &&
            prepared.find("read my source") != std::string::npos &&
            prepared.find("b3JpZ2luYWwgQWxwaGEgNDIK") == std::string::npos);
  Q4T_CHECK(OutputJson(f.plan.envelope.request) == original);
  Q4T_CHECK(f.plan.generation.chat.prepared_source_data &&
            f.plan.generation.input.items.array.size() == 2 &&
            f.plan.prompt_items == 1 &&
            RealtimeRetainedHistory(f.plan) == std::vector<size_t>{0});
  Q4T_CHECK(owner.Usage()->files == 1 && owner.Usage()->source_bytes == 18);
  Q4T_CHECK(!owner.Finish(f.config, &f.plan).ok());
  return true;
}

Q4T_TEST(
    realtime_file_prefix_stored_scope_and_accepted_bytes_survive_deletion) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto id = Upload(objects, "actual owned fact: violet", "fact.txt");
  Q4T_CHECK(f.Plan(
      Object({{"type", String("input_file")}, {"file_id", String(id)}})));
  auto wrong = Access();
  wrong.scope = "client:other";
  RealtimeFileInputs denied;
  Q4T_CHECK(!denied.Prepare(f.plan, f.config, &objects, wrong).ok());
  Q4T_CHECK(!f.plan.input_ready);
  RealtimeFileInputs owned;
  Q4T_CHECK(Ok(owned.Prepare(f.plan, f.config, &objects, Access())));
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), id)));
  Q4T_CHECK(Ok(owned.Finish(f.config, &f.plan)));
  Q4T_CHECK(OutputJson(f.plan.generation.generation_request)
                .find("actual owned fact: violet") != std::string::npos);
  return true;
}

Q4T_TEST(realtime_file_prefix_stored_image_keeps_actual_pixels_and_usage) {
  Fixture f;
  ObjectStore objects(f.options);
  std::ifstream input(
      fs::path(__FILE__).parent_path() / "fixtures/file_images/original.png",
      std::ios::binary);
  Q4T_CHECK(input.good());
  const std::string bytes((std::istreambuf_iterator<char>(input)), {});
  const auto id = Upload(objects, bytes, "original.png");
  Q4T_CHECK(f.Plan(Object({{"type", String("input_image")},
                           {"file_id", String(id)},
                           {"detail", String("low")}})));
  RealtimeFileInputs owner;
  Q4T_CHECK(Ok(owner.Prepare(f.plan, f.config, &objects, Access())) &&
            Ok(owner.Finish(f.config, &f.plan)));
  Q4T_CHECK(owner.Usage()->images == 1 && owner.Usage()->pixels > 0 &&
            owner.Usage()->source_bytes == bytes.size());
  Q4T_CHECK(OutputJson(f.plan.generation.generation_request)
                .find("data:image/png;base64,") != std::string::npos);
  Q4T_CHECK(f.plan.generation.chat.prepared_source_data);
  return true;
}

Q4T_TEST(realtime_file_sheets_require_completed_owned_preparation_before_plan) {
  Fixture f;
  Q4T_CHECK(f.Plan(Inline(true)));
  RealtimeFileInputs owner;
  Q4T_CHECK(Ok(owner.Prepare(f.plan, f.config, nullptr, Access())));
  Q4T_CHECK(owner.Files()->PendingSheets() &&
            owner.Files()->PendingOutputBudget() > 0);
  Q4T_CHECK(owner.Finish(f.config, &f.plan).code ==
            "file_augmentation_required");
  Q4T_CHECK(!f.plan.input_ready);
  FileSheetStep step;
  Q4T_CHECK(Ok(owner.Files()->NextSheet(f.config, &step)));
  Q4T_CHECK(OutputJson(step.request).find("alpha") != std::string::npos);
  constexpr auto summary =
      R"({"summary":"CPU fixture receipt","headers":["name","value"]})";
  Q4T_CHECK(!owner.Files()->CompleteSheet(step, summary, "length", 40, 8).ok());
  Q4T_CHECK(!f.plan.input_ready && owner.Usage()->model_steps == 0);
  // Explicit CPU receipt tests ownership, not model generation or quality.
  Q4T_CHECK(Ok(owner.Files()->CompleteSheet(step, summary, "stop", 40, 8)));
  Q4T_CHECK(Ok(owner.Finish(f.config, &f.plan)));
  Q4T_CHECK(owner.Usage()->model_input_tokens == 40 &&
            owner.Usage()->model_output_tokens == 8 &&
            owner.Usage()->model_steps == 1);
  Q4T_CHECK(OutputJson(f.plan.generation.generation_request).find("alpha") !=
            std::string::npos);
  return true;
}

Q4T_TEST(
    realtime_file_preparation_cancellation_expiry_and_changed_owner_are_atomic) {
  Fixture f;
  Q4T_CHECK(f.Plan(Inline()));
  RealtimeFileInputs cancelled;
  auto access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(cancelled.Prepare(f.plan, f.config, nullptr, access).http_status ==
            499);
  RealtimeFileInputs expired;
  access = Access();
  access.deadline = Clock::now();
  Q4T_CHECK(expired.Prepare(f.plan, f.config, nullptr, access).http_status ==
            408);
  Q4T_CHECK(!f.plan.input_ready);
  bool stop = false;
  access = Access();
  access.cancelled = [&] { return stop; };
  RealtimeFileInputs owner;
  Q4T_CHECK(Ok(owner.Prepare(f.plan, f.config, nullptr, access)));
  auto changed = f.plan;
  Set(&changed.envelope.request, "instructions", String("different owner"));
  Q4T_CHECK(owner.Finish(f.config, &changed).code ==
            "realtime_input_owner_invalid");
  changed = f.plan;
  ++changed.prompt_items;
  Q4T_CHECK(owner.Finish(f.config, &changed).code ==
            "realtime_input_owner_invalid");
  stop = true;
  Q4T_CHECK(owner.Finish(f.config, &f.plan).http_status == 499);
  Q4T_CHECK(!f.plan.input_ready &&
            f.plan.generation.generation_request.IsNull());
  stop = false;
  Q4T_CHECK(Ok(owner.Finish(f.config, &f.plan)));
  return true;
}

Q4T_TEST(
    realtime_file_deployment_budgets_and_changed_client_limit_cannot_expand) {
  for (const int selected : {0, 1, 2}) {
    Fixture f;
    auto options =
        std::make_shared<FileExpansionOptions>(*f.config.file_inputs);
    if (selected == 0) options->max_source_bytes = 1;
    if (selected == 1) options->max_retained_bytes = 1;
    if (selected == 2) options->max_rendered_bytes = 1;
    f.config.file_inputs = options;
    Q4T_CHECK(f.Plan(Inline()));
    RealtimeFileInputs owner;
    auto result = owner.Prepare(f.plan, f.config, nullptr, Access());
    if (result.ok()) result = owner.Finish(f.config, &f.plan);
    Q4T_CHECK(!result.ok() && !f.plan.input_ready);
  }
  Fixture f;
  Q4T_CHECK(f.Plan(Inline()));
  RealtimeFileInputs owner;
  Q4T_CHECK(Ok(owner.Prepare(f.plan, f.config, nullptr, Access())));
  ClientLimits limits;
  limits.max_output_tokens = 16;
  f.config.client_limits = &limits;
  Q4T_CHECK(owner.Finish(f.config, &f.plan).code ==
            "client_output_limit_exceeded");
  Q4T_CHECK(!f.plan.input_ready);
  return true;
}

Q4T_TEST(realtime_file_invalid_content_is_rejected_before_any_acquisition) {
  Fixture f;
  ObjectStore objects(f.options);
  auto bad = Inline();
  Set(&bad, "file_id", String("file_unread"));
  Q4T_CHECK(!f.Plan(bad));
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(f.Plan(Inline()));
  Q4T_CHECK(
      !CompleteRealtimeChatInput(f.config, Array(), true, &f.plan, &f.error));
  Q4T_CHECK(f.error.code == "realtime_input_owner_invalid" &&
            !f.plan.input_ready);
  return true;
}
