#include "q4t/server/responses_mcp_state.h"

#include <fcntl.h>
#include <poll.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <latch>
#include <thread>

#include "../src/server/object_protocol.h"
#include "../src/server/mcp_provider_common.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/file_expansion.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/moderation.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/responses_mcp_media.h"
#include "q4t/server/responses_owner.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Value(const std::string& text) {
  Json value;
  if (!Parse(text, &value)) throw std::runtime_error("MCP state fixture");
  return value;
}
StateAccess Access(std::string scope = "client:mcp-state") {
  return {std::move(scope), Clock::now() + 8s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "mcp-state-model";
  return config;
}
ResponsesMcpDefinition Definition(std::string approval = "always",
                                  Json patch = Object()) {
  auto declaration = Value(R"({"type":"mcp","server_label":"project",
    "server_url":"https://project.example/mcp",
    "authorization":"fixture-secret-token",
    "headers":{"X-Private":"fixture-secret-header"}})");
  Set(&declaration, "require_approval", String(std::move(approval)));
  for (const auto& [key, field] : patch.object) Set(&declaration, key, field);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(declaration, &definition, &error))
    throw std::runtime_error("MCP definition fixture");
  return definition;
}
Json Tools() {
  return Value(R"([{"name":"read","inputSchema":{"type":"object",
    "properties":{"path":{"type":"string"}},"required":["path"],
    "additionalProperties":false},"outputSchema":{"type":"object",
    "properties":{"text":{"type":"string"}},"required":["text"],
    "additionalProperties":false},"annotations":{"readOnlyHint":true}}])");
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok() || reply.commit_unknown)
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok() && !reply.commit_unknown;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-mcp-state-cpu");
    std::filesystem::create_directories(root);
    auto directory = (root / "mcp-state-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("fixture directory");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
Json Catalog(ResponsesMcpState& state,
             const ResponsesMcpDefinition& definition = Definition(),
             int lifetime = 600) {
  auto reply = state.Catalog(definition, Tools(), Access(), lifetime);
  if (!Ok(reply)) throw std::runtime_error("fixture catalog");
  return reply.value;
}
ObjectReply Propose(ResponsesMcpState& state, const Json& catalog,
                    const ResponsesMcpDefinition& definition = Definition(),
                    Json args = Value(R"({"path":"docs/MODEL.md"})"),
                    std::string caller = "direct") {
  return state.Propose(catalog, definition, "read", std::move(args),
                       std::move(caller), Access());
}
McpTransportReceipt Success(std::string dispatch_id = "") {
  McpTransportReceipt receipt;
  receipt.dispatch_id = std::move(dispatch_id);
  receipt.call_permitted = true;
  receipt.result = Value(R"({"content":[{"type":"text","text":"model info"}],
                            "structuredContent":{"text":"model info"}})");
  return receipt;
}
Json Record(ObjectStore& store, std::string id) {
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.scope = Access().scope;
  request.id = std::move(id);
  auto reply = store.Execute(request, Access().deadline);
  if (!Ok(reply)) throw std::runtime_error("fixture record");
  return reply.value;
}
Json CompletedResourceCall(ResponsesMcpState& state,
                           const ResponsesMcpDefinition& definition,
                           size_t links = 1) {
  const auto proposed = Propose(state, Catalog(state, definition), definition);
  if (!Ok(proposed)) throw std::runtime_error("resource call proposal");
  const auto id = proposed.value.GetString("id");
  const auto permitted = state.Permit(id, definition, Access());
  if (!Ok(permitted)) throw std::runtime_error("resource call permission");
  auto receipt = Success(permitted.value.GetString("dispatch_id"));
  for (size_t i = 0; i < links; ++i)
    Field(receipt.result, "content")
        .array.push_back(
            Object({{"type", String("resource_link")},
                    {"name", String("report")},
                    {"uri", String("file:///report" + std::to_string(i))}}));
  const auto final = state.Finish(id, receipt, {}, Access());
  if (!Ok(final)) throw std::runtime_error("resource call finish");
  return final.value;
}
McpTransportReceipt ResourceResult(std::string dispatch,
                                   std::string text = "remote document") {
  McpTransportReceipt receipt;
  receipt.dispatch_id = std::move(dispatch);
  receipt.call_permitted = true;
  receipt.result = Object(
      {{"contents", Array({Object({{"uri", String("file:///report0")},
                                   {"mimeType", String("text/plain")},
                                   {"text", String(std::move(text))}})})}});
  return receipt;
}
Json CompletedMediaCall(ResponsesMcpState& state, Json content) {
  const auto definition = Definition("never");
  auto proposed = Propose(state, Catalog(state, definition), definition);
  if (!Ok(proposed)) throw std::runtime_error("media call proposal");
  const auto id = proposed.value.GetString("id");
  auto permitted = state.Permit(id, definition, Access());
  if (!Ok(permitted)) throw std::runtime_error("media call permission");
  auto receipt = Success(permitted.value.GetString("dispatch_id"));
  Set(&receipt.result, "content", std::move(content));
  Set(&receipt.result, "_meta",
      Object({{"private_ui", String("hidden-client-data")}}));
  auto final = state.Finish(id, receipt, {}, Access());
  if (!Ok(final)) throw std::runtime_error("media call finish");
  return final.value;
}
Json MediaParts() {
  const std::string png =
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAA"
      "KEAPFkHeRLAAAAAElFTkSuQmCC";
  return Array({Object({{"type", String("image")},
                        {"mimeType", String("image/png")},
                        {"data", String(png)}}),
                Value(R"({"type":"resource","resource":{"uri":"project:note",
                   "mimeType":"text/plain; charset=utf-8","blob":"aGVsbG8="}})")});
}
Json VideoParts() {
  std::ifstream source(std::filesystem::path(__FILE__).parent_path() /
                           "fixtures/file_video/vp8.webm",
                       std::ios::binary);
  const std::string bytes((std::istreambuf_iterator<char>(source)), {});
  if (bytes.empty()) throw std::runtime_error("video fixture missing");
  namespace provider = mcp_detail::provider_detail;
  return Array({provider::Resource("project:clip.webm", "video/webm",
                                   provider::Base64(bytes))});
}
Json MediaItems(const std::string& id, const ObjectReply& projection) {
  return Array({Object(
      {{"id", String(id)}, {"content", *projection.value.Find("content")}})});
}
BinaryAccess MediaAccess() {
  auto access = Access();
  return {access.scope, access.deadline, access.cancelled};
}
}  // namespace

Q4T_TEST(responses_mcp_media_real_files_freeze_and_replay_without_codec) {
  Fixture fixture;
  Json final, rendered, bindings;
  std::string id;
  {
    ObjectStore objects(fixture.options);
    ResponsesMcpState state(objects, Config());
    final = CompletedMediaCall(state, MediaParts());
    id = final.GetString("id");
    auto source = state.ModelContent(id, Access());
    Q4T_CHECK(Ok(source) &&
              source.value.GetArray("unresolved_resources")->array.empty());
    Q4T_CHECK(OutputJson(source.value).find("hidden-client-data") ==
              std::string::npos);
    Q4T_CHECK(final.GetString("output").find("hidden-client-data") !=
              std::string::npos);
    const auto items = MediaItems(id, source);
    FileExpansion prepared(&objects, {});
    Q4T_CHECK(Ok(prepared.Prepare(items, FileInputProfile::kResponsesStored,
                                  Array(), MediaAccess())));
    Q4T_CHECK(!prepared.PendingSheets() && prepared.Usage().files == 2 &&
              prepared.Usage().images == 1 && prepared.Usage().pixels == 1);
    Q4T_CHECK(Ok(prepared.Render(items, &rendered)));
    Q4T_CHECK(OutputJson(rendered).find("hello") != std::string::npos);
    Q4T_CHECK(Ok(prepared.Persist(MediaAccess())));
    bindings = prepared.Bindings();
    Q4T_CHECK(Ok(state.BindModelSnapshots(id, source.value.GetInt("revision"),
                                          bindings, true, Access())));
    Q4T_CHECK(Ok(prepared.Release(MediaAccess())));
    const auto record = Record(objects, id);
    const auto dependencies =
        InspectObjectRetention("tool_run", *record.Find("data"));
    Q4T_CHECK(!dependencies.uncertain);
    Q4T_CHECK(dependencies.references.size() == bindings.array.size());
    for (const auto& binding : bindings.array)
      Q4T_CHECK(std::find(dependencies.references.begin(),
                          dependencies.references.end(),
                          binding.Find("snapshot")->GetString("id")) !=
                dependencies.references.end());
    Q4T_CHECK(OutputJson(*record.Find("data")->Find("input_snapshots")) ==
              OutputJson(bindings));
    Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
              OutputJson(final));
  }
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto source = state.ModelContent(id, Access());
  Q4T_CHECK(Ok(source) && source.value.GetBool("complete"));
  FileExpansion frozen(&objects, {});
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  const auto items = MediaItems(id, source);
  Q4T_CHECK(Ok(frozen.Prepare(items, FileInputProfile::kResponsesStored,
                              *source.value.Find("input_snapshots"),
                              MediaAccess(), true, control)));
  Json replayed;
  Q4T_CHECK(Ok(frozen.Render(items, &replayed)) &&
            control.counters->started == 0);
  Q4T_CHECK(OutputJson(replayed) == OutputJson(rendered));
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_media_requires_all_sources_and_revision_ownership) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto final = CompletedMediaCall(state, MediaParts());
  const auto id = final.GetString("id");
  auto source = state.ModelContent(id, Access());
  Q4T_CHECK(Ok(source));
  Q4T_CHECK(!state
                 .BindModelSnapshots(id, source.value.GetInt("revision"),
                                     Array(), true, Access())
                 .ok());
  FileExpansion prepared(&objects, {});
  Q4T_CHECK(Ok(prepared.Prepare(MediaItems(id, source),
                                FileInputProfile::kResponsesStored, Array(),
                                MediaAccess())));
  Q4T_CHECK(Ok(prepared.Persist(MediaAccess())));
  auto forged = prepared.Bindings();
  Set(&forged.array.front(), "binding_sha256", String(std::string(64, '0')));
  Q4T_CHECK(!state
                 .BindModelSnapshots(id, source.value.GetInt("revision"),
                                     forged, true, Access())
                 .ok());
  Q4T_CHECK(Ok(state.BindModelSnapshots(id, source.value.GetInt("revision"),
                                        prepared.Bindings(), true, Access())));
  Q4T_CHECK(state
                .BindModelSnapshots(id, source.value.GetInt("revision"),
                                    prepared.Bindings(), true, Access())
                .code == "mcp_media_revision_conflict");
  source = state.ModelContent(id, Access());
  Q4T_CHECK(Ok(source));
  Q4T_CHECK(state
                .BindModelSnapshots(id, source.value.GetInt("revision"),
                                    prepared.Bindings(), false, Access())
                .code == "mcp_media_already_final");
  Q4T_CHECK(Ok(state.BindModelSnapshots(id, source.value.GetInt("revision"),
                                        prepared.Bindings(), true, Access())));
  Q4T_CHECK(!state.ModelContent(id, Access("client:other")).ok());
  Q4T_CHECK(Ok(prepared.Release(MediaAccess())));
  return true;
}

