// Bounded portable quota snapshots. No keys, file I/O or CUDA dependencies.
#pragma once

#include <string>
#include <string_view>

#include "q4t/server/client_policy.h"

namespace q4t::server {

inline constexpr size_t kClientRateStateBytes = 48 * 1024;
Status ParseClientRateState(std::string_view raw, ClientRateState* output);
std::string EncodeClientRateState(const ClientRateState& state);
bool ValidClientRateState(const ClientRateState& state);

}  // namespace q4t::server
