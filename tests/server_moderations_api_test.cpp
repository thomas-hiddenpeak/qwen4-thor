#include "q4t/server/moderations_execution.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using q4t::io::Json;
Json Read(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("moderation fixture JSON");
  return value;
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "actual-qwen";
  config.model_aliases = {"alias"};
  config.structured = true;
  config.allow_media = true;
  return config;
}
ModerationsRequestPlan Plan(const Json& request) {
  ModerationsRequestPlan plan;
  ChatPlanError error;
  const auto status = PlanModerationsRequest(request, Config(), &plan, &error);
  if (!status) throw std::runtime_error(status.message() + error.param);
  return plan;
}
const std::string kScores = R"({"scores":[0,0,0,0,0,0,0,0,0,0,0,0.8,0]})";
Json Report(const ModerationPlan& plan) {
  Json report;
  if (!ParseModerationResult(kScores, plan, 0.5, &report))
    throw std::runtime_error("classifier fixture");
  return report;
}
GatewayClientPolicy Policy(uint64_t tpm = 8192) {
  GatewayClientPolicy policy;
  policy.version = 3;
  policy.gateway_token = std::string(64, 'b');
  policy.sha256 = std::string(64, 'a');
  policy.fair_queue = true;
  ClientLimits limits;
  limits.id = "alpha";
  limits.max_inflight = 2;
  limits.max_control_inflight = 2;
  limits.max_prompt_tokens = 8192;
  limits.max_output_tokens = 1;
  limits.max_body_bytes = 1 << 20;
  limits.requests_per_minute = 1;
  limits.tokens_per_minute = tpm;
  policy.clients.push_back(limits);
  return policy;
}
ObjectReply Step(ModelPreparation& account, ModerationStepResult* out,
                 uint64_t input = 10, uint64_t output = 3) {
  auto reply = account.ReservePrompt(input);
  if (!reply.ok()) return reply;
  if (!account.StartStep()) throw std::runtime_error("step ownership");
  account.ObserveOutput(output);
  *out = {kScores, "stop"};
  return {};
}
}  // namespace

Q4T_TEST(moderations_request_defaults_aliases_and_real_model_identity) {
  for (const auto* raw :
       {R"({"input":"hello"})", R"({"model":"alias","input":"hello"})",
        R"({"model":"actual-qwen","input":""})"}) {
    const auto plan = Plan(Read(raw));
    Q4T_CHECK(plan.model == "actual-qwen" && plan.inputs.size() == 1 &&
              plan.OutputBudget() == 512 && plan.inputs[0].text);
    Q4T_CHECK(plan.inputs[0].chat.constraints.json_output &&
              !plan.inputs[0].request.Has("tools"));
  }
  ModerationsRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanModerationsRequest(
      Read(R"({"model":"omni-moderation-latest","input":"x"})"), Config(),
      &plan, &error));
  Q4T_CHECK(error.http_status == 404 && error.param == "model" &&
            plan.inputs.empty());
  return true;
}

Q4T_TEST(moderations_string_array_preserves_each_input_and_order) {
  const auto request = Read(R"({"input":["first content","second content"]})");
  const auto original = OutputJson(request);
  const auto plan = Plan(request);
  Q4T_CHECK(plan.inputs.size() == 2 && plan.OutputBudget() == 1024);
  for (size_t i = 0; i < 2; ++i) {
    const auto wire = OutputJson(plan.inputs[i].request);
    Q4T_CHECK(wire.find(i == 0 ? "first content" : "second content") !=
              wire.npos);
    Q4T_CHECK(wire.find(i == 0 ? "second content" : "first content") ==
              wire.npos);
  }
  Q4T_CHECK(OutputJson(request) == original);
  return true;
}

Q4T_TEST(moderations_multimodal_parts_share_one_real_content_group) {
  auto request = Read(R"({"input":[{"type":"text","text":"actual context"},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]})");
  const auto plan = Plan(request);
  Q4T_CHECK(plan.inputs.size() == 1 && plan.inputs[0].text &&
            plan.inputs[0].image && plan.OutputBudget() == 512);
  Q4T_CHECK(OutputJson(plan.inputs[0].request).find("actual context") !=
            std::string::npos);
  Field(request, "input").array.erase(Field(request, "input").array.begin());
  const auto image = Plan(request);
  Q4T_CHECK(!image.inputs[0].text && image.inputs[0].image);
  return true;
}

