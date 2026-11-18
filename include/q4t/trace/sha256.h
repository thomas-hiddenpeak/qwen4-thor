#pragma once
#include <functional>
#include <memory>
#include <span>
#include <string>
#include "q4t/trace/router_trace.h"
namespace q4t::trace {
// Bounded incremental SHA-256; retains only one block and the hash state.
// Snapshot does not finalize/mutate the stream, so later Add remains valid.
// The configured byte cap is additionally bounded by SHA-256's bit length.
class Sha256Stream {
 public:
  explicit Sha256Stream(uint64_t max_bytes = UINT64_MAX / 8);
  ~Sha256Stream();
  Sha256Stream(const Sha256Stream&) = delete;
  Sha256Stream& operator=(const Sha256Stream&) = delete;
  Status Add(std::span<const uint8_t> bytes);
  Digest Snapshot() const;
  uint64_t bytes() const;

 private:
  struct State;
  std::unique_ptr<State> state_;
};
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
