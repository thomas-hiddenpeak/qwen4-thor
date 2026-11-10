// Bounded binary persistence on the isolated, quota-accounted object worker.
#pragma once

#include <vector>

#include "q4t/server/object_store.h"

namespace q4t::server {
inline constexpr uint64_t kBinaryChunkBytes = 1ULL << 20;
inline constexpr uint64_t kBinaryMaxBytes = 8ULL << 30;
inline constexpr size_t kBinaryMaxChunks = 16384;

struct BinaryAccess {
  // Supplied by authenticated server context, never an HTTP body field.
  std::string scope;
  std::chrono::steady_clock::time_point deadline;
  std::function<bool()> cancelled;
};
struct BinaryChunk {
  std::string id, sha256;
  uint64_t bytes = 0;
};
struct BinaryManifest {
  uint64_t bytes = 0;
  std::vector<BinaryChunk> chunks;
};
io::Json BinaryManifestJson(const BinaryManifest& manifest);
bool ParseBinaryManifest(const io::Json& value, BinaryManifest* manifest);

struct BinaryStage {
  std::string scope, id;
  int64_t revision = 0, created = 0, expires = 0;
  uint64_t max_bytes = 0;
  BinaryManifest manifest;
  bool closed = false, commit_unknown = false;
};
struct BinaryRead {
  std::string scope, id;
  int64_t expires = 0;
  BinaryManifest manifest;
  bool closed = false, commit_unknown = false;
};

// Callers coalesce transport fragments into <=1 MiB chunks. Each append
// atomically publishes a private chunk and its staging reference. Public
// resources only become visible after their caller validates the entire wire
// message and calls Publish. A lost receipt is never automatically replayed.
class BinaryStore {
 public:
  explicit BinaryStore(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Begin(const BinaryAccess& access, uint64_t max_bytes,
                    int lifetime_seconds, BinaryStage* stage);
  ObjectReply Append(const BinaryAccess& access, BinaryStage* stage,
                     std::string_view bytes);
  // Root is a known file, upload-part or private input snapshot. Its blob is
  // from the staged manifest; optional parent mutations share the same commit.
  ObjectReply Publish(const BinaryAccess& access, BinaryStage* stage,
                      ObjectRequest root,
                      std::span<const ObjectRequest> parents = {});
  ObjectReply Abort(const BinaryAccess& access, BinaryStage* stage);
  // Pin a durable root's exact manifest while it is read. Deletion of the
  // public file after Open cannot invalidate an active bounded download.
  ObjectReply Open(const BinaryAccess& access, ObjectKind kind,
                   const std::string& id, int lifetime_seconds,
                   BinaryRead* read, io::Json* pinned_data = nullptr);
  ObjectReply Read(const BinaryAccess& access, const BinaryRead& read,
                   size_t chunk, std::string* bytes);
  ObjectReply Close(const BinaryAccess& access, BinaryRead* read);

 private:
  ObjectStore& objects_;
};
}  // namespace q4t::server
