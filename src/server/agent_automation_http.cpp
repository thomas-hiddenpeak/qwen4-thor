#include "agent_automation_internal.h"

#include <ctime>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server {
ObjectReply HandleAgentAutomation(ObjectStore& objects, ResponsesJobs* jobs,
                                  const ChatPlanConfig& config,
                                  const StateAccess& access,
                                  std::string_view method,
                                  std::string_view target,
                                  const std::string& body) {
  using namespace object_detail;
  using Kind = AutomationRouteKind;
  const auto route = ParseAutomationRoute(target);
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Automation route not found");
  if (!AutomationMethodAllowed(method, route.kind)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    auto allowed =
        route.kind == Kind::kResource ? "GET, POST, DELETE"
        : route.kind == Kind::kCollection || route.kind == Kind::kInvocations
            ? "GET, POST"
        : route.kind == Kind::kInvocation ? "GET"
                                          : "POST";
    reply.value = Object({{"allow", String(allowed)}});
    return reply;
  }
  auto invalid = [](std::string key, std::string message) {
    auto reply = Error(400, "invalid_request", std::move(message));
    reply.value = Object({{"param", String(std::move(key))}});
    return reply;
  };
  if (method != "POST" && !body.empty())
    return invalid("body", "Unexpected request body");
  const auto split = target.find('?');
  const auto raw = split == target.npos ? "" : target.substr(split + 1);
  if (method == "GET" &&
      (route.kind == Kind::kCollection || route.kind == Kind::kInvocations ||
       route.kind == Kind::kInvocation))
    return automation_detail::List(objects, access, route, raw);
  if (!raw.empty()) return invalid("query", "Unexpected query parameters");
  auto request = Object();
  if (method == "POST" && !body.empty()) {
    auto status = io::ParseJson(body, &request, true, kApplicationJsonLimits);
    if (!status || !request.IsObject())
      return invalid("body", "Expected a JSON object");
  }
  const bool activating =
      method == "POST" &&
      (route.kind == Kind::kInvocations || route.kind == Kind::kCollection ||
       route.kind == Kind::kResume);
  if (activating && (!jobs || !jobs->AutomationsAvailable()))
    return Error(503, "automation_dispatcher_unavailable",
                 "Automation execution is unavailable");
  ObjectReply result;
  if (route.kind == Kind::kInvocations) {
    if (!request.object.empty())
      return invalid("body", "Unexpected invocation parameters");
    ObjectRequest lookup;
    lookup.kind = ObjectKind::kAutomation;
    lookup.scope = access.scope;
    lookup.id = route.automation;
    auto parent = objects.Execute(lookup, access.deadline, access.cancelled);
    if (!parent.ok()) return parent;
    result = automation_detail::Queue(objects, access, parent.value,
                                      std::time(nullptr), 0);
  } else {
    result = automation_detail::Registry(objects, access, config, route, method,
                                         request, std::time(nullptr));
  }
  if (jobs && method != "GET" && (result.ok() || result.commit_unknown))
    jobs->WakeAutomations();
  return result;
}
}  // namespace q4t::server
