// Private immutable input storage; never a client-addressable Files resource.
#pragma once

#include "q4t/server/binary_store.h"

namespace q4t::server {
struct InputSnapshotLimits {
  uint64_t max_bytes = 128ULL << 20;
  uint64_t max_metadata_bytes = 16ULL << 20;
  size_t max_parts = 1024;
};
struct InputSnapshotRef {
  std::string id, sha256;
};
struct InputSnapshotHold {
  std::string scope, id;
  int64_t expires_at = 0;
  bool released = false, commit_unknown = false;
};
struct InputSnapshot {
  io::Json metadata;
  std::vector<std::string> parts;
};
io::Json InputSnapshotReference(const InputSnapshotRef& reference);
bool ParseInputSnapshotReference(const io::Json& value,
                                 InputSnapshotRef* reference);

// Private content uses the existing quota-accounted 1 MiB binary chunks.
// A bounded hold protects preparation; durable application records must retain
// the reference before releasing it. Crashes leave only an expiring hold.
// No re-fetch or codec execution occurs on Read. Unknown commits are never
// replayed or "cleaned up" speculatively.
class InputSnapshotStore {
 public:
  explicit InputSnapshotStore(ObjectStore& objects)
      : binary_(objects), objects_(objects) {}
  ObjectReply Save(const BinaryAccess& access, std::string_view binding_sha256,
                   const io::Json& metadata,
                   std::span<const std::string_view> parts,
                   const InputSnapshotLimits& limits, int hold_seconds,
                   InputSnapshotRef* reference, InputSnapshotHold* hold);
  ObjectReply Read(const BinaryAccess& access,
                   const InputSnapshotRef& reference,
                   std::string_view binding_sha256,
                   const InputSnapshotLimits& limits, InputSnapshot* output);
  // Atomically retain the snapshot root while a different durable parent is
  // prepared. A binary read lease alone only protects chunks, not this root.
  ObjectReply Pin(const BinaryAccess& access, const InputSnapshotRef& reference,
                  std::string_view binding_sha256, int hold_seconds,
                  InputSnapshotHold* hold);
  ObjectReply Release(const BinaryAccess& access, InputSnapshotHold* hold);

 private:
  BinaryStore binary_;
  ObjectStore& objects_;
};
}  // namespace q4t::server
