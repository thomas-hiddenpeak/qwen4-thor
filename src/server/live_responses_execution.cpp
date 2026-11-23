#include "q4t/server/live_responses_execution.h"

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/live_responses_settings.h"

namespace q4t::server {
LiveResponsesResult ExecuteLiveResponses(
    const io::Json& settings, io::Json history, const ClientIdentity& client,
    ClientLease& permit, const std::shared_ptr<RequestControl>& control,
    HttpBodyBudget& input_budget, NativeResponsesStream::Sink sink,
    const std::function<void(const std::string&)>& execute) {
  using namespace object_detail;
  LiveResponsesResult result;
  const auto fail = [&](int status, std::string code, std::string message) {
    result.reply = Error(status, std::move(code), std::move(message));
    return result;
  };
  if (!client.Allowed() || !permit.Holds(client, ClientLane::kData) ||
      !control || !control->OwnedBy(client.Id()) || !sink || !execute)
    return fail(500, "live_response_owner_invalid",
                "Responses invocation owner is invalid");
  if (control->Cancelled())
    return fail(409, control->Reason(), "Response cancelled before execution");
  Json request;
  ChatPlanError error;
  auto status =
      MakeLiveResponsesRequest(settings, std::move(history), &request, &error);
  if (!status) {
    result.reply = Error(error.http_status, error.code, status.message());
    result.reply.value = Object({{"param", String(error.param)}});
    return result;
  }
  const auto body = OutputJson(request);
  request = {};
  const size_t limit =
      client.Scoped()
          ? std::min<size_t>(kChatBodyBytes, client.Limits()->max_body_bytes)
          : kChatBodyBytes;
  if (body.size() > limit)
    return fail(413, "live_input_capacity",
                "Delegated response exceeds client body limit");
  HttpBodyLease input(input_budget);
  if (!input.Acquire(body.size()))
    return fail(503, "body_capacity_exhausted",
                "Aggregate request body capacity exhausted");
  if (!control->ClaimInvocation())
    return fail(409, "response_already_invoked",
                "Delegated work cannot be replayed");
  NativeResponsesStream capture(std::move(sink));
  detail::HttpResponseContext context(
      detail::HttpResponseContext::Detached{-1, nullptr, &capture});
  context.SetClient(client, &permit);
  if (!context.SetTraceId(control->id()))
    return fail(500, "live_response_owner_invalid",
                "Invalid Responses trace identity");
  try {
    execute(body);
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(-1, *context.State(),
                                  detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(-1, *context.State());
  }
  capture.Seal();
  context.RefreshUsage();
  result.usage = context.Usage().Total();
  result.reply = capture.Error();
  if (capture.Complete()) result.terminal = capture.Response();
  if (!result.reply.ok()) return result;
  if (!capture.Complete() || result.terminal.IsNull()) {
    if (control->Cancelled())
      return fail(409, control->Reason(), "Delegated response cancelled");
    return fail(503, "live_response_incomplete",
                "Missing the final Responses event");
  }
  return result;
}
}  // namespace q4t::server
