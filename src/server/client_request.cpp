#include "q4t/server/client_request.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/application_routes.h"

namespace q4t::server {

bool AdmitClientRequest(std::string_view method, std::string_view path,
                        HttpRequestHeaders* headers, ClientAdmission& admission,
                        ClientLease* lease, ClientIdentity* identity,
                        int* failure_status) {
  *identity = {};
  const auto fail = [&](int status, const char* code) {
    *failure_status = status;
    headers->error_code = code;
    headers->failure = HttpReceiveFailure::kAdmission;
    return false;
  };
  const bool local =
      method == "GET" &&
      (path == "/healthz" || path == "/livez" || path == "/readyz" ||
       path == "/v1/config" || path == "/metrics");
  if (local) {
    // Identity-bearing traffic must not enter the local administration plane.
    if (!headers->client_id.empty() || !headers->gateway_token.empty())
      return fail(403, "client_route_forbidden");
    return true;
  }
  *identity =
      admission.Authenticate(headers->client_id, headers->gateway_token);
  if (!identity->Allowed()) return fail(401, "client_authentication_failed");
  if (identity->Scoped()) {
    const auto& limits = *identity->Limits();
    headers->body_limit =
        method == "POST" && path == "/v1/files" && limits.max_file_body_bytes
            ? limits.max_file_body_bytes
            : limits.max_body_bytes;
  }
  if (identity->Scoped() && headers->content_length > headers->body_limit)
    return fail(413, "client_body_limit_exceeded");
  const auto lane =
      IsCancellationRequest(method, path) ? ClientLane::kCancel
      : ((method == "GET" &&
          (path == "/v1/models" || path == "/v1/capabilities")) ||
         ((method == "GET" || method == "DELETE") && IsModelDetailPath(path)) ||
         IsResponsesObservation(method, path) ||
         IsFileMetadata(method, path) ||
         IsPromptObservation(method, path) ||
         IsStoredChatManagement(method, path))
          ? ClientLane::kControl
          : ClientLane::kData;
  if (!lease->Acquire(admission, *identity, lane))
    return fail(429, "client_capacity_exhausted");
  return true;
}

}  // namespace q4t::server