Q4T_TEST(responses_mcp_media_reads_frozen_resources_and_retains_failures) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const std::string uri = "file:///report0";
  for (const bool failed : {false, true}) {
    const auto final = CompletedResourceCall(state, definition);
    const auto id = final.GetString("id");
    auto source = state.ModelContent(id, Access());
    Q4T_CHECK(Ok(source) &&
              source.value.GetArray("unresolved_resources")->array.size() == 1);
    Q4T_CHECK(state
                  .BindModelSnapshots(id, source.value.GetInt("revision"),
                                      Array(), true, Access())
                  .code == "mcp_resource_pending");
    Q4T_CHECK(Ok(state.ProposeResource(id, uri, definition, Access())));
    if (failed) {
      Q4T_CHECK(Ok(state.FinishResource(
          id, uri, {},
          Error(502, "fixture_unavailable", "The resource could not be read"),
          Access())));
    } else {
      const auto permitted =
          state.PermitResource(id, uri, definition, Access());
      Q4T_CHECK(Ok(permitted));
      auto receipt = ResourceResult(permitted.value.GetString("dispatch_id"));
      receipt.result = Object(
          {{"contents",
            Array({Object(
                {{"uri", String(uri)},
                 {"mimeType", String("image/png")},
                 {"blob", *MediaParts().array.front().Find("data")}})})}});
      Q4T_CHECK(Ok(state.FinishResource(id, uri, receipt, {}, Access())));
    }
    source = state.ModelContent(id, Access());
    Q4T_CHECK(Ok(source) &&
              source.value.GetArray("unresolved_resources")->array.empty());
    const auto items = MediaItems(id, source);
    FileExpansion prepared(&objects, {});
    Q4T_CHECK(Ok(prepared.Prepare(items, FileInputProfile::kResponsesStored,
                                  Array(), MediaAccess())));
    Q4T_CHECK(prepared.Usage().images == (failed ? 0U : 1U));
    Json rendered;
    Q4T_CHECK(Ok(prepared.Render(items, &rendered)));
    if (failed)
      Q4T_CHECK(OutputJson(rendered).find("The resource could not be read") !=
                std::string::npos);
    Q4T_CHECK(Ok(prepared.Persist(MediaAccess())));
    Q4T_CHECK(
        Ok(state.BindModelSnapshots(id, source.value.GetInt("revision"),
                                    prepared.Bindings(), true, Access())));
    Q4T_CHECK(Ok(prepared.Release(MediaAccess())));
    Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
              OutputJson(final));
  }
  return true;
}

Q4T_TEST(responses_mcp_media_audience_and_audio_do_not_become_fake_text) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  auto parts = MediaParts();
  Set(&parts.array.front(), "annotations", Value(R"({"audience":["user"]})"));
  parts.array.pop_back();
  parts.array.push_back(Value(R"({"type":"audio","mimeType":"audio/wav",
      "data":"UklGRg=="})"));
  const auto final = CompletedMediaCall(state, parts);
  const auto id = final.GetString("id");
  const auto source = state.ModelContent(id, Access());
  Q4T_CHECK(Ok(source) && !source.value.GetBool("complete"));
  size_t audio = 0;
  for (const auto& part : source.value.GetArray("content")->array) {
    Q4T_CHECK(part.GetString("type") != "input_image");
    if (part.GetString("type") != "input_audio") continue;
    ++audio;
    Q4T_CHECK(part.Find("input_audio")->GetString("data") == "UklGRg==" &&
              part.Find("input_audio")->GetString("format") == "wav");
  }
  Q4T_CHECK(audio == 1);
  // A10 still needs an actual audio backend. This tests preserving the pending
  // obligation, not declaring audio delivered through a successful text path.
  Q4T_CHECK(state
                .BindModelSnapshots(id, source.value.GetInt("revision"),
                                    Array(), true, Access())
                .code == "mcp_media_backend_unavailable");
  Q4T_CHECK(!state.ModelContent(id, Access()).value.GetBool("complete"));
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_media_dependency_metadata_must_match_sealed_sources) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto final = CompletedMediaCall(state, MediaParts());
  const auto id = final.GetString("id");
  const auto source = state.ModelContent(id, Access());
  Q4T_CHECK(Ok(source));
  FileExpansion prepared(&objects, {});
  Q4T_CHECK(Ok(prepared.Prepare(MediaItems(id, source),
                                FileInputProfile::kResponsesStored, Array(),
                                MediaAccess())));
  Q4T_CHECK(Ok(prepared.Persist(MediaAccess())));
  Q4T_CHECK(Ok(state.BindModelSnapshots(id, source.value.GetInt("revision"),
                                        prepared.Bindings(), true, Access())));
  const auto record = Record(objects, id);
  ObjectRequest tamper;
  tamper.operation = ObjectOperation::kPut;
  tamper.kind = ObjectKind::kToolRun;
  tamper.scope = Access().scope;
  tamper.id = id;
  tamper.expected_revision = record.GetInt("revision");
  tamper.expires_at = record.GetInt("expires_at");
  tamper.data = *record.Find("data");
  Set(&tamper.data, "input_snapshots", Array());
  Q4T_CHECK(Ok(objects.Execute(tamper, Access().deadline)));
  const auto read = state.ModelContent(id, Access());
  Q4T_CHECK(read.code == "invalid_mcp_state" &&
            read.message.find("dependencies") != std::string::npos);
  Q4T_CHECK(!state.Authenticate(final, Access()).ok());
  Q4T_CHECK(Ok(prepared.Release(MediaAccess())));
  return true;
}

Q4T_TEST(responses_mcp_shared_media_model_and_review_use_actual_image) {
  Fixture f;
  Json final, rendered;
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    final = CompletedMediaCall(state, MediaParts());
    ResponsesMcpMedia media(objects);
    Q4T_CHECK(
        Ok(media.Prepare(state, Array({final}), {}, {}, Access(), false)));
    Q4T_CHECK(!media.PendingSheets() && media.Budget().usage.files == 2 &&
              media.Budget().usage.images == 1);
    Q4T_CHECK(Ok(media.Persist(state, Access())) &&
              Ok(media.Render(&rendered)));
    const auto id = final.GetString("id");
    Q4T_CHECK(state.ModelContent(id, Access()).value.GetBool("complete"));
    ResponsesInputPlan input;
    ChatPlanError error;
    Q4T_CHECK(NormalizeResponsesInput(Object({{"input", Array({final})}}),
                                      &input, &error, false, false, false,
                                      &rendered));
    Q4T_CHECK(input.messages.array.size() == 2);
    const auto& content = *input.messages.array.back().GetArray("content");
    Q4T_CHECK(std::count_if(content.array.begin(), content.array.end(),
                            [](const auto& part) {
                              return part.GetString("type") == "image_url";
                            }) == 1);
    Q4T_CHECK(OutputJson(content).find("hello") != std::string::npos);
    Q4T_CHECK(OutputJson(content).find("hidden-client-data") ==
              std::string::npos);
    ModerationContent in, out;
    Q4T_CHECK(CollectModerationInput(input.messages, &in, &error));
    Q4T_CHECK(CollectModerationOutput(Array({final}), &out, &error, &rendered));
    Q4T_CHECK(in.images.array.size() == 1 && out.images.array.size() == 1 &&
              OutputJson(in.images) == OutputJson(out.images));
    Q4T_CHECK(OutputJson(out.records).find("hello") != std::string::npos &&
              OutputJson(out.records).find("iVBOR") == std::string::npos);
    Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
              OutputJson(final));
  }
  ObjectStore restarted(f.options);
  ResponsesMcpState state(restarted, Config());
  ResponsesMcpMedia replay(restarted);
  Q4T_CHECK(Ok(replay.Prepare(state, Array({final}), {}, {}, Access(), true)));
  Q4T_CHECK(Ok(replay.Persist(state, Access())));
  Json again;
  Q4T_CHECK(Ok(replay.Render(&again)) &&
            OutputJson(again) == OutputJson(rendered));
  return true;
}

Q4T_TEST(responses_mcp_shared_media_budget_cannot_refill_between_calls) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto first = CompletedMediaCall(state, MediaParts());
  const auto second = CompletedMediaCall(state, MediaParts());
  const auto text = CompletedMediaCall(
      state, Array({Value(R"({"type":"text","text":"after image quota"})")}));
  FileExpansionOptions limits;
  limits.max_files = 2;
  limits.extraction.pdf.max_pixels = 1;
  ResponsesMcpMedia media(objects);
  Q4T_CHECK(
      Ok(media.Prepare(state, Array({first}), limits, {}, Access(), false)));
  Q4T_CHECK(Ok(media.Persist(state, Access())));
  const auto before = media.Budget();
  Q4T_CHECK(Ok(
      media.Prepare(state, Array({first, text}), limits, {}, Access(), false)));
  Q4T_CHECK(media.Budget().usage.files == before.usage.files &&
            media.Budget().usage.pixels == before.usage.pixels);
  Q4T_CHECK(
      !media.Prepare(state, Array({second}), limits, {}, Access(), false).ok());
  Q4T_CHECK(media.Budget().usage.files == 2 &&
            !state.ModelContent(second.GetString("id"), Access())
                 .value.GetBool("complete"));
  ResponsesMcpMedia mixed(objects);
  ResponsesMediaBudget other;
  other.usage.files = 1;
  Q4T_CHECK(
      !mixed.Prepare(state, Array({first}), limits, other, Access(), false)
           .ok());
  Q4T_CHECK(mixed.Budget().usage.files == 0);
  return true;
}

