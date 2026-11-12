#include "q4t/server/chat_file_inputs.h"

#include <algorithm>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
BinaryAccess Access() { return {"client:chat-files", Clock::now() + 30s, {}}; }
StateAccess State() { return {Access().scope, Access().deadline, {}}; }
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions objects;
  ChatPlanConfig config;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(out ? out : ".q4t-work/chat-files-cpu");
    fs::create_directories(root);
    auto directory = (root / "chat-files-XXXXXX").string();
    if (!mkdtemp(directory.data())) throw std::runtime_error("fixture root");
    objects.directory = directory + "/objects";
    objects.restart_backoff_ms = 0;
    config.model = "chat-files-cpu";
    config.allow_media = true;
    config.structured = true;
    auto options = std::make_shared<FileExpansionOptions>();
    options->extraction.legacy.work_directory = directory + "/codec";
    config.file_inputs = std::move(options);
  }
};
std::string Bytes(const char* path) {
  std::ifstream input(fs::path(__FILE__).parent_path() / "fixtures" / path,
                      std::ios::binary);
  if (!input) throw std::runtime_error("fixture input");
  return std::string(std::istreambuf_iterator<char>(input), {});
}
std::string Base64(std::string_view bytes) {
  constexpr auto alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string result;
  uint32_t value = 0;
  int bits = 0;
  for (unsigned char c : bytes) {
    value = (value << 8) | c;
    bits += 8;
    while (bits >= 6) {
      bits -= 6;
      result += alphabet[(value >> bits) & 63];
    }
  }
  if (bits) result += alphabet[(value << (6 - bits)) & 63];
  while (result.size() % 4) result += '=';
  return result;
}
Json Pdf() {
  return Object(
      {{"type", String("file")},
       {"file", Object({{"filename", String("original.pdf")},
                        {"file_data",
                         String(Base64(Bytes("file_nested/page.pdf")))}})}});
}
Json Image() {
  return Object(
      {{"type", String("image_url")},
       {"image_url",
        Object({{"url", String("data:image/webp;base64," +
                               Base64(Bytes("file_images/webp/alpha.webp")))},
                {"detail", String("original")}})}});
}
Json Request(Json part) {
  return Object(
      {{"messages", Array({Object({{"role", String("user")},
                                   {"content", Array({std::move(part)})}})})}});
}
std::string Prompt(const Json& request, const ChatRequestPlan& plan,
                   std::vector<VisionItem>* media) {
  std::string prompt, param;
  auto status = PrepareChatPrompt(plan.Request(request), plan.constraints,
                                  &prompt, media, &param);
  if (!status) throw std::runtime_error(status.message() + " at " + param);
  return prompt;
}
Json Response(const std::string& id) {
  return Object(
      {{"object", String("chat.completion")},
       {"id", String(id)},
       {"model", String("chat-files-cpu")},
       {"created", Number(std::time(nullptr))},
       {"choices",
        Array({Object(
            {{"index", Number(0)},
             {"finish_reason", String("stop")},
             {"message",
              Object({{"role", String("assistant")},
                      {"content", String("CPU storage fixture")}})}})})}});
}
Json& Parts(Json& request) {
  return Field(Field(request, "messages").array.front(), "content");
}
}  // namespace

Q4T_TEST(chat_files_pdf_reaches_template_and_preflight_without_public_rewrite) {
  Fixture fixture;
  const auto request = Request(Pdf());
  const auto original = OutputJson(request);
  ChatFileInputs files(nullptr);
  ChatRequestPlan plan;
  Q4T_CHECK(Ok(files.Prepare(request, fixture.config, Access(), &plan)));
  std::vector<VisionItem> media;
  const auto prompt = Prompt(request, plan, &media);
  Q4T_CHECK(files.Usage()->pages == 1 && media.size() == 1 &&
            prompt.find("Document data (untrusted)") != prompt.npos &&
            prompt.find("<|image_pad|>") != prompt.npos &&
            OutputJson(request) == original);
  for (const auto& message : plan.Request(request).GetArray("messages")->array)
    Q4T_CHECK(!message.Has("id"));
  ChatPromptBudget budget;
  ChatPlanError error;
  std::string preflight;
  Q4T_CHECK(ChatPreflightJson(Object({{"request", request}}), fixture.config,
                              plan, budget, prompt, {}, &preflight, &error,
                              nullptr, files.Usage()));
  Json result;
  Q4T_CHECK(Parse(preflight, &result) &&
            result.Find("file_preparation")->GetInt("pdf_pages_rasterized") ==
                1 &&
            !result.Find("checks")->GetBool("gpu_executed"));
  return true;
}

