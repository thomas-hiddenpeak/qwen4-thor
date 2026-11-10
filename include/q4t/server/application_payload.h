// Exact large application strings in private, quota-accounted binary chunks.
#pragma once

#include <map>

#include "q4t/server/input_snapshot.h"

namespace q4t::server {
inline constexpr std::string_view kApplicationPayloadField =
    "_q4t_application_payload";
struct ApplicationPayloadLimits {
  uint64_t max_data_bytes = 512ULL << 20;
  uint64_t max_string_bytes = 96ULL << 20;
  uint64_t max_staged_bytes = 256ULL << 20;
  uint64_t inline_bytes = 256ULL << 10;
  size_t max_references = 4096;
};
struct ApplicationStringRef {
  std::vector<std::string> path;
  uint64_t bytes = 0;
  std::string sha256;
  InputSnapshotRef snapshot;
};
// Validate private metadata without loading content, including for retention.
bool ParseApplicationPayloadReferences(
    const io::Json& data, std::vector<ApplicationStringRef>* references);
// Fast ordinary-object path. Complex/oversized trees select full validation.
bool NeedsApplicationPayload(const io::Json& data);
// Logical expanded charge, including structure, validated without storage IO.
ObjectReply MeasureApplicationPayload(const io::Json& data,
                                      const ApplicationPayloadLimits& limits,
                                      uint64_t* bytes);

// Restore is transactional. Its output remains INTERNAL application data:
// retain the private manifest for later metadata updates/reuse; API projections
// must never expose it. Files endpoints never expose these private snapshots.
ObjectReply RestoreApplicationPayload(ObjectStore& objects,
                                      const BinaryAccess& access,
                                      const io::Json& encoded,
                                      const ApplicationPayloadLimits& limits,
                                      io::Json* restored);
// ObjectStore's public-to-application projection. Restore all content and
// remove only the private root manifest. Lists retain order and cursor meaning
// while stopping before their aggregate expanded data budget is exceeded.
ObjectReply RestoreApplicationObjects(
    ObjectStore& objects, const BinaryAccess& access, bool list,
    io::Json* value, const ApplicationPayloadLimits& limits = {});

// One publication owner, optionally shared by all members of an atomic batch.
// Encoding preserves every original string byte; null placeholders exist only
// in the private durable representation. Consumers must restore before use.
class ApplicationPayloads {
 public:
  ApplicationPayloads(ObjectStore& objects, BinaryAccess access,
                      ApplicationPayloadLimits limits = {});
  ~ApplicationPayloads();
  ApplicationPayloads(const ApplicationPayloads&) = delete;
  ApplicationPayloads& operator=(const ApplicationPayloads&) = delete;
  // previous is a trusted durable representation for reuse after application
  // updates. Its old paths need not match a modified object's new structure.
  ObjectReply Encode(const io::Json& data, io::Json* encoded,
                     const io::Json* previous = nullptr);
  // Call only after publishing the parent(s). Unknown outcomes keep absolute
  // holds; bounded best-effort cleanup cannot turn committed success to error.
  void Published(bool commit_unknown) noexcept;

 private:
  ObjectStore& objects_;
  BinaryAccess access_;
  ApplicationPayloadLimits limits_;
  std::vector<InputSnapshotHold> holds_;
  std::map<std::string, InputSnapshotRef> known_;
  uint64_t staged_bytes_ = 0;
  ObjectReply unknown_result_;
  bool uncertain_ = false;
  bool published_ = false;
};
}  // namespace q4t::server
