#include "q4t/server/access_program.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "q4t/server/chat_capabilities.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_reasoning.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using namespace std::chrono_literals;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("access fixture JSON");
  return value;
}
const std::string kPolicy = R"({"version":1,"authority":"q4t-local",
  "model":"cpu","cyber_tier":"blue","minimum_grant":"blue",
  "default_selection":"model_tier","standard_allowed":true,
  "program_instructions":"NATIVE_AUTHORIZED_REVIEW: explain defensive checks.",
  "clients":[{"id":"cpu","grant":"blue"},{"id":"red","grant":"red"}]})";
std::shared_ptr<AccessProgramPolicy> Policy(std::string raw = kPolicy) {
  auto value = std::make_shared<AccessProgramPolicy>();
  const auto status = ParseAccessProgramPolicy(raw, value.get());
  if (!status) throw std::runtime_error(status.message());
  return value;
}
ClientLimits kBlue{.id = "cpu"}, kRed{.id = "red"}, kOther{.id = "other"};
ChatPlanConfig Config(const ClientLimits* client = &kBlue) {
  ChatPlanConfig value;
  value.model = "cpu";
  value.model_aliases = {"alias"};
  value.client_limits = client;
  value.access_program_policy = Policy();
  return value;
}
bool Select(std::string fields, const ChatPlanConfig& config,
            AccessProgramSelection* output, ChatPlanError* error) {
  return ResolveAccessProgram(Parse("{" + fields + "}"), config, output, error)
      .ok();
}
struct Fixture {
  std::filesystem::path path;
  Fixture() {
    const auto* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto parent = std::filesystem::absolute(
        root ? root : ".q4t-work/serve-access-programs-20261110/cpu");
    std::filesystem::create_directories(parent);
    auto name = (parent / "access-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("access fixture path");
    path = name;
  }
};
StateAccess Access() {
  return {"client:cpu", std::chrono::steady_clock::now() + 5s, {}};
}
void Write(const std::filesystem::path& path, const std::string& value) {
  std::ofstream(path, std::ios::binary) << value;
}
}  // namespace