Q4T_TEST(
    responses_mcp_video_model_review_and_restart_use_same_temporal_sample) {
  Fixture f;
  Json final, rendered;
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    final = CompletedMediaCall(state, VideoParts());
    ResponsesMcpMedia media(objects);
    Q4T_CHECK(
        Ok(media.Prepare(state, Array({final}), {}, {}, Access(), false)));
    Q4T_CHECK(media.Budget().usage.videos == 1 &&
              media.Budget().usage.video_frames == 6 &&
              media.Budget().usage.video_scanned_frames == 6);
    Q4T_CHECK(Ok(media.Persist(state, Access())) &&
              Ok(media.Render(&rendered)));
    ResponsesInputPlan input;
    ChatPlanError error;
    Q4T_CHECK(NormalizeResponsesInput(Object({{"input", Array({final})}}),
                                      &input, &error, false, false, false,
                                      &rendered));
    ModerationContent in, out;
    Q4T_CHECK(CollectModerationInput(input.messages, &in, &error) &&
              CollectModerationOutput(Array({final}), &out, &error, &rendered));
    Q4T_CHECK(in.images.array.size() == 1 &&
              in.images.array[0].GetString("type") == "video_frames" &&
              OutputJson(in.images) == OutputJson(out.images));
    const auto request = Object({{"messages", input.messages}});
    std::string prompt;
    std::vector<VisionItem> generated, reviewed;
    Q4T_CHECK(PrepareChatPrompt(request, {}, &prompt, &generated, &error.param,
                                &error.code, nullptr, false, {}, true));
    auto config = Config();
    config.allow_media = config.structured = true;
    ResponsesEnvelope envelope;
    ResponsesGenerationPlan generation;
    const auto native_request = Object({{"input", Array({final})}});
    Q4T_CHECK(ParseResponsesEnvelope(native_request, config, &envelope,
                                      &error));
    envelope.mcp_content = rendered;
    Q4T_CHECK(PrepareResponsesGeneration(
        envelope, *native_request.Find("input"), config, &generation, &error));
    Q4T_CHECK(generation.chat.prepared_source_data);
    ModerationPlan plan;
    Q4T_CHECK(
        PlanModeration({true, config.model}, out, config, 512, &plan, &error));
    Q4T_CHECK(PrepareChatPrompt(plan.request, {}, &prompt, &reviewed,
                                &error.param, &error.code));
    Q4T_CHECK(generated.size() == 1 && reviewed.size() == 1 &&
              generated[0].kind == VisionItem::kVideo &&
              generated[0].frames == reviewed[0].frames);
    const auto& review_parts =
        *plan.request.GetArray("messages")->array.back().GetArray("content");
    Q4T_CHECK(review_parts.array.size() == out.images.array.size() + 2 &&
              review_parts.array.front().GetString("type") == "text" &&
              review_parts.array.back().GetString("type") == "text");
    for (size_t i = 0; i < out.images.array.size(); ++i)
      Q4T_CHECK(OutputJson(review_parts.array[i + 1]) ==
                OutputJson(out.images.array[i]));
  }
  detail::SetVideoWorkerEntryForTesting("--file-video-missing-worker");
  ScopeExit reset([] { detail::SetVideoWorkerEntryForTesting(""); });
  ObjectStore restarted(f.options);
  ResponsesMcpState state(restarted, Config());
  ResponsesMcpMedia replay(restarted);
  Q4T_CHECK(Ok(replay.Prepare(state, Array({final}), {}, {}, Access(), true)));
  Json again;
  Q4T_CHECK(Ok(replay.Render(&again)) &&
            OutputJson(again) == OutputJson(rendered));
  return true;
}

Q4T_TEST(responses_mcp_video_work_budget_is_zero_after_cross_call_exhaustion) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto first = CompletedMediaCall(state, VideoParts());
  const auto second = CompletedMediaCall(state, VideoParts());
  const auto text = CompletedMediaCall(
      state, Array({Value(R"({"type":"text","text":"after video quota"})")}));
  FileExpansionOptions limits;
  limits.extraction.video.max_scanned_frames = 6;
  ResponsesMcpMedia media(objects);
  Q4T_CHECK(
      Ok(media.Prepare(state, Array({first}), limits, {}, Access(), false)));
  Q4T_CHECK(Ok(media.Persist(state, Access())));
  detail::SetVideoWorkerEntryForTesting("--file-video-missing-worker");
  ScopeExit reset([] { detail::SetVideoWorkerEntryForTesting(""); });
  // No codec can succeed here. Exhausted work must fail before starting one,
  // while an unrelated text tool result can still proceed.
  Q4T_CHECK(
      Ok(media.Prepare(state, Array({text}), limits, {}, Access(), false)));
  Q4T_CHECK(media.Prepare(state, Array({second}), limits, {}, Access(), false)
                .http_status == 413);
  Q4T_CHECK(media.Budget().usage.video_scanned_frames == 6 &&
            !state.ModelContent(second.GetString("id"), Access())
                 .value.GetBool("complete"));
  ResponsesMcpMedia other(objects);
  Q4T_CHECK(other.Prepare(state, Array({second}), limits, media.Budget(),
                          Access(), false)
                    .http_status == 413 &&
            other.Budget().usage.files == 0);
  return true;
}

Q4T_TEST(responses_mcp_shared_media_resource_unknown_is_frozen_not_replayed) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config(), "resp_mcp_media");
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(state, definition);
  int resolved = 0;
  const auto resolve = [&](const std::string& id, const std::string& uri) {
    ++resolved;
    auto result = state.ProposeResource(id, uri, definition, Access());
    if (!result.ok()) return result;
    result = state.PermitResource(id, uri, definition, Access());
    if (!result.ok()) return result;
    McpTransportReceipt receipt;
    receipt.dispatch_id = result.value.GetString("dispatch_id");
    receipt.call_permitted = true;
    receipt.outcome_unknown = true;
    return state.FinishResource(id, uri, receipt,
                                Error(502, "mcp_worker_failed", "lost receipt"),
                                Access());
  };
  ResponsesMcpMedia media(objects);
  Q4T_CHECK(Ok(media.Prepare(state, Array({final}), {}, {}, Access(), false,
                             resolve)) &&
            resolved == 1);
  Json content;
  Q4T_CHECK(Ok(media.Render(&content)) &&
            OutputJson(content).find("unknown") != std::string::npos);
  ResponsesMcpMedia replay(objects);
  Q4T_CHECK(Ok(replay.Prepare(state, Array({final}), {}, {}, Access(), true,
                              resolve)) &&
            resolved == 1);
  Json again;
  Q4T_CHECK(Ok(replay.Render(&again)) &&
            OutputJson(content) == OutputJson(again));
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_shared_media_needs_owner_and_authentic_original) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto linked = CompletedResourceCall(state, Definition("never"));
  ResponsesMcpMedia pending(objects);
  int calls = 0;
  const auto forbidden = [&](const auto&, const auto&) {
    ++calls;
    return ObjectReply{};
  };
  Q4T_CHECK(
      pending.Prepare(state, Array({linked}), {}, {}, Access(), true, forbidden)
              .code == "mcp_resource_preparation_required" &&
      calls == 0);
  const auto final = CompletedMediaCall(state, MediaParts());
  ResponsesMcpMedia frozen(objects);
  Q4T_CHECK(
      !frozen.Prepare(state, Array({final}), {}, {}, Access(), true).ok());
  auto forged = final;
  Set(&forged, "output", String("forged native content"));
  Q4T_CHECK(
      !frozen.Prepare(state, Array({forged}), {}, {}, Access(), false).ok());
  Q4T_CHECK(!frozen
                 .Prepare(state, Array({final}), {}, {}, Access("client:other"),
                          false)
                 .ok());
  Q4T_CHECK(frozen.Budget().usage.files == 0);
  return true;
}

Q4T_TEST(responses_mcp_shared_media_sheet_steps_freeze_and_resume_exactly) {
  Fixture f;
  Json calls, expected;
  FileExpansionOptions limits;
  limits.summary_output_tokens = 64;
  limits.total_summary_output_tokens = 128;
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto csv = Array({Value(R"({"type":"resource","resource":{
      "uri":"project:sheet","mimeType":"text/csv",
      "blob":"bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK"}})")});
    calls =
        Array({CompletedMediaCall(state, csv), CompletedMediaCall(state, csv)});
    ResponsesMcpMedia media(objects);
    Q4T_CHECK(Ok(media.Prepare(state, calls, limits, {}, Access(), false)));
    Q4T_CHECK(media.PendingSheets() && media.PendingOutputBudget() == 128 &&
              media.Budget().usage.model_steps == 0);
    Q4T_CHECK(Ok(media.Persist(state, Access())));
    Q4T_CHECK(!media.Render(&expected).ok());
  }
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    ResponsesMcpMedia media(objects);
    Q4T_CHECK(Ok(media.Prepare(state, calls, limits, {}, Access(), true)));
    auto config = Config();
    config.structured = true;
    for (int i = 0; i < 2; ++i) {
      FileSheetStep step;
      Q4T_CHECK(Ok(media.NextSheet(config, &step)));
      auto forged = step;
      forged.prompt_sha256 = std::string(64, '0');
      Q4T_CHECK(!media.CompleteSheet(forged, "{}", "stop", 100, 12).ok());
      Q4T_CHECK(Ok(media.CompleteSheet(
          step,
          R"({"summary":"CPU receipt: alpha 42, beta 7",)"
          R"("headers":["name: row label","value: quantity"]})",
          "stop", 100, 12)));
      Q4T_CHECK(Ok(media.Persist(state, Access())));
    }
    Q4T_CHECK(!media.PendingSheets() && media.PendingOutputBudget() == 0 &&
              media.Budget().usage.model_steps == 2 &&
              media.Budget().usage.model_output_tokens == 24);
    Q4T_CHECK(Ok(media.Render(&expected)));
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  ResponsesMcpMedia media(objects);
  Q4T_CHECK(Ok(media.Prepare(state, calls, limits, {}, Access(), true)));
  Q4T_CHECK(!media.PendingSheets() && media.Budget().usage.model_steps == 0);
  Json actual;
  Q4T_CHECK(Ok(media.Render(&actual)) &&
            OutputJson(actual) == OutputJson(expected));
  return true;
}