Q4T_TEST(moderations_invalid_types_fields_and_partial_plans_fail_closed) {
  for (
      const auto* raw :
      {"null",
       "{}",
       R"({"input":null})",
       R"({"input":[]})",
       R"({"input":42})",
       R"({"input":{},"model":"alias"})",
       R"({"input":"ok","model":null})",
       R"({"input":"ok","stream":true})",
       R"({"input":["ok",null]})",
       R"({"input":["ok",{}]})",
       R"({"input":[{"type":"text","text":"ok"},"mixed"]})",
       R"({"input":[{"type":"text"}]})",
       R"({"input":[{"type":"text","text":5}]})",
       R"({"input":[{"type":"text","text":"ok","extra":0}]})",
       R"({"input":[{"type":"image_url"}]})",
       R"({"input":[{"type":"image_url","image_url":null}]})",
       R"({"input":[{"type":"image_url","image_url":{"url":""}}]})",
       R"({"input":[{"type":"image_url","image_url":{"url":5}}]})",
       R"({"input":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AA==","detail":"auto"}}]})",
       R"({"input":[{"type":"input_audio","input_audio":{}}]})"}) {
    auto plan = Plan(Read(R"({"input":"old"})"));
    ChatPlanError error;
    Q4T_CHECK(!PlanModerationsRequest(Read(raw), Config(), &plan, &error));
    Q4T_CHECK(plan.inputs.empty() && plan.model.empty());
  }
  return true;
}

Q4T_TEST(moderations_request_capacity_is_aggregate_and_never_truncates) {
  auto request = Object({{"input", Array()}});
  auto& values = Field(request, "input").array;
  values.assign(kModerationsInputs, String("small"));
  Q4T_CHECK(Plan(request).inputs.size() == kModerationsInputs);
  values.push_back(String("one too many"));
  ModerationsRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanModerationsRequest(request, Config(), &plan, &error));
  Q4T_CHECK(error.http_status == 413 && plan.inputs.empty());
  values = {String(std::string(kModerationsTextBytes / 2, 'a')),
            String(std::string(kModerationsTextBytes / 2 + 1, 'b'))};
  Q4T_CHECK(!PlanModerationsRequest(request, Config(), &plan, &error));
  Q4T_CHECK(error.http_status == 413 && error.param == "input[1]" &&
            plan.inputs.empty());
  values.assign(kModerationsParts + 1,
                Object({{"type", String("text")}, {"text", String("")}}));
  Q4T_CHECK(!PlanModerationsRequest(request, Config(), &plan, &error));
  Q4T_CHECK(error.http_status == 413);
  return true;
}

Q4T_TEST(moderations_missing_backend_is_not_a_safe_response) {
  auto config = Config();
  config.structured = false;
  ModerationsRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanModerationsRequest(Read(R"({"input":"hello"})"), config, &plan,
                                    &error));
  Q4T_CHECK(error.code == "structured_backend_unavailable" &&
            plan.inputs.empty());
  return true;
}

Q4T_TEST(moderations_public_response_requires_complete_real_reports) {
  auto plan = Plan(Read(R"({"input":["one","two"]})"));
  std::vector<Json> reports{Report(plan.inputs[0]), Report(plan.inputs[1])};
  Json response;
  Q4T_CHECK(BuildModerationsResponse("modr_actual", plan, reports, &response));
  Q4T_CHECK(response.object.size() == 3 && !response.Has("usage") &&
            response.GetArray("results")->array.size() == 2);
  for (const auto& value : response.GetArray("results")->array)
    Q4T_CHECK(value.object.size() == 4 && !value.Has("type") &&
              !value.Has("model") && value.GetBool("flagged"));
  if (const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR")) {
    std::filesystem::create_directories(output);
    std::ofstream(std::filesystem::path(output) / "moderations-response.json")
        << OutputJson(response);
  }
  for (const auto& invalid :
       {std::vector<Json>{}, std::vector<Json>{reports[0]},
        std::vector<Json>{reports[0],
                          ModerationError("unavailable", "failed")}}) {
    Q4T_CHECK(
        !BuildModerationsResponse("modr_actual", plan, invalid, &response));
    Q4T_CHECK(response.IsNull());
  }
  Set(&reports[1], "model", String("unrelated"));
  Q4T_CHECK(!BuildModerationsResponse("modr_actual", plan, reports, &response));
  Q4T_CHECK(response.IsNull());
  return true;
}