Q4T_TEST(access_program_standard_defaults_and_strict_request_types) {
  ChatPlanConfig config;
  config.model = "cpu";
  AccessProgramSelection selected;
  ChatPlanError error;
  for (const auto* fields : {"", R"("access_programs":{})"}) {
    Q4T_CHECK(Select(fields, config, &selected, &error));
    Q4T_CHECK(selected.selected.IsNull() && selected.provenance.IsNull() &&
              selected.instructions.empty());
  }
  Q4T_CHECK(Select(R"("access_programs":{"cyber":"standard"})", config,
                   &selected, &error));
  Q4T_CHECK(selected.selected.GetString("cyber") == "standard");
  for (const auto* value :
       {"null", "false", "[]", "42", R"({"cyber":null})", R"({"cyber":false})",
        R"({"cyber":""})", R"({"cyber":"future"})", R"({"unknown":true})",
        R"({"cyber":"standard","grant":"red"})"}) {
    Q4T_CHECK(!Select(std::string("\"access_programs\":") + value, config,
                      &selected, &error));
    Q4T_CHECK(error.http_status == 400 && selected.selected.IsNull());
  }
  for (const auto* tier : {"daybreak_blue", "daybreak_red"}) {
    Q4T_CHECK(
        !Select(std::string(R"("access_programs":{"cyber":")") + tier + R"("})",
                config, &selected, &error));
    Q4T_CHECK(error.code == "unsupported_access_program");
  }
  return true;
}

Q4T_TEST(access_program_blue_authorization_and_explicit_selection_matrix) {
  AccessProgramSelection selected;
  ChatPlanError error;
  for (const auto* client :
       {&kBlue, &kRed, &kOther, static_cast<ClientLimits*>(nullptr)}) {
    auto config = Config(client);
    const bool authorized = client == &kBlue || client == &kRed;
    Q4T_CHECK(Select("", config, &selected, &error));
    Q4T_CHECK(selected.selected.IsNull() == !authorized);
    Q4T_CHECK(selected.instructions.empty() == !authorized);
    Q4T_CHECK(selected.provenance.GetString("authority") == "q4t-local");
    Q4T_CHECK(Select(R"("access_programs":{"cyber":"daybreak_blue"})", config,
                     &selected, &error) == authorized);
    if (!authorized)
      Q4T_CHECK(error.http_status == 403 &&
                error.code == "access_program_not_enabled");
    Q4T_CHECK(Select(R"("access_programs":{"cyber":"standard"})", config,
                     &selected, &error));
    Q4T_CHECK(selected.instructions.empty() &&
              selected.selected.GetString("cyber") == "standard");
    Q4T_CHECK(!Select(R"("access_programs":{"cyber":"daybreak_red"})", config,
                      &selected, &error));
    Q4T_CHECK(error.http_status == 400 &&
              error.code == "invalid_access_program");
  }
  return true;
}

Q4T_TEST(access_program_model_minimum_grant_and_no_implicit_red_fallback) {
  auto policy = Policy();
  policy->minimum_grant = "red";
  auto config = Config();
  config.access_program_policy = policy;
  AccessProgramSelection selected;
  ChatPlanError error;
  Q4T_CHECK(Select("", config, &selected, &error) &&
            selected.selected.IsNull());
  Q4T_CHECK(!Select(R"("access_programs":{"cyber":"daybreak_blue"})", config,
                    &selected, &error) &&
            error.http_status == 403);
  config.client_limits = &kRed;
  Q4T_CHECK(Select("", config, &selected, &error));
  Q4T_CHECK(selected.selected.GetString("cyber") == "daybreak_blue");
  policy->default_standard = true;
  Q4T_CHECK(Select("", config, &selected, &error) &&
            selected.selected.IsNull());
  policy->tier = "red";
  policy->default_standard = false;
  config.client_limits = &kBlue;
  Q4T_CHECK(!Select("", config, &selected, &error) && error.http_status == 403);
  policy->standard_allowed = false;
  Q4T_CHECK(!Select(R"("access_programs":{"cyber":"standard"})", config,
                    &selected, &error) &&
            error.http_status == 400);
  config.client_limits = &kRed;
  Q4T_CHECK(Select("", config, &selected, &error));
  Q4T_CHECK(selected.selected.GetString("cyber") == "daybreak_red");
  config.model = "different-model";
  Q4T_CHECK(!Select("", config, &selected, &error) && error.http_status == 503);
  return true;
}

Q4T_TEST(access_program_private_file_binding_and_transactional_errors) {
  auto policy = Policy();
  GatewayClientPolicy clients;
  clients.clients = {kBlue, kRed};
  Q4T_CHECK(BindAccessProgramPolicy(*policy, "cpu", &clients));
  Q4T_CHECK(!BindAccessProgramPolicy(*policy, "alias", &clients));
  Q4T_CHECK(!BindAccessProgramPolicy(*policy, "cpu", nullptr));
  clients.clients.pop_back();
  Q4T_CHECK(!BindAccessProgramPolicy(*policy, "cpu", &clients));
  const auto original = policy->sha256;
  for (const auto* patch :
       {R"("version":1.0)", R"("authority":"openai")",
        R"("cyber_tier":"standard")", R"("minimum_grant":"admin")",
        R"("default_selection":"highest")", R"("standard_allowed":null)",
        R"("program_instructions":"   ")",
        R"("clients":[{"id":"cpu","grant":"blue"},
                                            {"id":"cpu","grant":"red"}])"}) {
    const auto replacement = Parse(std::string("{") + patch + "}");
    auto bad = Parse(kPolicy);
    for (auto& field : bad.object)
      if (field.first == replacement.object[0].first)
        field.second = replacement.object[0].second;
    Q4T_CHECK(!ParseAccessProgramPolicy(OutputJson(bad), policy.get()));
    Q4T_CHECK(policy->sha256 == original);
  }
  Q4T_CHECK(!ParseAccessProgramPolicy(kPolicy + "{}", policy.get()));
  Q4T_CHECK(!ParseAccessProgramPolicy(std::string(65537, ' '), policy.get()));
  Fixture fixture;
  const auto file = fixture.path / "policy.json";
  Write(file, kPolicy);
  chmod(file.c_str(), 0600);
  Q4T_CHECK(LoadAccessProgramPolicy(file, policy.get()));
  chmod(file.c_str(), 0644);
  Q4T_CHECK(!LoadAccessProgramPolicy(file, policy.get()));
  chmod(file.c_str(), 0600);
  std::filesystem::create_symlink(file, fixture.path / "link");
  Q4T_CHECK(!LoadAccessProgramPolicy(fixture.path / "link", policy.get()));
  std::filesystem::create_hard_link(file, fixture.path / "hard");
  Q4T_CHECK(!LoadAccessProgramPolicy(file, policy.get()));
  return true;
}