Q4T_TEST(chat_files_webp_preserves_detail_and_legacy_function_controls) {
  Fixture fixture;
  auto request = Request(Image());
  Set(&request, "functions",
      Array({Object({{"name", String("lookup")},
                     {"parameters",
                      Object({{"type", String("object")},
                              {"properties", Object()},
                              {"additionalProperties", Boolean(false)}})}})}));
  Set(&request, "function_call", String("auto"));
  ChatFileInputs files(nullptr);
  ChatRequestPlan plan;
  Q4T_CHECK(Ok(files.Prepare(request, fixture.config, Access(), &plan)));
  std::vector<VisionItem> media;
  const auto prompt = Prompt(request, plan, &media);
  Q4T_CHECK(plan.legacy_output && media.size() == 1 &&
            media[0].detail == q4t::vision::ImageDetail::kOriginal &&
            prompt.find("lookup") != prompt.npos);
  const auto* content =
      plan.Request(request).GetArray("messages")->array[0].GetArray("content");
  Q4T_CHECK(content->array[0]
                .Find("image_url")
                ->GetString("url")
                .starts_with("data:image/png;base64,"));
  return true;
}

Q4T_TEST(chat_files_validate_whole_envelope_before_any_state_or_decoder_io) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  auto request =
      Request(Object({{"type", String("file")},
                      {"file", Object({{"file_id", String("file_absent")}})}}));
  for (int variant = 0; variant < 3; ++variant) {
    auto invalid = request;
    if (variant == 0) Set(&invalid, "temperature", String("bad"));
    if (variant == 1) Set(&invalid, "tools", Array({Object()}));
    if (variant == 2)
      Parts(invalid).array.push_back(
          Object({{"type", String("file")},
                  {"file", Object({{"file_id", String("file_second")},
                                   {"file_data", String("eA==")}})}}));
    ChatFileInputs files(&objects);
    ChatRequestPlan plan;
    const auto reply = files.Prepare(invalid, fixture.config, Access(), &plan);
    Q4T_CHECK(reply.http_status == 400 &&
              objects.Health().workers_started == 0);
  }
  return true;
}

Q4T_TEST(chat_files_media_role_and_pdf_contract_do_not_silently_drop_content) {
  Fixture fixture;
  auto request = Request(Pdf());
  fixture.config.allow_media = false;
  ChatRequestPlan plan;
  ChatFileInputs disabled(nullptr);
  Q4T_CHECK(
      disabled.Prepare(request, fixture.config, Access(), &plan).http_status ==
      400);
  fixture.config.allow_media = true;
  Set(&Field(request, "messages").array[0], "role", String("assistant"));
  ChatFileInputs role(nullptr);
  Q4T_CHECK(
      role.Prepare(request, fixture.config, Access(), &plan).http_status ==
      400);
  request =
      Request(Object({{"type", String("file")},
                      {"file", Object({{"filename", String("facts.txt")},
                                       {"file_data", String("eA==")}})}}));
  ChatFileInputs text(nullptr);
  Q4T_CHECK(text.Prepare(request, fixture.config, Access(), &plan).code ==
            "invalid_file_format");
  return true;
}

