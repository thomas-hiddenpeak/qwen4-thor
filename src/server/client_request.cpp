#include "q4t/server/client_request.h"
#include "q4t/server/model_catalog.h"

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
  if (identity->Scoped() &&
      headers->content_length > identity->Limits()->max_body_bytes)
    return fail(413, "client_body_limit_exceeded");
  const auto lane =
      ((method == "GET" &&
        (path == "/v1/models" || path == "/v1/capabilities")) ||
       ((method == "GET" || method == "DELETE") && IsModelDetailPath(path)))
          ? ClientLane::kControl
      : method == "POST" && path == "/v1/requests/cancel" ? ClientLane::kControl
                                                          : ClientLane::kData;
  if (!lease->Acquire(admission, *identity, lane))
    return fail(429, "client_capacity_exhausted");
  return true;
}

}  // namespace q4t::server
