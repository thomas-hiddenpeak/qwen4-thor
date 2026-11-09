// Authenticated opaque application state. No public API can read key material.
#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/native_crypto.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {
struct StateCipherBinding {
  std::string model;
  std::string template_id;
  std::string purpose;
  std::string item_id;
};

class StateCipher {
 public:
  explicit StateCipher(ObjectStore& objects) : objects_(objects) {}
  static constexpr size_t kMaxKeys = 8;
  // Bounds the number of random 96-bit nonces used by one 256-bit key.
  static constexpr int64_t kMaxSeals = 1 << 20;
  // Lazy first use. Malformed existing state is never replaced with a new key.
  ObjectReply Ready(const StateAccess& access);
  // Trusted local administration only. These return IDs/revision/use counts,
  // never keys. Rotation requires an observed revision and explicit retire IDs;
  // retiring a key invalidates its outstanding opaque states.
  ObjectReply Describe(const StateAccess& access);
  ObjectReply Rotate(int64_t expected_revision,
                     const std::vector<std::string>& retire,
                     const StateAccess& access);
  ObjectReply Seal(std::string_view plaintext,
                   const StateCipherBinding& binding, const StateAccess& access,
                   std::string* opaque);
  ObjectReply Open(std::string_view opaque, const StateCipherBinding& binding,
                   const StateAccess& access, std::string* plaintext);

 private:
  ObjectStore& objects_;
};
}  // namespace q4t::server