Q4T_TEST(access_program_actual_context_pro_summary_and_standard_override) {
  auto config = Config();
  // Context planning uses fully initialized normal client quotas in runtime.
  // This fixture tests the authorization boundary independently of quotas.
  auto limits = kBlue;
  limits.max_prompt_tokens = 8192;
  limits.max_output_tokens = 512;
  config.client_limits = &limits;
  for (const auto* effort : {"none", "minimal", "high", "max"}) {
    const auto request = Parse(
        std::string(R"({"input":"Check the project documents.","model":"alias",
          "reasoning":{"summary":"concise","effort":")") +
        effort + "\"}}");
    ResponsesEnvelope envelope;
    ChatPlanError error;
    Q4T_CHECK(ParseResponsesEnvelope(request, config, &envelope, &error));
    ResponsesGenerationPlan plan;
    Q4T_CHECK(PrepareResponsesGeneration(envelope, *request.Find("input"),
                                         config, &plan, &error));
    const auto& actual = plan.chat.Request(plan.generation_request);
    Q4T_CHECK(actual.GetArray("messages")
                  ->array[0]
                  .GetString("content")
                  .starts_with("NATIVE_AUTHORIZED_REVIEW"));
    Q4T_CHECK(OutputJson(plan.input.items).find("NATIVE_AUTHORIZED_REVIEW") ==
              std::string::npos);
    std::string rendered, param, code;
    std::vector<VisionItem> media;
    Q4T_CHECK(PrepareChatPrompt(actual, plan.chat.constraints, &rendered,
                                &media, &param, &code));
    Q4T_CHECK(rendered.find("NATIVE_AUTHORIZED_REVIEW") <
              rendered.find("Check the project documents."));
    ResponsesSummaryPlan pro, summary;
    Q4T_CHECK(PlanResponsesDeliberation(plan, 256, 0, config, &pro, &error));
    Q4T_CHECK(pro.request.GetArray("messages")
                  ->array[0]
                  .GetString("content")
                  .starts_with("NATIVE_AUTHORIZED_REVIEW"));
    const auto snapshot = Parse(R"({"output":[{"type":"reasoning","content":[
      {"type":"reasoning_text","text":"Checked the existing source."}]}]})");
    Q4T_CHECK(PlanResponsesSummary(envelope, snapshot, 256, 20, config,
                                   &summary, &error));
    Q4T_CHECK(summary.request.GetArray("messages")
                  ->array[0]
                  .GetString("content")
                  .starts_with("NATIVE_AUTHORIZED_REVIEW"));
  }
  ResponsesEnvelope envelope;
  ChatPlanError error;
  const auto standard = Parse(R"({"input":"Check the project documents.",
    "access_programs":{"cyber":"standard"},"reasoning":{"effort":"none"}})");
  Q4T_CHECK(ParseResponsesEnvelope(standard, config, &envelope, &error));
  ResponsesGenerationPlan plan;
  Q4T_CHECK(PrepareResponsesGeneration(envelope, *standard.Find("input"),
                                       config, &plan, &error));
  Q4T_CHECK(
      OutputJson(plan.generation_request).find("NATIVE_AUTHORIZED_REVIEW") ==
      std::string::npos);
  return true;
}