Q4T_TEST(responses_mcp_shared_media_sheet_reservation_cannot_refill) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto csv = Array({Value(R"({"type":"resource","resource":{
    "uri":"project:sheet","mimeType":"text/csv",
    "blob":"bmFtZSx2YWx1ZQphbHBoYSw0MgpiZXRhLDcK"}})")});
  const auto calls =
      Array({CompletedMediaCall(state, csv), CompletedMediaCall(state, csv)});
  FileExpansionOptions limits;
  limits.summary_output_tokens = 64;
  limits.total_summary_output_tokens = 64;
  ResponsesMcpMedia media(objects);
  Q4T_CHECK(media.Prepare(state, calls, limits, {}, Access(), false).code ==
            "file_input_capacity_exceeded");
  Q4T_CHECK(media.Budget().usage.files == 1 &&
            media.PendingOutputBudget() == 64);
  ResponsesMcpMedia mixed(objects);
  ResponsesMediaBudget other;
  other.usage.model_output_tokens = 1;
  Q4T_CHECK(mixed
                .Prepare(state, Array({calls.array[0]}), limits, other,
                         Access(), false)
                .code == "file_input_capacity_exceeded");
  return true;
}

Q4T_TEST(responses_mcp_resource_requires_original_link_scope_and_policy) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(state, definition);
  const auto id = final.GetString("id");
  const std::string uri = "file:///report0";
  Q4T_CHECK(state.ProposeResource(id, "file:///different", definition, Access())
                .code == "mcp_resource_denied");
  Q4T_CHECK(
      !state.ProposeResource(id, uri, definition, Access("client:other")).ok());
  auto other_config = Config();
  other_config.model = "another-model";
  ResponsesMcpState other(objects, other_config);
  Q4T_CHECK(!other.ProposeResource(id, uri, definition, Access()).ok());
  auto changed = definition;
  Set(&changed.declaration, "server_url",
      String("https://different.example/mcp"));
  Q4T_CHECK(state.ProposeResource(id, uri, changed, Access()).code ==
            "mcp_server_changed");
  changed = definition;
  changed.credentials.authorization.clear();
  changed.credentials.headers.clear();
  Q4T_CHECK(state.ProposeResource(id, uri, changed, Access()).code ==
            "mcp_credentials_required");
  changed = definition;
  Set(&changed.declaration, "allowed_tools", Array({String("different")}));
  Q4T_CHECK(state.ProposeResource(id, uri, changed, Access()).code ==
            "mcp_tool_denied");
  Q4T_CHECK(
      state.ProposeResource(id, uri, Definition("always"), Access()).code ==
      "mcp_approval_required");
  Q4T_CHECK(state.Resource(id, uri, Access()).http_status == 404);
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_resource_single_dispatch_freezes_result_across_restart) {
  Fixture f;
  Json final, frozen;
  std::string id;
  const auto definition = Definition("never");
  const std::string uri = "file:///report0";
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config(), "resp_resource_owner");
    final = CompletedResourceCall(state, definition);
    id = final.GetString("id");
    Q4T_CHECK(Ok(state.ProposeResource(id, uri, definition, Access())));
    Q4T_CHECK(
        !state.FinishResource(id, uri, ResourceResult(""), {}, Access()).ok());
    const auto permitted = state.PermitResource(id, uri, definition, Access());
    Q4T_CHECK(Ok(permitted));
    Q4T_CHECK(state.PermitResource(id, uri, definition, Access()).http_status ==
              409);
    auto receipt = ResourceResult("wrong-owner");
    Q4T_CHECK(state.FinishResource(id, uri, receipt, {}, Access()).code ==
              "mcp_resource_owner_conflict");
    receipt.dispatch_id = permitted.value.GetString("dispatch_id");
    const auto completed = state.FinishResource(id, uri, receipt, {}, Access());
    Q4T_CHECK(Ok(completed) &&
              completed.value.GetString("state") == "completed");
    Q4T_CHECK(OutputJson(*completed.value.Find("result")) ==
              OutputJson(receipt.result));
    frozen = completed.value;
    Q4T_CHECK(!frozen.Has("dispatch_id") && !frozen.Has("execution_owner"));
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto reopened = state.Resource(id, uri, Access());
  Q4T_CHECK(Ok(reopened) && OutputJson(reopened.value) == OutputJson(frozen));
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  Q4T_CHECK(state.ProposeResource(id, uri, definition, Access()).http_status ==
            409);
  Q4T_CHECK(state.PermitResource(id, uri, definition, Access()).http_status ==
            409);
  Q4T_CHECK(!state.Resource(id, uri, Access("client:other")).ok());
  Q4T_CHECK(OutputJson(Record(objects, id)).find("remote document") ==
            std::string::npos);
  return true;
}

Q4T_TEST(responses_mcp_resource_failures_preserve_known_tool_and_never_replay) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const std::string uri = "file:///report0";
  for (const int variant : {0, 1, 2, 3}) {
    const auto final = CompletedResourceCall(state, definition);
    const auto id = final.GetString("id");
    Q4T_CHECK(Ok(state.ProposeResource(id, uri, definition, Access())));
    McpTransportReceipt receipt;
    ObjectReply transport;
    if (!variant) {
      transport =
          Error(502, "mcp_resources_unavailable", "No resource capability");
    } else {
      const auto permitted =
          state.PermitResource(id, uri, definition, Access());
      Q4T_CHECK(Ok(permitted));
      receipt = ResourceResult(permitted.value.GetString("dispatch_id"));
      if (variant == 1) {
        receipt.outcome_unknown = true;
        transport = Error(502, "mcp_transport_error", "Connection lost");
      } else if (variant == 2) {
        receipt.result = Object({{"contents", Array({Object()})}});
      } else {
        receipt.error = Value(
            R"({"code":-32602,"message":"No resource","data":{"uri":"file:///report0"}})");
      }
    }
    const auto completed =
        state.FinishResource(id, uri, receipt, transport, Access());
    Q4T_CHECK(Ok(completed));
    Q4T_CHECK(completed.value.GetString("state") ==
              (variant == 1 ? "unknown" : "failed"));
    Q4T_CHECK(completed.value.Find("result")->IsNull());
    Q4T_CHECK(
        !state.FinishResource(id, uri, receipt, transport, Access()).ok());
    Q4T_CHECK(!state.PermitResource(id, uri, definition, Access()).ok());
    Q4T_CHECK(!state.ProposeResource(id, uri, definition, Access()).ok());
    Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
              OutputJson(final));
  }
  return true;
}

Q4T_TEST(responses_mcp_resource_cleanup_uses_its_own_execution_identity) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState original(objects, Config(), "resp_original_tool");
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(original, definition, 3);
  const auto id = final.GetString("id");
  ResponsesMcpState reader(objects, Config(), "resp_resource_owner");
  for (int i = 0; i < 3; ++i) {
    const auto uri = "file:///report" + std::to_string(i);
    Q4T_CHECK(Ok(reader.ProposeResource(id, uri, definition, Access())));
    if (!i) continue;
    const auto permitted = reader.PermitResource(id, uri, definition, Access());
    Q4T_CHECK(Ok(permitted));
    if (i == 2)
      Q4T_CHECK(Ok(reader.FinishResource(
          id, uri, ResourceResult(permitted.value.GetString("dispatch_id")), {},
          Access())));
  }
  ResponseStatePlan ended;
  ended.id = "resp_original_tool";
  ended.scope = Access().scope;
  ended.execution_owner = CurrentResponseOwner();
  Q4T_CHECK(Ok(original.Recover(id, Access(), &ended)));
  Q4T_CHECK(reader.Resource(id, "file:///report0", Access())
                .value.GetString("state") == "ready");
  Q4T_CHECK(reader.Resource(id, "file:///report1", Access())
                .value.GetString("state") == "dispatching");
  ended.id = "resp_resource_owner";
  auto items = Array({final});
  Q4T_CHECK(Ok(reader.Reconcile(&items, ended, Access())));
  Q4T_CHECK(OutputJson(items.array.front()) == OutputJson(final));
  Q4T_CHECK(reader.Resource(id, "file:///report0", Access())
                .value.GetString("state") == "failed");
  Q4T_CHECK(reader.Resource(id, "file:///report1", Access())
                .value.GetString("state") == "unknown");
  Q4T_CHECK(reader.Resource(id, "file:///report2", Access())
                .value.GetString("state") == "completed");
  Q4T_CHECK(
      !reader.PermitResource(id, "file:///report1", definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_resource_capacity_cannot_erase_tool_or_frozen_sibling) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(state, definition, 2);
  const auto id = final.GetString("id");
  Json first;
  for (int i = 0; i < 2; ++i) {
    const auto uri = "file:///report" + std::to_string(i);
    Q4T_CHECK(Ok(state.ProposeResource(id, uri, definition, Access())));
    const auto permitted = state.PermitResource(id, uri, definition, Access());
    Q4T_CHECK(Ok(permitted));
    const auto receipt =
        ResourceResult(permitted.value.GetString("dispatch_id"),
                       std::string((i ? 2 : 3) << 20, 'x'));
    const auto completed = state.FinishResource(id, uri, receipt, {}, Access());
    Q4T_CHECK(Ok(completed));
    if (!i) {
      Q4T_CHECK(completed.value.GetString("state") == "completed");
      first = completed.value;
    } else {
      Q4T_CHECK(completed.value.GetString("state") == "failed");
      Q4T_CHECK(completed.value.Find("error")->GetString("code") ==
                "mcp_resource_capacity");
      Q4T_CHECK(completed.value.Find("result")->IsNull());
    }
  }
  Q4T_CHECK(OutputJson(state.Resource(id, "file:///report0", Access()).value) ==
            OutputJson(first));
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_resource_concurrent_permission_has_one_winner) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(state, definition);
  const auto id = final.GetString("id");
  const std::string uri = "file:///report0";
  Q4T_CHECK(Ok(state.ProposeResource(id, uri, definition, Access())));
  std::latch ready(2), go(1);
  ObjectReply results[2];
  auto worker = [&](size_t i) {
    ResponsesMcpState owned(objects, Config());
    ready.count_down();
    go.wait();
    results[i] = owned.PermitResource(id, uri, definition, Access());
  };
  std::thread first(worker, 0), second(worker, 1);
  ready.wait();
  go.count_down();
  first.join();
  second.join();
  Q4T_CHECK(int(results[0].ok()) + int(results[1].ok()) == 1);
  const auto& won = results[results[0].ok() ? 0 : 1];
  Q4T_CHECK(Ok(state.FinishResource(
      id, uri, ResourceResult(won.value.GetString("dispatch_id")), {},
      Access())));
  Q4T_CHECK(!state.PermitResource(id, uri, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_resource_reserves_space_to_close_pending_siblings) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(state, definition, 2);
  const auto id = final.GetString("id");
  for (const auto* uri : {"file:///report0", "file:///report1"})
    Q4T_CHECK(Ok(state.ProposeResource(id, uri, definition, Access())));
  const auto permitted =
      state.PermitResource(id, "file:///report0", definition, Access());
  Q4T_CHECK(Ok(permitted));
  auto receipt = ResourceResult(permitted.value.GetString("dispatch_id"), "");
  auto candidate = state.Inspect(id, Access());
  Q4T_CHECK(Ok(candidate));
  auto& entry = Field(candidate.value, "resources").array.front();
  Set(&entry, "state", String("completed"));
  Set(&entry, "result", receipt.result);
  const auto bytes = (4U << 20) - OutputJson(candidate.value).size() - 512;
  receipt = ResourceResult(receipt.dispatch_id, std::string(bytes, 'x'));
  const auto completed =
      state.FinishResource(id, "file:///report0", receipt, {}, Access());
  Q4T_CHECK(Ok(completed) && completed.value.GetString("state") == "failed");
  Q4T_CHECK(completed.value.Find("error")->GetString("code") ==
            "mcp_resource_capacity");
  Q4T_CHECK(Ok(state.FinishResource(
      id, "file:///report1", {},
      Error(502, "fixture_unavailable", "Resource unavailable"), Access())));
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_resource_count_bound_keeps_existing_preparations) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto final = CompletedResourceCall(state, definition, 33);
  const auto id = final.GetString("id");
  for (int i = 0; i < 32; ++i)
    Q4T_CHECK(Ok(state.ProposeResource(id, "file:///report" + std::to_string(i),
                                       definition, Access())));
  Q4T_CHECK(state.ProposeResource(id, "file:///report32", definition, Access())
                .code == "mcp_resource_capacity");
  Q4T_CHECK(state.Resource(id, "file:///report32", Access()).http_status ==
            404);
  Q4T_CHECK(state.Resource(id, "file:///report31", Access())
                .value.GetString("state") == "ready");
  Q4T_CHECK(OutputJson(state.Authenticate(final, Access()).value) ==
            OutputJson(final));
  return true;
}

