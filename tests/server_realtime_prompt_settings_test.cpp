#include "q4t/server/realtime_prompt_settings.h"

#include <cstdlib>
#include <filesystem>
#include <memory>

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("Realtime prompt fixture JSON");
  return value;
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu";
  return config;
}
StateAccess Access() {
  return {"client:realtime-prompt", Clock::now() + std::chrono::seconds(5), {}};
}
struct Fixture {
  ObjectStoreOptions options;
  std::unique_ptr<ObjectStore> store;
  std::unique_ptr<PromptRegistry> registry;
  RealtimePromptSettings settings{DefaultRealtimeChatSettings(Config(), "")};
  size_t lookups = 0;
  RealtimePromptResolver resolve;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(output ? output : ".q4t-work/prompt");
    std::filesystem::create_directories(root);
    auto path = (root / "realtime-prompt-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("prompt fixture path");
    options.directory = path + "/objects";
    store = std::make_unique<ObjectStore>(options);
    registry = std::make_unique<PromptRegistry>(*store);
    resolve = [&](const Json& reference, size_t budget, PromptSnapshot* out) {
      ++lookups;
      return registry->Resolve(reference, Access(), out, budget);
    };
  }
  Json Definition(std::string text, Json extra = Object()) {
    Set(&extra, "instructions", String(std::move(text)));
    return Object({{"api", String("realtime")},
                   {"request", extra},
                   {"variables", Object()}});
  }
  Json Create(const Json& definition) {
    const auto created = registry->Create(definition, Config(), Access());
    if (!created.ok()) throw std::runtime_error(created.message);
    return Object({{"id", String(created.value.GetString("id"))}});
  }
  ObjectReply Update(Json update, const RealtimePromptValidator& valid = {}) {
    Set(&update, "type", String("realtime"));
    return settings.Update(update, resolve, valid);
  }
  ObjectReply Select(const Json& reference) {
    return Update(Object({{"prompt", reference}}));
  }
};
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
}  // namespace