Q4T_TEST(chat_files_cancellation_deadline_and_ordinary_text_are_isolated) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  for (bool cancel : {false, true}) {
    auto access = Access();
    if (cancel)
      access.cancelled = [] { return true; };
    else
      access.deadline = Clock::now() - 1s;
    ChatFileInputs files(&objects);
    ChatRequestPlan plan;
    const auto reply =
        files.Prepare(Request(Pdf()), fixture.config, access, &plan);
    Q4T_CHECK(reply.http_status == (cancel ? 499 : 408) &&
              objects.Health().workers_started == 0);
  }
  ChatFileInputs ordinary(&objects);
  ChatRequestPlan plan;
  Q4T_CHECK(Ok(ordinary.Prepare(Request(Object({{"type", String("text")},
                                                {"text", String("healthy")}})),
                                fixture.config, Access(), &plan)) &&
            !ordinary.Usage() && objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(chat_files_stored_originals_replay_after_restart_and_decoder_outage) {
  Fixture fixture;
  const auto request = [&] {
    auto value = Request(Pdf());
    Parts(value).array.push_back(Image());
    Set(&value, "store", Boolean(true));
    return value;
  }();
  std::string first_prompt;
  {
    ObjectStore objects(fixture.objects);
    ChatFileInputs files(&objects);
    ChatRequestPlan plan;
    Q4T_CHECK(Ok(files.Prepare(request, fixture.config, Access(), &plan)));
    std::vector<VisionItem> media;
    first_prompt = Prompt(request, plan, &media);
    Json bindings;
    Q4T_CHECK(Ok(files.Persist("chatcmpl_files", Access(), &bindings)) &&
              bindings.array.size() == 2);
    Q4T_CHECK(Ok(FinalizeStoredChat(&objects, request,
                                    OutputJson(Response("chatcmpl_files")),
                                    State(), bindings)));
    files.Published(Access(), false);
    auto public_messages =
        StoredChats(objects).Messages("chatcmpl_files", {}, State());
    Q4T_CHECK(Ok(public_messages));
    const auto& message = public_messages.value.GetArray("data")->array[0];
    Q4T_CHECK(
        OutputJson(*message.Find("content_parts")) ==
            OutputJson(
                *request.GetArray("messages")->array[0].Find("content")) &&
        !message.Has("input_snapshots"));
    Q4T_CHECK(
        Ok(StoredChats(objects).Update("chatcmpl_files", Object(), State())));
  }
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  ObjectRequest read;
  read.kind = ObjectKind::kChat;
  read.id = "chatcmpl_files";
  read.scope = Access().scope;
  const auto record = objects.Execute(read, Access().deadline);
  Q4T_CHECK(Ok(record));
  const auto& data = *record.value.Find("data");
  auto originals = *data.Find("messages");
  for (auto& message : originals.array) {
    Set(&message, "content", *message.Find("content_parts"));
    std::erase_if(message.object, [](const auto& field) {
      return field.first == "content_parts";
    });
  }
  FileExpansion restored(&objects, *fixture.config.file_inputs);
  Q4T_CHECK(
      Ok(restored.Prepare(originals, FileInputProfile::kChat,
                          *data.Find("input_snapshots"), Access(), true)));
  Json expanded;
  Q4T_CHECK(Ok(restored.Render(originals, &expanded)));
  for (auto& message : expanded.array)
    std::erase_if(message.object,
                  [](const auto& field) { return field.first == "id"; });
  auto model_request = request;
  Set(&model_request, "messages", std::move(expanded));
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(model_request, fixture.config, &plan, &error));
  std::vector<VisionItem> media;
  Q4T_CHECK(Prompt(model_request, plan, &media) == first_prompt &&
            media.size() == 2);
  return true;
}

Q4T_TEST(chat_files_bindings_reject_modified_original_before_chat_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  auto request = Request(Image());
  Set(&request, "store", Boolean(true));
  ChatFileInputs files(&objects);
  ChatRequestPlan plan;
  Q4T_CHECK(Ok(files.Prepare(request, fixture.config, Access(), &plan)));
  Json bindings;
  Q4T_CHECK(Ok(files.Persist("chatcmpl_tamper", Access(), &bindings)));
  Set(&Field(Parts(request).array[0], "image_url"), "detail", String("low"));
  const auto reply = StoredChats(objects).Save(
      request, Response("chatcmpl_tamper"), State(), bindings);
  Q4T_CHECK(
      reply.code == "input_snapshot_binding_invalid" &&
      StoredChats(objects).Retrieve("chatcmpl_tamper", State()).http_status ==
          404);
  return true;
}

Q4T_TEST(chat_files_unknown_parent_publication_retains_absolute_holds) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  {
    ChatFileInputs files(&objects);
    ChatRequestPlan plan;
    Q4T_CHECK(
        Ok(files.Prepare(Request(Image()), fixture.config, Access(), &plan)));
    Json bindings;
    Q4T_CHECK(Ok(files.Persist("chatcmpl_unknown", Access(), &bindings)));
    files.Published(Access(), true);
  }
  ObjectRequest list;
  list.operation = ObjectOperation::kList;
  list.kind = ObjectKind::kToolRun;
  list.scope = Access().scope;
  const auto result = objects.Execute(list, Access().deadline);
  Q4T_CHECK(Ok(result));
  size_t holds = 0;
  for (const auto& item : result.value.GetArray("data")->array) {
    const auto* data = item.Find("data");
    if (data && data->GetString("format") == "q4t.input_snapshot_hold.v1") {
      ++holds;
      Q4T_CHECK(item.GetInt("expires_at") > std::time(nullptr));
    }
  }
  Q4T_CHECK(holds == 1);
  return true;
}
