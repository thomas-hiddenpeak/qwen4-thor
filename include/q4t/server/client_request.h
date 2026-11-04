// Header-phase authorization shared by the live server and host contracts.
#pragma once

#include <string_view>

#include "q4t/server/client_policy.h"
#include "q4t/server/http_request.h"

namespace q4t::server {

// Exact local observation routes remain a trusted same-host boundary. All
// other routes require gateway identity when the immutable policy is enabled.
// On failure no body reservation/read or dispatch is allowed. The caller owns
// the lease through response/heartbeat cleanup, including exception unwinding.
bool AdmitClientRequest(std::string_view method, std::string_view path,
                        HttpRequestHeaders* headers, ClientAdmission& admission,
                        ClientLease* lease, ClientIdentity* identity,
                        int* failure_status);

}  // namespace q4t::server
