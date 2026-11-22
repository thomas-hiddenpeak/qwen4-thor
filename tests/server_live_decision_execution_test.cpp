#include "q4t/server/live_decision_execution.h"

#include "../src/server/responses_json.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::responses_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;

struct Evaluation {
  Json questions =
      Array({Object({{"type", String("predicate")},
                     {"instructions", String("Check the version")}})});
  ChatPlanConfig plan;
  ClientAdmission clients;
  ClientIdentity client = clients.Authenticate("", "");
  ClientLease permit;
  HttpBodyBudget budget{4096};
  std::shared_ptr<RequestControl> control = std::make_shared<RequestControl>(
      "decision_test", "", Clock::now() + 5s, client.Id());
  Json request;
  Evaluation() {
    plan.model = "native";
    ChatPlanError error;
    if (!MakeLiveDecisionRequest("native", questions, Array(), Array(), plan,
                                 &request, &error) ||
        !permit.Acquire(clients, client, ClientLane::kData))
      throw std::runtime_error("Decision fixture owner");
  }
  ObjectReply Run(const std::function<void(const std::string&)>& fn) {
    return ExecuteLiveDecision(request, client, permit, control, budget, fn);
  }
  Json Result(const ActualUsage& usage) {
    Json result;
    if (!BuildDecisionResult(request,
                             Array({Object({{"type", String("predicate")},
                                            {"name", {}},
                                            {"probability", Number(1)}})}),
                             usage, &result))
      throw std::runtime_error("Decision fixture output");
    return result;
  }
};
}  // namespace

Q4T_TEST(live_decision_snapshot_preserves_data_and_call_lineage) {
  Evaluation f;
  Json out;
  ChatPlanError error;
  auto first =
      Object({{"role", String("user")}, {"content", String("startup")}});
  auto json = Object({{"type", String("input_json")},
                      {"data", Object({{"id", String("project record")}})}});
  auto items = Array({Object({{"id", String("session_local")},
                              {"role", String("user")},
                              {"content", Array({json})}}),
                      Object({{"type", String("function_call")},
                              {"id", String("local_call")},
                              {"name", String("read_doc")},
                              {"call_id", String("c1")},
                              {"arguments", String("{}")}}),
                      Object({{"type", String("function_call_output")},
                              {"call_id", String("c1")},
                              {"output", String("rev426")}})});
  Q4T_CHECK(MakeLiveDecisionRequest("native", f.questions, Array({first}),
                                    items, f.plan, &out, &error));
  auto snapshot = OutputJson(out);
  const auto& input = out.GetArray("input")->array;
  Q4T_CHECK(input.size() == 4 && input[0].GetString("content") == "startup" &&
            !input[1].Has("id") && items.array[0].Has("id") &&
            input[1].Find("content")->array[0].Find("data")->GetString("id") ==
                "project record" &&
            input[2].GetString("call_id") == "c1");
  items.array.erase(items.array.begin() + 1);
  Q4T_CHECK(!MakeLiveDecisionRequest("native", f.questions, Array(), items,
                                     f.plan, &out, &error) &&
            error.param == "input[1].call_id" && OutputJson(out) == snapshot);
  Q4T_CHECK(!MakeLiveDecisionRequest("other", f.questions, Array(), Array(),
                                     f.plan, &out, &error) &&
            error.http_status == 404);
  return true;
}

Q4T_TEST(
    live_decision_finite_execution_checks_real_usage_and_single_invocation) {
  Evaluation f;
  size_t calls = 0;
  bool owner = false;
  const auto invoke = [&](const std::string& body) {
    ++calls;
    auto* http = detail::HttpResponseContext::Current(-1);
    owner = http && http->Client().Id() == f.client.Id() &&
            http->RateDomain() == ClientRateDomain::kOnline &&
            http->TraceId() == f.control->id() &&
            f.budget.Used() == body.size();
    const ActualUsage used{30, 1, 4, 0, 2};
    http->Usage().Enable();
    http->Usage().Merge(used);
    auto result = f.Result(used);
    std::ranges::reverse(Field(result, "usage").object);
    detail::SendSimple(-1, 200, "OK", OutputJson(result), "application/json");
  };
  auto reply = f.Run(invoke);
  Q4T_CHECK(reply.ok() && calls == 1 && owner && f.budget.Used() == 0 &&
            reply.value.Find("usage")->GetInt("total_tokens") == 31);
  Q4T_CHECK(f.Run(invoke).code == "decision_already_invoked" && calls == 1);
  return true;
}

Q4T_TEST(
    live_decision_finite_execution_rejects_missing_or_inconsistent_terminal) {
  for (int i = 0; i < 5; ++i) {
    Evaluation f;
    auto reply = f.Run([&](const auto&) {
      if (i == 0) return;
      auto* http = detail::HttpResponseContext::Current(-1);
      auto result = f.Result({5, 1});
      if (i != 1) {
        http->Usage().Enable();
        http->Usage().Merge(ActualUsage{5, i == 2 ? 2u : 1u});
      }
      if (i == 3) Set(&result, "model", String("other"));
      if (i == 4)
        Set(&Field(result, "answers").array[0], "name", String("other"));
      detail::SendSimple(-1, 200, "OK", OutputJson(result), "application/json");
    });
    Q4T_CHECK(!reply.ok() && !reply.value.Has("answers") &&
              f.budget.Used() == 0);
  }
  return true;
}

Q4T_TEST(
    live_decision_finite_execution_isolates_auth_capacity_cancel_and_quota) {
  Evaluation f;
  size_t calls = 0;
  const auto invoke = [&](const auto&) {
    ++calls;
    detail::SendError(-1, 429, "Current limit", {}, "client_rpm_exhausted",
                      "questions");
  };
  auto own = f.control;
  f.control = std::make_shared<RequestControl>("foreign", "", Clock::now() + 1s,
                                               "other");
  Q4T_CHECK(f.Run(invoke).code == "live_decision_owner_invalid");
  f.control = std::make_shared<RequestControl>("expired", "", Clock::now() - 1s,
                                               f.client.Id());
  Q4T_CHECK(f.Run(invoke).code == "deadline_exceeded");
  f.control = own;
  {
    HttpBodyLease other(f.budget);
    Q4T_CHECK(other.Acquire(4096) &&
              f.Run(invoke).code == "body_capacity_exhausted");
  }
  Q4T_CHECK(calls == 0);
  auto reply = f.Run(invoke);
  Q4T_CHECK(calls == 1 && reply.http_status == 429 &&
            reply.code == "client_rpm_exhausted" &&
            reply.value.GetString("param") == "questions" &&
            f.budget.Used() == 0);
  return true;
}