Q4T_TEST(responses_mcp_recovery_requires_real_exit_and_never_replays) {
  for (const int phase : {0, 1, 2}) {
    Fixture fixture;
    int channel[2];
    Q4T_CHECK(pipe2(channel, O_CLOEXEC) == 0);
    const auto child = fork();
    Q4T_CHECK(child >= 0);
    if (child == 0) {
      close(channel[0]);
      try {
        std::string id;
        {
          ObjectStore objects(fixture.options);
          ResponsesMcpState state(objects, Config());
          const auto definition = Definition("never");
          auto proposed =
              Propose(state, Catalog(state, definition), definition);
          if (!Ok(proposed)) _exit(2);
          id = proposed.value.GetString("id");
          if (phase) {
            auto permit = state.Permit(id, definition, Access());
            if (!Ok(permit)) _exit(3);
            if (phase == 2 &&
                !Ok(state.Finish(id,
                                 Success(permit.value.GetString("dispatch_id")),
                                 {}, Access())))
              _exit(4);
          }
        }
        if (write(channel[1], id.data(), id.size()) !=
            static_cast<ssize_t>(id.size()))
          _exit(5);
        for (;;) pause();
      } catch (...) {
        _exit(6);
      }
    }
    close(channel[1]);
    bool reaped = false;
    ScopeExit cleanup([&] {
      close(channel[0]);
      if (!reaped) {
        kill(child, SIGKILL);
        int status;
        while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
        }
      }
    });
    pollfd ready{channel[0], POLLIN, 0};
    Q4T_CHECK(poll(&ready, 1, 5000) > 0);
    char buffer[256];
    const auto size = read(channel[0], buffer, sizeof(buffer));
    Q4T_CHECK(size > 0);
    const std::string id(buffer, size);
    ObjectStore objects(fixture.options);
    ResponsesMcpState state(objects, Config());
    const auto original = state.Inspect(id, Access());
    Q4T_CHECK(Ok(original));
    Q4T_CHECK(ResponseOwnerStatus(*original.value.Find("execution_owner")) ==
              ResponseOwnerState::kAlive);
    if (phase != 2) Q4T_CHECK(state.Recover(id, Access()).http_status == 409);
    Q4T_CHECK(kill(child, SIGTERM) == 0);
    int status = 0;
    Q4T_CHECK(waitpid(child, &status, 0) == child);
    reaped = true;
    Q4T_CHECK(WIFSIGNALED(status) && WTERMSIG(status) == SIGTERM);
    const auto recovered = state.Recover(id, Access());
    Q4T_CHECK(Ok(recovered));
    if (phase == 2)
      Q4T_CHECK(OutputJson(recovered.value) ==
                OutputJson(*original.value.Find("terminal")));
    else {
      const auto code = recovered.value.Find("error")->Find("content");
      Q4T_CHECK(code->GetString("code") ==
                (phase ? "mcp_result_unknown" : "mcp_execution_interrupted"));
      Q4T_CHECK(state.Permit(id, Definition("never"), Access()).http_status ==
                409);
    }
    Q4T_CHECK(OutputJson(state.Recover(id, Access()).value) ==
              OutputJson(recovered.value));
  }
  return true;
}

Q4T_TEST(responses_mcp_recovery_requires_exact_response_cleanup_proof) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config(), "resp_mcp_owner");
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  for (const bool dispatched : {false, true}) {
    const auto proposal = Propose(state, catalog, definition);
    Q4T_CHECK(Ok(proposal));
    const auto id = proposal.value.GetString("id");
    if (dispatched) Q4T_CHECK(Ok(state.Permit(id, definition, Access())));
    ResponseStatePlan ended;
    ended.id = "resp_mcp_other";
    ended.scope = Access().scope;
    ended.execution_owner = CurrentResponseOwner();
    Q4T_CHECK(state.Recover(id, Access(), &ended).http_status == 409);
    ended.id = "resp_mcp_owner";
    ended.scope = "client:other";
    Q4T_CHECK(state.Recover(id, Access(), &ended).http_status == 409);
    ended.scope = Access().scope;
    Set(&ended.execution_owner, "pid_namespace",
        Number(ended.execution_owner.GetInt("pid_namespace") + 1));
    Q4T_CHECK(state.Recover(id, Access(), &ended).http_status == 409);
    ended.execution_owner = CurrentResponseOwner();
    Q4T_CHECK(Ok(state.Recover(id, Access(), &ended)));
    auto fragment = proposal.value;
    Set(&fragment, "status", String("incomplete"));
    auto restored = state.Authenticate(fragment, Access());
    Q4T_CHECK(Ok(restored) && restored.value.GetString("status") == "failed");
    Set(&fragment, "arguments", String("{}"));
    Q4T_CHECK(!state.Authenticate(fragment, Access()).ok());
  }
  const auto approval = Propose(state, Catalog(state));
  Q4T_CHECK(Ok(approval));
  Q4T_CHECK(
      state.Recover(approval.value.GetString("id"), Access()).http_status ==
      409);
  Q4T_CHECK(Ok(state.Authenticate(approval.value, Access())));
  return true;
}

Q4T_TEST(responses_mcp_recovery_unknown_identity_is_not_death) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto proposal = Propose(state, Catalog(state, definition), definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  Q4T_CHECK(Ok(state.Permit(id, definition, Access())));
  auto record = Record(objects, id);
  StateCipher cipher(objects);
  const StateCipherBinding binding{
      Config().model,
      Digest(std::string(identity::kTemplateReference) + ':' +
             identity::kRenderer),
      "responses.mcp.call.v1", id};
  std::string plain;
  Q4T_CHECK(Ok(cipher.Open(record.Find("data")->GetString("sealed"), binding,
                           Access(), &plain)));
  auto payload = Value(plain);
  for (const bool missing : {false, true}) {
    auto owner = CurrentResponseOwner();
    Set(&owner, "pid_namespace", Number(owner.GetInt("pid_namespace") + 1));
    Set(&payload, "execution_owner", missing ? Json() : owner);
    std::string sealed;
    Q4T_CHECK(Ok(cipher.Seal(OutputJson(payload), binding, Access(), &sealed)));
    ObjectRequest write;
    write.operation = ObjectOperation::kPut;
    write.kind = ObjectKind::kToolRun;
    write.scope = Access().scope;
    write.id = id;
    write.expected_revision = record.GetInt("revision");
    write.expires_at = write.mutate_before = record.GetInt("expires_at");
    write.data = *record.Find("data");
    Set(&write.data, "sealed", String(sealed));
    Q4T_CHECK(Ok(objects.Execute(write, Access().deadline)));
    record = Record(objects, id);
    Q4T_CHECK(state.Recover(id, Access()).http_status == 409);
    Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
              "dispatching");
  }
  return true;
}

Q4T_TEST(responses_mcp_unproposed_fragment_grants_no_result_or_permission) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  auto fragment = Value(R"({"id":"mcp_never_proposed","type":"mcp_call",
    "name":"read","server_label":"project","arguments":"{\"path\":",
    "status":"incomplete","output":null,"error":null})");
  Q4T_CHECK(Ok(state.Authenticate(fragment, Access())));
  Q4T_CHECK(
      state.Permit("mcp_never_proposed", Definition(), Access()).http_status ==
      404);
  Set(&fragment, "output", String("forged output"));
  Q4T_CHECK(!state.Authenticate(fragment, Access()).ok());
  Set(&fragment, "output", Json());
  Set(&fragment, "status", String("completed"));
  Q4T_CHECK(!state.Authenticate(fragment, Access()).ok());
  return true;
}

Q4T_TEST(
    responses_mcp_credentials_must_be_resupplied_and_http_error_is_standard) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  auto proposed = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposed));
  const auto id = proposed.value.GetString("id");
  ResponsesMcpDefinition anonymous;
  ChatPlanError error;
  Q4T_CHECK(
      PlanResponsesMcpDefinition(definition.declaration, &anonymous, &error));
  Q4T_CHECK(state.Permit(id, anonymous, Access()).code ==
            "mcp_credentials_required");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "ready");
  const auto permit = state.Permit(id, definition, Access());
  Q4T_CHECK(Ok(permit));
  auto receipt = Success(permit.value.GetString("dispatch_id"));
  receipt.outcome_unknown = true;
  auto transport = Error(502, "mcp_http_error", "Remote request failed");
  transport.value = Object({{"upstream_status", Number(503)}});
  const auto finished = state.Finish(id, receipt, transport, Access());
  Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed");
  Q4T_CHECK(finished.value.Find("error")->GetString("type") == "http_error" &&
            finished.value.Find("error")->GetInt("code") == 503 &&
            state.Inspect(id, Access()).value.GetString("state") == "unknown");
  return true;
}