Q4T_TEST(access_program_queued_restore_revalidates_policy_and_grant) {
  Fixture fixture;
  ObjectStoreOptions options;
  options.directory = fixture.path / "objects";
  ObjectStore store(options);
  auto config = Config();
  auto limits = kBlue;
  limits.max_output_tokens = 512;
  limits.max_prompt_tokens = 8192;
  config.client_limits = &limits;
  ResponsesExecution run(store);
  const auto request = Parse(R"({"input":"Review the document.",
      "background":true,"access_programs":{"cyber":"daybreak_blue"},
      "reasoning":{"effort":"none"}})");
  Q4T_CHECK(run.Prepare(request, config, Access(), 42, true).ok());
  std::string wire;
  Q4T_CHECK(run.Begin(&wire).ok());
  ResponsesExecution same(store);
  Q4T_CHECK(same.RestoreQueued(run.Id(), config, Access()).ok());
  auto changed = Policy();
  changed->sha256 = std::string(64, 'f');
  config.access_program_policy = changed;
  ResponsesExecution different(store);
  Q4T_CHECK(different.RestoreQueued(run.Id(), config, Access()).code ==
            "response_access_program_changed");
  config.access_program_policy = Policy();
  config.client_limits = &kOther;
  ResponsesExecution denied(store);
  Q4T_CHECK(denied.RestoreQueued(run.Id(), config, Access()).code ==
            "access_program_not_enabled");
  Q4T_CHECK(!different.Prepared() && !denied.Prepared());
  return true;
}

Q4T_TEST(access_program_actual_wire_compaction_and_original_input) {
  Fixture fixture;
  ObjectStoreOptions options;
  options.directory = fixture.path / "objects";
  ObjectStore store(options);
  auto config = Config();
  auto limits = kBlue;
  limits.max_output_tokens = 512;
  limits.max_prompt_tokens = 8192;
  config.client_limits = &limits;
  ResponsesExecution run(store);
  const auto request =
      Parse(R"({"input":[{"role":"user","content":"Old question"},
    {"role":"assistant","content":"Old answer"},
    {"role":"user","content":"Current question"}],
    "context_management":[{"type":"compaction","compact_threshold":1}],
    "stream":true,"stream_options":{"include_obfuscation":false},
    "reasoning":{"effort":"none"}})");
  Q4T_CHECK(run.Prepare(request, config, Access(), 42).ok());
  Q4T_CHECK(run.SelectCompaction(100, config).ok());
  Q4T_CHECK(run.CompactionSelected());
  Q4T_CHECK(run.Compaction()
                .request.GetArray("messages")
                ->array[0]
                .GetString("content")
                .starts_with("NATIVE_AUTHORIZED_REVIEW"));
  std::string wire, all;
  Q4T_CHECK(run.Begin(&wire).ok());
  all += wire;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "Reviewed actual project material.";
  Q4T_CHECK(run.Append(delta, "null", &wire));
  all += wire;
  Q4T_CHECK(run.Finish(false, 0, {40, 0, 5, 0}, 43, &wire).ok());
  all += wire;
  const auto result = run.ResultJson();
  Q4T_CHECK(result.find("NATIVE_AUTHORIZED_REVIEW") == result.npos);
  Q4T_CHECK(result.find("q4t-local") != result.npos);
  Write(fixture.path / "access-program.json.wire", result);
  Write(fixture.path / "access-program.sse.wire", all);
  const auto caps = ChatCapabilitiesJson("cpu", false, true, 0,
                                         config.access_program_policy.get());
  Q4T_CHECK(caps.find("NATIVE_AUTHORIZED_REVIEW") == caps.npos);
  Q4T_CHECK(caps.find("\"cloud_approval\":false") != caps.npos);
  return true;
}
