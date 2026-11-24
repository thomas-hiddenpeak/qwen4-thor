#include "q4t/server/application_routes.h"
#include "q4t/test.h"

namespace q4t::server {
namespace {
Q4T_TEST(agent_automation_routes_preserve_nested_ownership_and_methods) {
  using Kind = AutomationRouteKind;
  constexpr auto root = "/v1/agents/automations";
  constexpr auto list = "/v1/agents/automations/automation_a/invocations";
  constexpr auto row =
      "/v1/agents/automations/automation_a/invocations/invocation_b";
  Q4T_CHECK(IsManagedAgentTarget(root));
  Q4T_CHECK(ManagedAgentMethodAllowed("GET", root));
  Q4T_CHECK(ManagedAgentMethodAllowed("POST", root));
  Q4T_CHECK(!ManagedAgentMethodAllowed("DELETE", root));
  Q4T_CHECK(ManagedAgentMethodAllowed("GET", list));
  Q4T_CHECK(ManagedAgentMethodAllowed("POST", list));
  const auto parsed = ParseAutomationRoute(row);
  Q4T_CHECK(parsed.kind == Kind::kInvocation);
  Q4T_CHECK(parsed.automation == "automation_a");
  Q4T_CHECK(parsed.invocation == "invocation_b");
  Q4T_CHECK(IsManagedAgentObservation("GET", row));
  Q4T_CHECK(!ManagedAgentMethodAllowed("POST", row));
  Q4T_CHECK(!IsAgentRegistrationTarget(root));
  Q4T_CHECK(!IsManagedAgentTarget("/v1/agents/automations_extra"));
  Q4T_CHECK(!IsManagedAgentTarget("/v1/agents/automations/automation_"));
  Q4T_CHECK(!IsManagedAgentTarget("/v1/agents/automations/automation_a/"));
  Q4T_CHECK(!IsManagedAgentTarget(
      "/v1/agents/automations/automation_a/invocations/invocation_%2Fb"));
  Q4T_CHECK(!IsManagedAgentTarget(
      "/v1/agents/automations/automation_a/invocations/invocation_b#other"));
  return true;
}

Q4T_TEST(agent_automation_actions_are_mutations) {
  using Kind = AutomationRouteKind;
  constexpr auto id = "/v1/agents/automations/automation_a";
  constexpr auto pause = "/v1/agents/automations/automation_a/pause";
  constexpr auto resume = "/v1/agents/automations/automation_a/resume";
  Q4T_CHECK(ManagedAgentMethodAllowed("GET", id));
  Q4T_CHECK(ManagedAgentMethodAllowed("POST", id));
  Q4T_CHECK(ManagedAgentMethodAllowed("DELETE", id));
  Q4T_CHECK(ParseAutomationRoute(pause).kind == Kind::kPause);
  Q4T_CHECK(IsAutomationAction(pause));
  Q4T_CHECK(IsAutomationAction(resume));
  Q4T_CHECK(
      IsAutomationAction("/v1/agents/automations/automation_a/invocations"));
  Q4T_CHECK(!IsAutomationAction(id));
  Q4T_CHECK(!IsAutomationAction("/v1/agents/automations"));
  Q4T_CHECK(ManagedAgentMethodAllowed("POST", pause));
  Q4T_CHECK(!ManagedAgentMethodAllowed("GET", pause));
  Q4T_CHECK(ParseAutomationRoute(resume).kind == Kind::kResume);
  Q4T_CHECK(ManagedAgentMethodAllowed("POST", resume));
  Q4T_CHECK(!ManagedAgentMethodAllowed("DELETE", resume));
  Q4T_CHECK(
      ParseAutomationRoute("/v1/agents/automations?after=automation_a").kind ==
      Kind::kCollection);
  return true;
}
}  // namespace
}  // namespace q4t::server