Q4T_TEST(responses_mcp_state_catalog_rejects_forgery_scope_and_model) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  auto forged = catalog;
  Set(&forged, "server_label", String("forged"));
  Q4T_CHECK(!Propose(state, forged).ok());
  forged = catalog;
  Set(&Field(forged, "tools").array[0], "input_schema", Object());
  Q4T_CHECK(!Propose(state, forged).ok());
  Q4T_CHECK(!state
                 .Propose(catalog, Definition(), "read", Object(), "direct",
                          Access("client:other"))
                 .ok());
  auto config = Config();
  config.model = "different-model";
  ResponsesMcpState different(objects, config);
  Q4T_CHECK(!Propose(different, catalog).ok());
  Q4T_CHECK(Ok(Propose(state, catalog)));
  return true;
}

Q4T_TEST(responses_mcp_state_approval_single_use_and_denial_survive_restart) {
  Fixture f;
  std::string id, denied;
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto catalog = Catalog(state);
    const auto proposal = Propose(state, catalog);
    Q4T_CHECK(Ok(proposal) &&
              proposal.value.GetString("type") == "mcp_approval_request");
    id = proposal.value.GetString("id");
    Q4T_CHECK(!state.Permit(id, Definition(), Access()).ok());
    const auto allowed =
        state.Decide(id, true, "read only", Definition(), Access());
    Q4T_CHECK(Ok(allowed) &&
              allowed.value.GetString("approval_request_id") == id);
    Q4T_CHECK(
        state.Decide(id, true, "again", Definition(), Access()).http_status ==
        409);
    auto next = Propose(state, catalog);
    Q4T_CHECK(Ok(next));
    denied = next.value.GetString("id");
    next = state.Decide(denied, false, "not allowed", Definition(), Access());
    Q4T_CHECK(Ok(next) && next.value.GetString("status") == "failed");
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  Q4T_CHECK(Ok(state.Permit(id, Definition(), Access())));
  Q4T_CHECK(state.Permit(id, Definition(), Access()).http_status == 409);
  Q4T_CHECK(!state.Permit(denied, Definition("never"), Access()).ok());
  const auto persisted = state.Inspect(denied, Access());
  Q4T_CHECK(Ok(persisted) && persisted.value.GetString("state") == "denied");
  return true;
}

Q4T_TEST(responses_mcp_state_concurrent_permission_sends_at_most_once) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto proposal = Propose(state, Catalog(state, definition), definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  std::latch start(1);
  ObjectReply a, b;
  std::jthread first([&] {
    start.wait();
    a = state.Permit(id, definition, Access());
  });
  std::jthread second([&] {
    start.wait();
    b = state.Permit(id, definition, Access());
  });
  start.count_down();
  first.join();
  second.join();
  Q4T_CHECK(static_cast<int>(a.ok()) + static_cast<int>(b.ok()) == 1);
  Q4T_CHECK(!a.commit_unknown && !b.commit_unknown);
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
            "dispatching");
  const auto winner = a.ok() ? a : b;
  Q4T_CHECK(!winner.value.GetString("dispatch_id").empty());
  McpTransportReceipt losing;
  auto failure = Error(409, "mcp_dispatch_not_ready", "Other request won");
  Q4T_CHECK(state.Finish(id, losing, failure, Access()).code ==
            "mcp_dispatch_owner_conflict");
  losing.dispatch_id = "mcpd_forged";
  losing.call_permitted = true;
  losing.outcome_unknown = true;
  Q4T_CHECK(state.Finish(id, losing, failure, Access()).code ==
            "mcp_dispatch_owner_conflict");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
            "dispatching");
  Q4T_CHECK(Ok(state.Finish(id, Success(winner.value.GetString("dispatch_id")),
                            {}, Access())));
  return true;
}

Q4T_TEST(
    responses_mcp_state_original_schema_and_current_permissions_intersect) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  Q4T_CHECK(!Propose(state, catalog, definition, Value(R"({"path":4})")).ok());
  Q4T_CHECK(!Propose(state, catalog, definition, Object()).ok());
  Q4T_CHECK(
      !Propose(state, catalog, definition, Value(R"({"path":"x","extra":1})"))
           .ok());
  const auto proposal = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  for (const auto* patch :
       {R"({"allowed_tools":[]})", R"({"allowed_callers":["programmatic"]})",
        R"({"server_url":"https://another.example/mcp"})",
        R"({"server_label":"other"})"}) {
    const auto current = Definition("never", Value(patch));
    Q4T_CHECK(!Propose(state, catalog, current).ok());
    Q4T_CHECK(!state.Permit(id, current, Access()).ok());
  }
  Q4T_CHECK(state.Permit(id, Definition(), Access()).code ==
            "mcp_approval_required");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "ready");
  Q4T_CHECK(Ok(state.Permit(id, definition, Access())));
  const auto restricted =
      Definition("never", Value(R"({"allowed_callers":["programmatic"]})"));
  const auto private_catalog = Catalog(state, restricted);
  Q4T_CHECK(!Propose(state, private_catalog, definition).ok());
  Q4T_CHECK(Ok(Propose(state, private_catalog, restricted,
                       Value(R"({"path":"docs/MODEL.md"})"), "programmatic")));
  return true;
}

Q4T_TEST(responses_mcp_state_does_not_persist_credentials_even_encrypted) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  const auto proposal = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposal));
  StateCipher cipher(objects);
  for (const auto& [id, kind] :
       {std::pair(catalog.GetString("id"), "catalog"),
        std::pair(proposal.value.GetString("id"), "call")}) {
    const auto record = Record(objects, id);
    const auto* data = record.Find("data");
    Q4T_CHECK(data && data->GetString("sealed").starts_with("q4t1:"));
    std::string plain;
    const StateCipherBinding binding{
        Config().model,
        Digest(std::string(identity::kTemplateReference) + ':' +
               identity::kRenderer),
        "responses.mcp." + std::string(kind) + ".v1", id};
    Q4T_CHECK(
        Ok(cipher.Open(data->GetString("sealed"), binding, Access(), &plain)));
    Q4T_CHECK(plain.find("fixture-secret") == plain.npos &&
              plain.find("authorization") == plain.npos &&
              plain.find("X-Private") == plain.npos);
    const auto retention = InspectObjectRetention("tool_run", *data);
    Q4T_CHECK(!retention.uncertain && !retention.active &&
              !retention.collect_when_unreferenced &&
              retention.references.empty());
  }
  for (const auto& entry :
       std::filesystem::recursive_directory_iterator(f.options.directory)) {
    if (!entry.is_regular_file()) continue;
    std::ifstream file(entry.path(), std::ios::binary);
    const std::string bytes((std::istreambuf_iterator<char>(file)), {});
    Q4T_CHECK(bytes.find("fixture-secret") == bytes.npos);
  }
  return true;
}

Q4T_TEST(responses_mcp_state_unknown_dispatch_never_replays_after_restart) {
  Fixture f;
  std::string id;
  const auto definition = Definition("never");
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto proposal =
        Propose(state, Catalog(state, definition), definition);
    Q4T_CHECK(Ok(proposal));
    id = proposal.value.GetString("id");
    Q4T_CHECK(Ok(state.Permit(id, definition, Access())));
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
            "dispatching");
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  McpTransportReceipt unknown;
  // This fixture destroyed the previous executor/store owner above. Recovery
  // can reconcile its private dispatch; an ordinary losing caller cannot.
  unknown.dispatch_id =
      state.Inspect(id, Access()).value.GetString("dispatch_id");
  unknown.outcome_unknown = true;
  const auto finished = state.Finish(
      id, unknown, Error(503, "mcp_worker_failed", "Worker unavailable"),
      Access());
  Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed");
  Q4T_CHECK(finished.value.Find("error")->Find("content")->GetString("code") ==
            "mcp_result_unknown");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "unknown");
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_state_result_schema_errors_and_no_result_forgery) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  for (int variant = 0; variant < 5; ++variant) {
    const auto proposal = Propose(state, catalog, definition);
    Q4T_CHECK(Ok(proposal));
    const auto id = proposal.value.GetString("id");
    auto receipt = Success();
    Q4T_CHECK(!state.Finish(id, receipt, {}, Access()).ok());
    const auto permitted = state.Permit(id, definition, Access());
    Q4T_CHECK(Ok(permitted));
    receipt.dispatch_id = permitted.value.GetString("dispatch_id");
    if (variant == 0) {
      Set(&receipt.result, "structuredContent", Value(R"({"text":12})"));
    } else if (variant == 1) {
      std::erase_if(receipt.result.object, [](const auto& field) {
        return field.first == "structuredContent";
      });
    } else if (variant == 2) {
      Set(&receipt.result, "isError", Boolean(true));
    } else if (variant == 3) {
      receipt.error = Value(R"({"code":-32603,"message":"Remote error"})");
    } else {
      Set(&receipt.result, "structuredContent",
          Object({{"text", String(std::string(2U << 20, 'x'))}}));
      Set(&receipt.result, "content",
          Array({Object({{"type", String("text")},
                         {"text", String(std::string(2U << 20, 'x'))}})}));
    }
    const auto finished = state.Finish(id, receipt, {}, Access());
    Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed");
    if (variant == 2) {
      Q4T_CHECK(finished.value.GetString("output") ==
                OutputJson(receipt.result));
    } else {
      Q4T_CHECK(finished.value.Find("output")->IsNull());
    }
    Q4T_CHECK(!state.Finish(id, Success(), {}, Access()).ok());
    Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  }
  return true;
}

