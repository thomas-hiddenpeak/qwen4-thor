#include "q4t/server/live_decision_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/finite_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/schema_graph.h"

namespace q4t::server {
using namespace object_detail;
Status MakeLiveDecisionRequest(std::string model, const Json& questions,
                               Json conversation, Json items,
                               const ChatPlanConfig& planning, Json* output,
                               ChatPlanError* error) {
  if (!output || !error || !conversation.IsArray() || !items.IsArray())
    return Status::Fail("Invalid Live Decision snapshot");
  for (auto& item : items.array) {
    std::erase_if(item.object,
                  [](const auto& field) { return field.first == "id"; });
    conversation.array.push_back(std::move(item));
  }
  auto request = Object({{"model", String(std::move(model))},
                         {"questions", questions},
                         {"input", std::move(conversation)}});
  const auto status = CheckDecisionRequest(request, planning, error);
  if (status) *output = std::move(request);
  return status;
}

ObjectReply ExecuteLiveDecision(
    const Json& request, const ClientIdentity& client, ClientLease& permit,
    const std::shared_ptr<RequestControl>& control,
    HttpBodyBudget& input_budget,
    const std::function<void(const std::string&)>& execute) {
  if (!client.Allowed() || !permit.Holds(client, ClientLane::kData) ||
      !control || !control->OwnedBy(client.Id()) || !execute)
    return Error(500, "live_decision_owner_invalid", "Invalid Decision owner");
  if (control->Cancelled())
    return Error(409, control->Reason(), "Decision cancelled before execution");
  const auto body = OutputJson(request);
  const auto limit =
      client.Scoped()
          ? std::min<size_t>(kChatBodyBytes, client.Limits()->max_body_bytes)
          : kChatBodyBytes;
  if (body.size() > limit)
    return Error(413, "live_input_capacity",
                 "Decision exceeds client body limit");
  HttpBodyLease lease(input_budget);
  if (!lease.Acquire(body.size()))
    return Error(503, "body_capacity_exhausted",
                 "Aggregate body capacity exhausted");
  if (!control->ClaimInvocation())
    return Error(409, "decision_already_invoked",
                 "Decision cannot be replayed");
  FiniteResponse captured;
  detail::HttpResponseContext context(
      detail::HttpResponseContext::Detached{-1, &captured});
  context.SetClient(client, &permit);
  if (!context.SetTraceId(control->id()))
    return Error(500, "live_decision_owner_invalid",
                 "Invalid Decision trace ID");
  try {
    execute(body);
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(-1, *context.State(),
                                  detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(-1, *context.State());
  }
  Json result;
  if (!captured.Complete() || !io::ParseJson(captured.TakeBody(), &result, true,
                                             kApplicationJsonLimits))
    return Error(503, "live_decision_incomplete", "Missing final Decision");
  if (captured.Status() != 200) {
    const auto* details = result.Find("error");
    if (!details || !details->IsObject())
      return Error(502, "live_decision_invalid", "Invalid Decision error");
    auto reply =
        Error(captured.Status(), details->GetString("code", "server_error"),
              details->GetString("message", "Decision failed"));
    if (const auto* param = details->Find("param"); param && param->IsString())
      reply.value = Object({{"param", *param}});
    return reply;
  }
  context.RefreshUsage();
  const auto actual = context.Usage().Total();
  ChatPlanError error;
  const auto* answers = result.GetArray("answers");
  Json expected;
  std::string observed_usage, expected_usage;
  if (!actual || !answers ||
      !ValidateDecisionShape(result, DecisionShape::kResult, &error) ||
      !BuildDecisionResult(request, *answers, *actual, &expected) ||
      result.GetString("model") != expected.GetString("model") ||
      !CanonicalSchemaValue(*result.Find("usage"), &observed_usage) ||
      !CanonicalSchemaValue(*expected.Find("usage"), &expected_usage) ||
      observed_usage != expected_usage)
    return Error(502, "live_decision_invalid",
                 "Decision identity, answers or actual usage did not match");
  if (control->Cancelled())
    return Error(409, control->Reason(), "Decision cancelled");
  ObjectReply reply;
  reply.value = std::move(result);
  return reply;
}
}  // namespace q4t::server
