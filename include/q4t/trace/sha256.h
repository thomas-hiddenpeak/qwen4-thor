#pragma once
#include <span>
#include <string>
#include "q4t/trace/router_trace.h"
namespace q4t::trace {
Digest Sha256(std::span<const uint8_t> bytes);
Status HashFile(const std::string& path, Digest* digest);
std::string HexDigest(const Digest& digest);
#if defined(Q4T_SHA256_TESTING)
bool Sha256HardwareForTest();
Digest Sha256ChunksForTest(std::span<const uint8_t> bytes, size_t chunk,
                           bool portable);
Status HashFilePortableForTest(const std::string& path, Digest* digest);
#endif
}  // namespace q4t::trace