Q4T_TEST(responses_mcp_state_native_error_preserves_data_after_restart) {
  Fixture f;
  std::string id;
  Json expected;
  const auto definition = Definition("never");
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto proposal =
        Propose(state, Catalog(state, definition), definition);
    Q4T_CHECK(Ok(proposal));
    id = proposal.value.GetString("id");
    const auto permitted = state.Permit(id, definition, Access());
    Q4T_CHECK(Ok(permitted));
    auto receipt = Success(permitted.value.GetString("dispatch_id"));
    receipt.result = Value(R"({"isError":true,"content":[
      {"type":"text","text":"remote validation failed"},
      {"type":"audio","mimeType":"audio/wav","data":"UklGRg=="}],
      "structuredContent":{"reason":"missing field","retryable":false},
      "_meta":{"trace":"remote-id"}})");
    auto finished = state.Finish(id, receipt, {}, Access());
    Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed");
    Q4T_CHECK(finished.value.GetString("output") == OutputJson(receipt.result));
    expected = finished.value;
  }
  ObjectStore objects(f.options);
  ResponsesMcpState recovered(objects, Config());
  auto authenticated = recovered.Authenticate(expected, Access());
  Q4T_CHECK(Ok(authenticated) &&
            OutputJson(authenticated.value) == OutputJson(expected));
  Q4T_CHECK(Ok(recovered.Recover(id, Access())));
  Q4T_CHECK(recovered.Permit(id, definition, Access()).http_status == 409);
  auto forged = expected;
  Set(&forged, "output", String("{\"isError\":false,\"content\":[]}"));
  Q4T_CHECK(!recovered.Authenticate(forged, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_state_completed_result_roundtrip_after_restart) {
  Fixture f;
  Json final;
  std::string id;
  const auto definition = Definition("never");
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto proposal =
        Propose(state, Catalog(state, definition), definition);
    Q4T_CHECK(Ok(proposal));
    id = proposal.value.GetString("id");
    const auto permitted = state.Permit(id, definition, Access());
    Q4T_CHECK(Ok(permitted));
    const auto finished = state.Finish(
        id, Success(permitted.value.GetString("dispatch_id")), {}, Access());
    Q4T_CHECK(Ok(finished) &&
              finished.value.GetString("status") == "completed");
    final = finished.value;
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto restored = state.Inspect(id, Access());
  Q4T_CHECK(Ok(restored) && restored.value.GetString("state") == "completed");
  Q4T_CHECK(OutputJson(*restored.value.Find("terminal")) == OutputJson(final));
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_state_cancel_expiry_and_retention_are_bounded) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition, 1);
  const auto proposal = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(state.Permit(id, definition, cancelled).http_status == 499);
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(state.Permit(id, definition, expired).http_status == 408);
  const auto until = Record(objects, id).GetInt("expires_at");
  const auto deadline = Clock::now() + 2s;
  while (std::time(nullptr) < until && Clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
  Q4T_CHECK(std::time(nullptr) >= until);
  // Ordinary reads already run automatic pruning. Inspect before those reads
  // to verify the explicit collector's original-expiry decision as well.
  auto planned = objects.Retention(0, 128, {}, Access().deadline);
  Q4T_CHECK(Ok(planned));
  const auto* candidates = planned.value.GetArray("candidates");
  Q4T_CHECK(candidates && candidates->array.size() == 2);
  auto collected = objects.Retention(
      0, 128, planned.value.GetString("plan_sha256"), Access().deadline);
  Q4T_CHECK(Ok(collected) && collected.value.GetInt("deleted_objects") == 2);
  Q4T_CHECK(!Propose(state, catalog, definition).ok());
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_history_approval_index_and_optional_call_fields) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  const auto proposal = Propose(state, catalog);
  Q4T_CHECK(Ok(proposal));
  const auto approval_id = proposal.value.GetString("id");
  Q4T_CHECK(Ok(state.Authenticate(proposal.value, Access())));
  auto decision = Object({{"type", String("mcp_approval_response")},
                          {"approval_request_id", String(approval_id)},
                          {"approve", Boolean(true)}});
  Q4T_CHECK(Ok(state.Authenticate(decision, Access())));
  Q4T_CHECK(state.Inspect(approval_id, Access()).value.GetString("state") ==
            "approval");
  auto allowed =
      state.Decide(approval_id, true, "read", Definition(), Access());
  Q4T_CHECK(Ok(allowed));
  const auto call_id = allowed.value.GetString("id");
  Q4T_CHECK(call_id.starts_with("mcp_") && call_id != approval_id);
  Q4T_CHECK(state.Inspect(call_id, Access()).value.GetString("state") ==
            "ready");
  const auto permitted = state.Permit(approval_id, Definition(), Access());
  Q4T_CHECK(Ok(permitted));
  const auto finished = state.Finish(
      call_id, Success(permitted.value.GetString("dispatch_id")), {}, Access());
  Q4T_CHECK(Ok(finished));
  auto omitted = finished.value;
  std::erase_if(omitted.object, [](const auto& field) {
    return field.first == "approval_request_id" || field.first == "status" ||
           field.first == "error";
  });
  const auto restored = state.Authenticate(omitted, Access());
  Q4T_CHECK(Ok(restored) &&
            OutputJson(restored.value) == OutputJson(finished.value));
  Q4T_CHECK(Ok(state.Authenticate(proposal.value, Access())));
  Set(&decision, "approve", Boolean(false));
  Q4T_CHECK(state.Authenticate(decision, Access()).http_status == 409);
  auto forged = omitted;
  Set(&forged, "output", String("fabricated result"));
  Q4T_CHECK(!state.Authenticate(forged, Access()).ok());
  Q4T_CHECK(!state.Authenticate(omitted, Access("client:foreign")).ok());
  const auto alias = Record(objects, approval_id);
  const auto call = Record(objects, call_id);
  Q4T_CHECK(alias.GetInt("expires_at") == call.GetInt("expires_at"));
  const auto retention =
      InspectObjectRetention("tool_run", *alias.Find("data"));
  Q4T_CHECK(!retention.uncertain && !retention.collect_when_unreferenced &&
            retention.references == std::vector<std::string>{call_id});
  return true;
}

Q4T_TEST(
    responses_mcp_history_catalog_restores_metadata_without_weakening_schema) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  auto minimal = catalog;
  auto& tool = Field(minimal, "tools").array[0];
  std::erase_if(tool.object, [](const auto& field) {
    return field.first == "description" || field.first == "annotations";
  });
  const auto restored = state.Authenticate(minimal, Access());
  Q4T_CHECK(Ok(restored) && OutputJson(restored.value) == OutputJson(catalog));
  auto opened = state.OpenCatalog(minimal, Definition(), Access());
  Q4T_CHECK(Ok(opened) &&
            opened.value.GetArray("tools")->array[0].Has("outputSchema"));
  Q4T_CHECK(
      !state
           .OpenCatalog(
               minimal,
               Definition(
                   "always",
                   Value(R"({"server_url":"https://other.example/mcp"})")),
               Access())
           .ok());
  Set(&Field(minimal, "tools").array[0], "input_schema", Object());
  Q4T_CHECK(!state.Authenticate(minimal, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_history_stream_ids_and_failed_catalog_are_owned) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog =
      state.Catalog(definition, Tools(), Access(), 600, "mcpl_streamed");
  Q4T_CHECK(Ok(catalog) && catalog.value.GetString("id") == "mcpl_streamed");
  Q4T_CHECK(state.Catalog(definition, Tools(), Access(), 600, "mcpl_streamed")
                .http_status == 409);
  const auto args = Value(R"({"path":"docs/MODEL.md"})");
  const auto call = state.Propose(catalog.value, definition, "read", args,
                                  "direct", Access(), "mcp_streamed");
  Q4T_CHECK(Ok(call) && call.value.GetString("id") == "mcp_streamed");
  Q4T_CHECK(state
                .Propose(catalog.value, definition, "read", args, "direct",
                         Access(), "mcp_streamed")
                .http_status == 409);
  const auto waiting =
      state.Propose(catalog.value, Definition(), "read", args, "direct",
                    Access(), "mcp_requires_approval");
  Q4T_CHECK(Ok(waiting) &&
            waiting.value.GetString("type") == "mcp_approval_request");
  Q4T_CHECK(state.Permit("mcp_requires_approval", Definition(), Access())
                .http_status == 409);
  Q4T_CHECK(
      !state.Catalog(definition, Tools(), Access(), 600, "mcpl_bad", "failed")
           .ok());
  const auto failed = state.Catalog(definition, Array(), Access(), 600,
                                    "mcpl_failed", "MCP target unavailable");
  Q4T_CHECK(Ok(failed) && Ok(state.Authenticate(failed.value, Access())));
  Q4T_CHECK(
      !state.Propose(failed.value, definition, "read", args, "direct", Access())
           .ok());
  return true;
}

Q4T_TEST(responses_mcp_history_approval_index_is_atomic_under_object_quota) {
  Fixture f;
  // Catalog occupies one; approval+call require two. Neither may be left by a
  // rejected atomic creation. The key ring has a separate internal scope.
  f.options.max_client_objects = 2;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  Q4T_CHECK(!Propose(state, catalog).ok());
  ObjectRequest list;
  list.operation = ObjectOperation::kList;
  list.kind = ObjectKind::kToolRun;
  list.scope = Access().scope;
  auto records = objects.Execute(list, Access().deadline);
  Q4T_CHECK(Ok(records));
  const auto* data = records.value.GetArray("data");
  Q4T_CHECK(data && data->array.size() == 1 &&
            data->array.front().GetString("id") == catalog.GetString("id"));
  Q4T_CHECK(Ok(state.Authenticate(catalog, Access())));
  return true;
}

namespace {
Json PrivateSession(std::string id = "mcps_fixture",
                    std::string token = "private-token") {
  std::string scope;
  if (!InputValueIdentity(String(Access().scope), &scope))
    throw std::runtime_error("fixture session scope");
  const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();
  return Object({{"id", String(std::move(id))},
                 {"binding", String(Digest("fixture"))},
                 {"scope", String(scope)},
                 {"protocol", String("2025-06-18")},
                 {"session_id", String(std::move(token))},
                 {"legacy", Boolean(false)},
                 {"tools", Boolean(true)},
                 {"resources", Boolean(true)},
                 {"created_ms", Number(now)},
                 {"expires_ms", Number(now + 3600000)},
                 {"idle_ms", Number(300000)},
                 {"idle_expires_ms", Number(now + 300000)}});
}
}  // namespace

Q4T_TEST(responses_mcp_session_catalog_is_sealed_scope_bound_and_cas_owned) {
  Fixture fixture;
  const auto definition = Definition("never");
  const auto session = PrivateSession();
  std::string id;
  {
    ObjectStore objects(fixture.options);
    ResponsesMcpState state(objects, Config());
    const auto catalog =
        state.Catalog(definition, Tools(), Access(), 600, "", "", &session);
    Q4T_CHECK(Ok(catalog));
    id = catalog.value.GetString("id");
    Q4T_CHECK(OutputJson(catalog.value).find("private-token") ==
              std::string::npos);
    Q4T_CHECK(OutputJson(Record(objects, id)).find("private-token") ==
              std::string::npos);
    auto read = state.Session(id, definition, Access());
    Q4T_CHECK(Ok(read) && read.value.GetInt("revision") == 1 &&
              read.value.Find("session")->GetString("session_id") ==
                  "private-token");
    auto next = PrivateSession("mcps_next", "replacement-token");
    Q4T_CHECK(Ok(state.BindSession(id, definition, 1, next, Access())));
    Q4T_CHECK(state.BindSession(id, definition, 1, session, Access()).code ==
              "mcp_session_revision_conflict");
    Q4T_CHECK(!state.Session(id, definition, Access("client:other")).ok());
    auto changed = Definition(
        "never", Object({{"server_url", String("https://other.example/mcp")}}));
    Q4T_CHECK(state.Session(id, changed, Access()).code ==
              "mcp_server_changed");
    Set(&next, "authorization", String("must-not-store"));
    Q4T_CHECK(!state.BindSession(id, definition, 2, next, Access()).ok());
    Q4T_CHECK(Ok(state.Authenticate(catalog.value, Access())));
  }
  ObjectStore restarted(fixture.options);
  ResponsesMcpState state(restarted, Config());
  const auto read = state.Session(id, definition, Access());
  Q4T_CHECK(Ok(read) && read.value.GetInt("revision") == 2 &&
            read.value.Find("session")->GetString("session_id") ==
                "replacement-token");
  return true;
}

Q4T_TEST(responses_mcp_session_dispatch_and_result_keep_exact_authority) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  auto session = PrivateSession();
  const auto catalog =
      state.Catalog(definition, Tools(), Access(), 600, "", "", &session);
  Q4T_CHECK(Ok(catalog));
  const auto proposed = Propose(state, catalog.value, definition);
  Q4T_CHECK(Ok(proposed));
  const auto id = proposed.value.GetString("id");
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  auto changed = PrivateSession("mcps_other", "other-token");
  Q4T_CHECK(!state.Permit(id, definition, Access(), &changed).ok());
  const auto permitted = state.Permit(id, definition, Access(), &session);
  Q4T_CHECK(Ok(permitted));
  auto receipt = Success(permitted.value.GetString("dispatch_id"));
  Q4T_CHECK(!state.Finish(id, receipt, {}, Access(), &changed).ok());
  Q4T_CHECK(Ok(state.Finish(id, receipt, {}, Access(), &session)));
  Q4T_CHECK(!state.Permit(id, definition, Access(), &session).ok());
  const auto private_call = state.Inspect(id, Access());
  Q4T_CHECK(Ok(private_call) && private_call.value.Find("session")->GetString(
                                    "session_id") == "private-token");
  Q4T_CHECK(OutputJson(Record(objects, id)).find("private-token") ==
            std::string::npos);
  return true;
}

Q4T_TEST(responses_mcp_session_resource_keeps_original_after_catalog_renewal) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  auto session = PrivateSession();
  const auto catalog =
      state.Catalog(definition, Tools(), Access(), 600, "", "", &session);
  Q4T_CHECK(Ok(catalog));
  const auto proposed = Propose(state, catalog.value, definition);
  Q4T_CHECK(Ok(proposed));
  const auto id = proposed.value.GetString("id");
  const auto permitted = state.Permit(id, definition, Access(), &session);
  Q4T_CHECK(Ok(permitted));
  auto receipt = Success(permitted.value.GetString("dispatch_id"));
  Field(receipt.result, "content")
      .array.push_back(Object({{"type", String("resource_link")},
                               {"name", String("source")},
                               {"uri", String("file:///report0")}}));
  Q4T_CHECK(Ok(state.Finish(id, receipt, {}, Access(), &session)));
  auto renewed = PrivateSession("mcps_new", "new-token");
  Q4T_CHECK(Ok(state.BindSession(catalog.value.GetString("id"), definition, 1,
                                 renewed, Access())));
  Q4T_CHECK(
      Ok(state.ProposeResource(id, "file:///report0", definition, Access())));
  Q4T_CHECK(!state
                 .PermitResource(id, "file:///report0", definition, Access(),
                                 &renewed)
                 .ok());
  Q4T_CHECK(
      !state.PermitResource(id, "file:///report0", definition, Access()).ok());
  auto lost = session;
  Set(&lost, "lost", Boolean(true));
  Q4T_CHECK(
      !state.PermitResource(id, "file:///report0", definition, Access(), &lost)
           .ok());
  auto dispatched = state.PermitResource(id, "file:///report0", definition,
                                         Access(), &session);
  Q4T_CHECK(Ok(dispatched));
  const auto resource =
      ResourceResult(dispatched.value.GetString("dispatch_id"));
  Q4T_CHECK(
      Ok(state.FinishResource(id, "file:///report0", resource, {}, Access())));
  return true;
}

