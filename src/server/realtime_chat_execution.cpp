#include "q4t/server/realtime_chat_execution.h"

#include "object_protocol.h"
#include "q4t/server/finite_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
RealtimeGenerationResult ExecuteRealtimeChat(
    const io::Json& request, const ClientIdentity& client, ClientLease& permit,
    const std::shared_ptr<RequestControl>& control,
    const RealtimeDeltaSink& emit,
    const std::function<void(NativeChatGeneration&)>& execute,
    HttpBodyBudget& input_budget) {
  using namespace object_detail;
  RealtimeGenerationResult result;
  const auto fail = [&](int http, std::string code, std::string message) {
    result.reply = Error(http, std::move(code), std::move(message));
    result.state = RealtimeResponseEnd::State::kFailed;
    return result;
  };
  NativeChatGeneration native(request, emit);
  if (!client.Allowed() || !permit.Holds(client, ClientLane::kData) ||
      !control || !control->OwnedBy(client.Id()) || !execute || !native.Valid())
    return fail(500, "realtime_execution_owner_invalid",
                "Invalid native Realtime execution owner");
  if (control->Cancelled())
    return fail(409, control->Reason(), "Response cancelled before execution");
  const auto wire = OutputJson(request);
  const auto limit =
      client.Scoped()
          ? std::min<size_t>(kChatBodyBytes, client.Limits()->max_body_bytes)
          : kChatBodyBytes;
  if (wire.size() > limit)
    return fail(413, "realtime_input_capacity",
                "Response input exceeds its client byte limit");
  HttpBodyLease input(input_budget);
  if (!input.Acquire(wire.size()))
    return fail(503, "body_capacity_exhausted",
                "Aggregate request body capacity exhausted");
  if (!control->ClaimInvocation())
    return fail(409, "realtime_execution_already_claimed",
                "Response execution cannot be replayed");
  FiniteResponse captured(kWebSocketMessageLimit);
  detail::HttpResponseContext context(
      detail::HttpResponseContext::Detached{-1, &captured});
  context.SetClient(client, &permit);
  if (!context.SetTraceId(control->id()))
    return fail(500, "realtime_execution_owner_invalid",
                "Invalid response identity");
  try {
    execute(native);
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(-1, *context.State(),
                                  detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(-1, *context.State());
  }
  native.Seal();
  context.RefreshUsage();
  if (auto usage = context.Usage().Total()) {
    RealtimeResponseUsage observed;
    observed.total = *usage;
    result.usage = observed;
  }
  if (!native.Error().ok()) {
    result.reply = native.Error();
    result.state = RealtimeResponseEnd::State::kFailed;
    return result;
  }
  Json body;
  if (!captured.Complete() ||
      !io::ParseJson(captured.Body(), &body, true, kApplicationJsonLimits))
    return fail(503, "realtime_response_incomplete",
                "Native Chat did not return a complete response");
  if (captured.Status() < 200 || captured.Status() >= 300) {
    const auto* error = body.Find("error");
    return fail(
        captured.Status(),
        error ? error->GetString("code", "server_error") : "server_error",
        error ? error->GetString("message", "Native response failed")
              : "Native response failed");
  }
  const auto* choices = body.GetArray("choices");
  if (!choices || choices->array.size() != 1 ||
      !choices->array[0].Find("message") ||
      !choices->array[0].Find("message")->IsObject())
    return fail(503, "realtime_response_invalid",
                "Native Chat returned an invalid terminal response");
  const auto& choice = choices->array[0];
  const auto reason = choice.GetString("finish_reason");
  if (reason == "length" || reason == "content_filter") {
    result.state = RealtimeResponseEnd::State::kIncomplete;
    result.reason = reason == "length" ? "max_output_tokens" : reason;
  } else if (reason != "stop" && reason != "tool_calls") {
    return fail(503, "realtime_response_invalid",
                "Native Chat returned an unknown finish reason");
  }
  result.message = *choice.Find("message");
  return result;
}
}  // namespace q4t::server
