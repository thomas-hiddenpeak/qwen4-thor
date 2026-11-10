#include "q4t/server/batch_execution.h"

#include <new>

#include "object_protocol.h"
#include "q4t/server/finite_response.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
BatchRowResult ExecuteBatchEndpoint(
    const std::string& request_id, const ClientIdentity& client,
    ClientLease& permit, const std::shared_ptr<RequestControl>& control,
    const std::function<void()>& execute,
    std::shared_ptr<ClientRateLease> rate) {
  using namespace object_detail;
  BatchRowResult result;
  result.request_id = request_id;
  const auto failure = [&](int status, std::string code, std::string message) {
    result.error = Error(status, std::move(code), std::move(message));
    return result;
  };
  if (!ValidId(request_id) || !control || control->id() != request_id ||
      !client.Allowed() || !permit.Holds(client, ClientLane::kData) ||
      !execute || (rate && !rate->PrechargedFor(client)))
    return failure(500, "batch_execution_owner_invalid",
                   "Native Batch execution ownership is invalid");
  const auto interrupted = [&]() -> BatchRowResult {
    if (std::chrono::steady_clock::now() >= control->deadline())
      return failure(408, "request_deadline_exceeded",
                     "Batch row execution deadline exceeded");
    return failure(499, "batch_cancelled", "Batch row execution cancelled");
  };
  if (control->Cancelled()) return interrupted();
  if (!control->ClaimInvocation())
    return failure(409, "batch_execution_already_claimed",
                   "The original execution ticket cannot be replayed");
  FiniteResponse output;
  detail::HttpResponseContext http(
      detail::HttpResponseContext::Detached{-1, &output});
  http.SetClient(client, &permit, ClientRateDomain::kBatch);
  if (rate && !http.BindBatchRate(std::move(rate)))
    return failure(500, "batch_execution_rate_invalid",
                   "Native Batch execution rate ownership is invalid");
  if (!http.SetTraceId(request_id))
    return failure(500, "batch_execution_owner_invalid",
                   "Invalid native Batch request identity");
  try {
    execute();
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(-1, *http.State(),
                                   detail::ExceptionKind::kHostMemory);
  } catch (...) {
    detail::SendExceptionResponse(-1, *http.State());
  }
  // A received terminal stays valid if cancellation arrives during cleanup.
  // Missing or partial output never turns into a success or a model replay.
  if (output.Complete()) {
    result.http_status = output.Status();
    result.response_body = output.TakeBody();
    return result;
  }
  if (control->Cancelled()) return interrupted();
  return failure(503, "batch_response_incomplete",
                 "Native endpoint did not produce one complete JSON response");
}
}  // namespace q4t::server