Q4T_TEST(moderations_execution_shares_one_rpm_and_settles_actual_work) {
  ClientAdmission clients;
  Q4T_CHECK(clients.Configure(Policy()));
  const auto client = clients.Authenticate("alpha", std::string(64, 'b'));
  ClientRateLease rate;
  auto plan = Plan(Read(R"({"input":["one","two"]})"));
  int steps = 0;
  auto reply = ExecuteModerationsRequest(
      "modr_actual", plan, clients, client, rate,
      [&](const auto&, auto& account, auto* out) {
        ++steps;
        return Step(account, out, steps * 10, steps * 3);
      },
      [] { return false; });
  Q4T_CHECK(reply.ok() && steps == 2 &&
            reply.value.GetArray("results")->array.size() == 2);
  Q4T_CHECK(clients.RateUsage(0).requests == 1);
  rate.Settle();
  const auto usage = clients.RateUsage(0);
  Q4T_CHECK(usage.reserved == 0 && usage.admitted == 1 &&
            usage.settled_tokens == 39);
  return true;
}

Q4T_TEST(moderations_execution_failure_keeps_usage_without_partial_success) {
  for (const int mode : {0, 1, 2}) {
    ClientAdmission clients;
    Q4T_CHECK(clients.Configure(Policy()));
    const auto client = clients.Authenticate("alpha", std::string(64, 'b'));
    ClientRateLease rate;
    const auto plan = Plan(Read(R"({"input":["one","two","three"]})"));
    int steps = 0;
    auto reply = ExecuteModerationsRequest(
        "modr_actual", plan, clients, client, rate,
        [&](const auto&, auto& account, auto* out) {
          ++steps;
          auto result = Step(account, out);
          if (steps == 2) {
            if (mode == 0) out->content = "{}";
            if (mode == 1) out->finish_reason = "length";
            if (mode == 2) return Error(503, "device_fault", "actual failure");
          }
          return result;
        },
        [] { return false; });
    Q4T_CHECK(!reply.ok() && steps == 2 && !reply.value.Has("results"));
    rate.Settle();
    Q4T_CHECK(clients.RateUsage(0).settled_tokens == 26 &&
              clients.RateUsage(0).reserved == 0);
  }
  return true;
}

Q4T_TEST(moderations_execution_cancel_and_quota_stop_before_more_work) {
  for (const bool denied : {false, true}) {
    ClientAdmission clients;
    Q4T_CHECK(clients.Configure(Policy(denied ? 16 : 8192)));
    const auto client = clients.Authenticate("alpha", std::string(64, 'b'));
    ClientRateLease rate;
    const auto plan = Plan(Read(R"({"input":["one","two"]})"));
    int steps = 0;
    auto reply = ExecuteModerationsRequest(
        "modr_actual", plan, clients, client, rate,
        [&](const auto&, auto& account, auto* out) {
          ++steps;
          return Step(account, out);
        },
        [&] { return steps == 1; });
    Q4T_CHECK(!reply.ok() && steps == 1 && !reply.value.Has("results"));
    Q4T_CHECK(reply.http_status == (denied ? 429 : 409));
    rate.Settle();
    const auto usage = clients.RateUsage(0);
    Q4T_CHECK(usage.reserved == 0 && usage.settled_tokens == (denied ? 0 : 13));
  }
  return true;
}

Q4T_TEST(moderations_batch_precharge_is_claimed_once_without_online_usage) {
  ClientAdmission clients;
  Q4T_CHECK(clients.Configure(Policy()));
  const auto client = clients.Authenticate("alpha", std::string(64, 'b'));
  ClientRateLease rate(ClientRateDomain::kBatch);
  const auto plan = Plan(Read(R"({"input":["one","two"]})"));
  Q4T_CHECK(rate.PrechargeBatch(clients, client, 30, 0, ClientClock::now(), 0,
                                plan.OutputBudget()) ==
            ClientRateResult::kAllowed);
  auto reply = ExecuteModerationsRequest(
      "modr_actual", plan, clients, client, rate,
      [](const auto&, auto& account, auto* out) { return Step(account, out); },
      [] { return false; });
  Q4T_CHECK(reply.ok());
  rate.Settle();
  const auto usage =
      clients.RateUsage(0, ClientClock::now(), ClientRateDomain::kBatch);
  Q4T_CHECK(usage.requests == 1 && usage.admitted == 1 &&
            usage.settled_tokens == 26 && usage.reserved == 0 &&
            clients.RateUsage(0).admitted == 0);
  return true;
}
