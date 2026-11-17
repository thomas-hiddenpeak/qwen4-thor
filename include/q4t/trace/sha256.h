#pragma once
#include <functional>
#include <span>
#include <string>
#include "q4t/trace/router_trace.h"
namespace q4t::trace {
Digest Sha256(std::span<const uint8_t> bytes);
// Same digest with a cooperative checkpoint every 64 KiB and at publication.
// Failure leaves *digest unchanged; the callback may combine cancel/deadline.
Status HashBytes(std::span<const uint8_t> bytes, Digest* digest,
                 const std::function<bool()>& cancelled = {});
Status HashFile(const std::string& path, Digest* digest);
std::string HexDigest(const Digest& digest);
#if defined(Q4T_SHA256_TESTING)
bool Sha256HardwareForTest();
Digest Sha256ChunksForTest(std::span<const uint8_t> bytes, size_t chunk,
                           bool portable);
Status HashFilePortableForTest(const std::string& path, Digest* digest);
#endif
}  // namespace q4t::trace
