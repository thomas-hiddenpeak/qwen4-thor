#pragma once

#include <functional>

#include "q4t/server/batch_state.h"
#include "q4t/server/client_policy.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
// A successful durable StartRow receipt must precede this call. The caller
// owns the permit/control until all execution has drained; this adapter never
// admits, retries, starts a worker or performs another HTTP request.
// The callback invokes the actual native endpoint and must drain its resources
// on every exit, including exceptions. Its complete finite HTTP JSON is kept.
BatchRowResult ExecuteBatchEndpoint(
    const std::string& request_id, const ClientIdentity& client,
    ClientLease& permit, const std::shared_ptr<RequestControl>& control,
    const std::function<void()>& execute,
    std::shared_ptr<ClientRateLease> rate = {}, std::string_view endpoint = {});
}  // namespace q4t::server