Q4T_TEST(realtime_prompt_defaults_do_not_mask_template_fields) {
  Fixture f;
  const auto reference = f.Create(
      f.Definition("from actual template", Parse(R"({"max_output_tokens":17,
      "tools":[{"name":"lookup"}],"tool_choice":"required"})")));
  Q4T_CHECK(Ok(f.Select(reference)));
  const auto& effective = f.settings.Current();
  Q4T_CHECK(effective.settings.GetString("instructions") ==
            "from actual template");
  Q4T_CHECK(effective.settings.GetInt("max_output_tokens") == 17 &&
            effective.settings.GetArray("tools")->array.size() == 1 &&
            effective.settings.GetString("tool_choice") == "required");
  Q4T_CHECK(effective.prompt &&
            effective.settings.Find("prompt")->GetString("version") == "1");
  return true;
}

Q4T_TEST(realtime_prompt_explicit_fields_survive_selection_and_null_clear) {
  Fixture f;
  Q4T_CHECK(Ok(f.Update(Parse(R"({"instructions":"client","tools":[],
                                "max_output_tokens":29})"))));
  auto reference = f.Create(f.Definition("template", Parse(R"({
    "max_output_tokens":7,"tools":[{"name":"lookup"}]})")));
  Q4T_CHECK(Ok(f.Select(reference)));
  Q4T_CHECK(f.settings.Current().settings.GetString("instructions") ==
            "client");
  Q4T_CHECK(f.settings.Current().settings.GetArray("tools")->array.empty());
  Q4T_CHECK(Ok(f.Update(Parse(R"({"instructions":""})"))));
  Q4T_CHECK(Ok(f.Select(Json())));
  Q4T_CHECK(!f.settings.Current().prompt &&
            f.settings.Current().settings.Find("prompt")->IsNull() &&
            f.settings.Current().settings.GetString("instructions").empty() &&
            f.settings.Current().settings.GetInt("max_output_tokens") == 29);
  return true;
}

Q4T_TEST(realtime_prompt_ordinary_updates_never_follow_or_reload_registry) {
  Fixture f;
  auto reference = f.Create(f.Definition("version one"));
  Q4T_CHECK(Ok(f.Select(reference)));
  const auto id = reference.GetString("id");
  Q4T_CHECK(Ok(f.registry->Append(id, 1, f.Definition("version two"), Config(),
                                  Access())));
  Q4T_CHECK(Ok(f.Update(Parse(R"({"max_output_tokens":19})"))));
  Q4T_CHECK(f.lookups == 1 && f.settings.Current().settings.GetString(
                                  "instructions") == "version one");
  Q4T_CHECK(Ok(f.registry->Erase(id, Access())));
  Q4T_CHECK(Ok(f.Update(Parse(R"({"parallel_tool_calls":false})"))));
  RealtimePromptSelection response;
  Q4T_CHECK(Ok(f.settings.Response(Object(), f.resolve, {}, &response)));
  Q4T_CHECK(f.lookups == 1 && response.prompt &&
            response.prompt->reference.GetString("version") == "1");
  return true;
}

Q4T_TEST(realtime_prompt_single_response_override_and_clear_are_isolated) {
  Fixture f;
  const auto session = f.Create(f.Definition("session template"));
  const auto other = f.Create(f.Definition("response template"));
  Q4T_CHECK(Ok(f.Select(session)));
  const auto before = OutputJson(f.settings.Current().settings);
  RealtimePromptSelection response;
  Q4T_CHECK(Ok(f.settings.Response(Object({{"prompt", other}}), f.resolve, {},
                                   &response)));
  Q4T_CHECK(response.settings.GetString("instructions") == "response template");
  Q4T_CHECK(OutputJson(f.settings.Current().settings) == before);
  Q4T_CHECK(Ok(f.settings.Response(Parse(R"({"prompt":null,"input":[],
       "conversation":"none","metadata":{"key":"value"}})"),
                                   f.resolve, {}, &response)));
  Q4T_CHECK(!response.prompt && response.settings.GetString("instructions") ==
                                    "You are a helpful assistant.");
  Q4T_CHECK(!response.settings.Has("input") &&
            !response.settings.Has("conversation") &&
            !response.settings.Has("metadata"));
  Q4T_CHECK(OutputJson(f.settings.Current().settings) == before);
  return true;
}

Q4T_TEST(realtime_prompt_failed_validation_does_not_leak_explicit_override) {
  Fixture f;
  const auto before = OutputJson(f.settings.Current().settings);
  auto rejected = f.Update(
      Parse(R"({"instructions":"must not persist"})"),
      [](const auto&) { return Error(503, "fixture_unavailable", "fixture"); });
  Q4T_CHECK(!rejected.ok() &&
            OutputJson(f.settings.Current().settings) == before);
  auto reference = f.Create(f.Definition("template survives"));
  Q4T_CHECK(Ok(f.Select(reference)));
  Q4T_CHECK(f.settings.Current().settings.GetString("instructions") ==
            "template survives");
  RealtimePromptSelection output = f.settings.Current();
  const auto old_output = OutputJson(output.settings);
  Q4T_CHECK(!f.settings
                 .Response(
                     Parse(R"({"prompt":null})"), f.resolve,
                     [](const auto&) {
                       return Error(503, "fixture_unavailable", "fixture");
                     },
                     &output)
                 .ok());
  Q4T_CHECK(OutputJson(output.settings) == old_output && output.prompt);
  return true;
}

Q4T_TEST(realtime_prompt_immutable_model_and_tracing_apply_to_templates) {
  Fixture f;
  auto wrong = f.Create(f.Definition("wrong", Parse(R"({"model":"other"})")));
  Q4T_CHECK(f.Select(wrong).code == "realtime_model_immutable");
  RealtimePromptSelection output;
  Q4T_CHECK(
      f.settings.Response(Object({{"prompt", wrong}}), f.resolve, {}, &output)
          .code == "realtime_model_immutable");
  auto traced =
      f.Create(f.Definition("traced", Parse(R"({"tracing":"auto"})")));
  Q4T_CHECK(Ok(f.Select(traced)));
  Q4T_CHECK(f.Select(Json()).code == "realtime_tracing_immutable");
  Q4T_CHECK(f.settings.Current().settings.GetString("tracing") == "auto");
  return true;
}

Q4T_TEST(
    realtime_prompt_nested_explicit_audio_does_not_freeze_template_defaults) {
  Fixture f;
  auto first = f.Create(f.Definition("first", Parse(R"({"audio":{"output":{
    "voice":"alloy","speed":1.0},"input":{"noise_reduction":null}}})")));
  auto next = f.Create(f.Definition("next", Parse(R"({"audio":{"output":{
    "voice":"ash","speed":0.9}}})")));
  Q4T_CHECK(Ok(f.Select(first)));
  Q4T_CHECK(Ok(f.Update(Parse(R"({"audio":{"output":{"speed":1.2},
    "input":{"transcription":null}}})"))));
  Q4T_CHECK(Ok(f.Select(next)));
  const auto* audio = f.settings.Current().settings.Find("audio");
  Q4T_CHECK(audio->Find("output")->GetString("voice") == "ash" &&
            audio->Find("output")->GetNumber("speed") == 1.2 &&
            audio->Find("input")->Find("transcription")->IsNull());
  return true;
}

Q4T_TEST(realtime_prompt_prefix_is_private_and_accepted_snapshots_are_values) {
  Fixture f;
  const auto first = f.Create(f.Definition("read source", Parse(R"({
    "input":[{"role":"user","content":"source-bound prefix"}]})")));
  Q4T_CHECK(Ok(f.Select(first)));
  RealtimePromptSelection response;
  Q4T_CHECK(Ok(f.settings.Response(Object(), f.resolve, {}, &response)));
  Q4T_CHECK(!response.settings.Has("input") && response.prompt &&
            response.prompt->request.GetArray("input")->array.size() == 1);
  const auto saved = OutputJson(response.prompt->request);
  Q4T_CHECK(Ok(f.Select(Json())));
  Q4T_CHECK(Ok(f.registry->Erase(first.GetString("id"), Access())));
  Q4T_CHECK(OutputJson(response.prompt->request) == saved);
  return true;
}

Q4T_TEST(realtime_prompt_expansion_uses_owner_budget_and_keeps_previous_state) {
  Fixture f;
  const auto reference = f.Create(Parse(R"({"api":"realtime","variables":{
    "value":"string"},"request":{"instructions":"{{value}}{{value}}{{value}}"}})"));
  auto small = reference;
  Set(&small, "variables", Object({{"value", String("small")}}));
  Q4T_CHECK(Ok(f.Select(small)));
  const auto before = OutputJson(f.settings.Current().settings);
  auto large = reference;
  Set(&large, "variables",
      Object({{"value", String(std::string(6ULL << 20, 'x'))}}));
  Q4T_CHECK(f.Select(large).code == "prompt_expansion_limit");
  Q4T_CHECK(OutputJson(f.settings.Current().settings) == before);
  PromptSnapshot snapshot;
  Q4T_CHECK(f.registry->Resolve(small, Access(), &snapshot, 0).code ==
            "prompt_budget_invalid");
  Q4T_CHECK(Ok(f.registry->Resolve(small, Access(), &snapshot, 4096)));
  return true;
}

Q4T_TEST(realtime_prompt_lookup_failures_and_mismatched_identity_are_atomic) {
  Fixture f;
  const auto reference = f.Create(f.Definition("template"));
  Q4T_CHECK(Ok(f.Select(reference)));
  const auto before = OutputJson(f.settings.Current().settings);
  auto lookup = f.resolve;
  f.resolve = [&](const Json& ref, size_t budget, PromptSnapshot* out) {
    auto result = lookup(ref, budget, out);
    if (result.ok()) Set(&out->reference, "version", String("2"));
    return result;
  };
  auto pinned = reference;
  Set(&pinned, "version", String("1"));
  Q4T_CHECK(f.Select(pinned).code == "prompt_snapshot_mismatch");
  f.resolve = [](const auto&, size_t, auto*) {
    ObjectReply result;
    result.commit_unknown = true;
    return result;
  };
  Q4T_CHECK(f.Select(reference).commit_unknown);
  Q4T_CHECK(OutputJson(f.settings.Current().settings) == before);
  return true;
}
