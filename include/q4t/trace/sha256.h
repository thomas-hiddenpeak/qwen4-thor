#pragma once
#include <span>
#include <string>
#include "q4t/trace/router_trace.h"
namespace q4t::trace {
Digest Sha256(std::span<const uint8_t> bytes);
Status HashFile(const std::string& path, Digest* digest);
std::string HexDigest(const Digest& digest);
}  // namespace q4t::trace