namespace {
ResponsesMcpDefinition MappedDefinition(std::string approval = "always",
                                        std::string generation = "one") {
  auto declaration = Definition(approval).declaration;
  std::erase_if(declaration.object,
                [](const auto& field) { return field.first == "server_url"; });
  Set(&declaration, "tunnel_id", String("tunnel_project"));
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(declaration, &definition, &error))
    throw std::runtime_error("Mapped state fixture");
  definition.credentials.url = "https://private.example/relay";
  definition.target_binding = Digest(generation);
  return definition;
}
}  // namespace

Q4T_TEST(responses_mcp_mapping_original_catalog_and_approval_cannot_migrate) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto original = MappedDefinition();
  const auto changed = MappedDefinition("always", "two");
  const auto catalog = Catalog(state, original);
  Q4T_CHECK(OutputJson(catalog).find(original.target_binding) ==
            std::string::npos);
  Q4T_CHECK(state.OpenCatalog(catalog, changed, Access()).code ==
            "mcp_target_changed");
  Q4T_CHECK(Propose(state, catalog, changed).code == "mcp_target_changed");
  const auto pending = Propose(state, catalog, original);
  Q4T_CHECK(Ok(pending));
  const auto id = pending.value.GetString("id");
  Q4T_CHECK(state.Decide(id, true, "", changed, Access()).code ==
            "mcp_target_changed");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "approval");
  Q4T_CHECK(Ok(state.Decide(id, true, "", original, Access())));
  const auto call =
      state.Inspect(id, Access()).value.Find("call")->GetString("id");
  Q4T_CHECK(state.Permit(call, changed, Access()).code == "mcp_target_changed");
  Q4T_CHECK(Ok(state.Permit(call, original, Access())));
  return true;
}

Q4T_TEST(responses_mcp_mapping_missing_allows_offline_history_and_denial_only) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto original = MappedDefinition();
  const auto catalog = Catalog(state, original);
  const auto pending = Propose(state, catalog, original);
  Q4T_CHECK(Ok(pending));
  auto missing = original;
  missing.target_binding.clear();
  missing.credentials.url.clear();
  Q4T_CHECK(Ok(state.OpenCatalog(catalog, missing, Access())));
  Q4T_CHECK(Propose(state, catalog, missing).code ==
            "mcp_target_mapping_required");
  Q4T_CHECK(
      state.Decide(pending.value.GetString("id"), true, "", missing, Access())
          .code == "mcp_target_mapping_required");
  Q4T_CHECK(Ok(state.Decide(pending.value.GetString("id"), false, "cancel",
                            missing, Access())));
  Q4T_CHECK(Ok(state.Authenticate(catalog, Access())));
  Q4T_CHECK(state.Catalog(missing, Tools(), Access()).code ==
            "mcp_target_mapping_required");
  return true;
}

Q4T_TEST(responses_mcp_mapping_resource_stays_bound_after_store_restart) {
  Fixture fixture;
  const auto original = MappedDefinition("never");
  const auto changed = MappedDefinition("never", "replacement");
  std::string id;
  {
    ObjectStore objects(fixture.options);
    ResponsesMcpState state(objects, Config());
    id = CompletedResourceCall(state, original).GetString("id");
  }
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  Q4T_CHECK(
      state.ProposeResource(id, "file:///report0", changed, Access()).code ==
      "mcp_target_changed");
  Q4T_CHECK(
      Ok(state.ProposeResource(id, "file:///report0", original, Access())));
  Q4T_CHECK(
      state.PermitResource(id, "file:///report0", changed, Access()).code ==
      "mcp_target_changed");
  Q4T_CHECK(
      Ok(state.PermitResource(id, "file:///report0", original, Access())));
  return true;
}

Q4T_TEST(responses_mcp_native_agent_is_private_and_part_of_dispatch_identity) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  auto session = PrivateSession();
  Set(&session, "agent_instance", String("private-agent-one"));
  const auto catalog =
      state.Catalog(definition, Tools(), Access(), 600, "", "", &session);
  Q4T_CHECK(Ok(catalog));
  Q4T_CHECK(OutputJson(catalog.value).find("private-agent-one") ==
            std::string::npos);
  Q4T_CHECK(OutputJson(Record(objects, catalog.value.GetString("id")))
                .find("private-agent-one") == std::string::npos);
  const auto proposed = Propose(state, catalog.value, definition);
  Q4T_CHECK(Ok(proposed));
  const auto id = proposed.value.GetString("id");
  auto changed = session;
  Set(&changed, "agent_instance", String("private-agent-two"));
  Q4T_CHECK(!state.Permit(id, definition, Access(), &changed).ok());
  std::erase_if(changed.object, [](const auto& field) {
    return field.first == "agent_instance";
  });
  Q4T_CHECK(!state.Permit(id, definition, Access(), &changed).ok());
  const auto permitted = state.Permit(id, definition, Access(), &session);
  Q4T_CHECK(Ok(permitted));
  auto receipt = Success(permitted.value.GetString("dispatch_id"));
  Q4T_CHECK(!state.Finish(id, receipt, {}, Access(), &changed).ok());
  Q4T_CHECK(Ok(state.Finish(id, receipt, {}, Access(), &session)));
  return true;
}

Q4T_TEST(responses_mcp_native_agent_snapshot_rejects_invalid_and_legacy_owner) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  for (const auto& instance : {String(""), String("bad\ninstance"),
                               String(std::string(129, 'a')), Number(1)}) {
    auto session = PrivateSession();
    Set(&session, "agent_instance", instance);
    Q4T_CHECK(!state
                   .Catalog(Definition("never"), Tools(), Access(), 600, "", "",
                            &session)
                   .ok());
  }
  auto session = PrivateSession();
  Set(&session, "agent_instance", String("native-agent"));
  Set(&session, "legacy", Boolean(true));
  Q4T_CHECK(!state
                 .Catalog(Definition("never"), Tools(), Access(), 600, "", "",
                          &session)
                 .ok());
  return true;
}
