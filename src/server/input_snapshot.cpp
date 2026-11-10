#include "q4t/server/input_snapshot.h"

#include <algorithm>
#include <array>
#include <ctime>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
bool Hex(std::string_view text) {
  return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
bool Limits(const InputSnapshotLimits& limits) {
  return limits.max_bytes > 0 && limits.max_bytes <= (512ULL << 20) &&
         limits.max_metadata_bytes > 0 &&
         limits.max_metadata_bytes <= (32ULL << 20) &&
         limits.max_metadata_bytes <= limits.max_bytes &&
         limits.max_parts > 0 && limits.max_parts <= 1024;
}
ObjectReply Invalid() {
  return Error(500, "input_snapshot_invalid", "Invalid private input snapshot");
}
ObjectReply Corrupt() {
  return Error(503, "input_snapshot_inconsistent",
               "Immutable input snapshot is inconsistent");
}
ObjectReply Capacity() {
  return Error(413, "input_snapshot_capacity", "Input snapshot exceeds budget");
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.scope.empty()) return Invalid();
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Input snapshot cancelled");
  if (Clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Input snapshot timed out");
  return {};
}
bool MetadataSize(const Json& value, uint64_t* remaining, size_t* values,
                  size_t depth = 0) {
  if (depth > 64 || !*values || *remaining < 4) return false;
  --*values;
  *remaining -= 4;
  if (value.str.size() > *remaining) return false;
  *remaining -= value.str.size();
  for (const auto& item : value.array)
    if (!MetadataSize(item, remaining, values, depth + 1)) return false;
  for (const auto& [key, item] : value.object) {
    if (key.size() > *remaining) return false;
    *remaining -= key.size();
    if (!MetadataSize(item, remaining, values, depth + 1)) return false;
  }
  return true;
}
BinaryAccess Cleanup(const BinaryAccess& access) {
  return {access.scope, Clock::now() + std::chrono::milliseconds(250), {}};
}
ObjectRequest Request(const BinaryAccess& access, ObjectKind kind,
                      const std::string& id) {
  ObjectRequest result;
  result.scope = access.scope;
  result.kind = kind;
  result.id = id;
  result.operation = ObjectOperation::kPut;
  result.ttl_seconds = 0;
  return result;
}
}  // namespace

Json InputSnapshotReference(const InputSnapshotRef& reference) {
  return Object(
      {{"id", String(reference.id)}, {"sha256", String(reference.sha256)}});
}
bool ParseInputSnapshotReference(const Json& value,
                                 InputSnapshotRef* reference) {
  if (!reference || !value.IsObject() || value.object.size() != 2 ||
      !value.GetString("id").starts_with("input_snapshot_") ||
      !ValidId(value.GetString("id")) || !Hex(value.GetString("sha256")))
    return false;
  *reference = {value.GetString("id"), value.GetString("sha256")};
  return true;
}

ObjectReply InputSnapshotStore::Save(
    const BinaryAccess& access, std::string_view binding_sha256,
    const Json& metadata, std::span<const std::string_view> parts,
    const InputSnapshotLimits& limits, int hold_seconds,
    InputSnapshotRef* reference, InputSnapshotHold* hold) {
  if (hold && hold->commit_unknown)
    return Error(503, "input_snapshot_commit_unknown",
                 "Inspect the original snapshot; do not replay", true);
  auto result = Active(access);
  if (!result.ok()) return result;
  if (!reference || !hold || !reference->id.empty() || !hold->id.empty() ||
      !Hex(binding_sha256) || !Limits(limits) || !metadata.IsObject() ||
      hold_seconds < 1 || hold_seconds > 3600)
    return Invalid();
  uint64_t remaining = limits.max_metadata_bytes;
  size_t values = 2 * 1024 * 1024;
  if (parts.size() > limits.max_parts ||
      !MetadataSize(metadata, &remaining, &values))
    return Capacity();
  auto text = OutputJson(metadata);
  if (text.size() > limits.max_metadata_bytes) return Capacity();
  std::vector<std::string_view> segments{text};
  segments.insert(segments.end(), parts.begin(), parts.end());
  uint64_t bytes = 0;
  auto description = Array();
  for (const auto segment : segments) {
    if (segment.size() > limits.max_bytes - bytes) return Capacity();
    bytes += segment.size();
    description.array.push_back(Object({{"bytes", Number(segment.size())},
                                        {"sha256", String(Digest(segment))}}));
  }
  auto root = Request(access, ObjectKind::kInputSnapshot,
                      NewObjectId(ObjectKind::kInputSnapshot));
  auto lease =
      Request(access, ObjectKind::kToolRun, NewObjectId(ObjectKind::kToolRun));
  if (root.id.empty() || lease.id.empty())
    return Error(503, "object_entropy_unavailable", "No secure snapshot ID");
  // The hold must cover the operation's whole remaining budget. Its deadline
  // is absolute and never refreshed by a codec/read or process restart.
  lease.expires_at = std::time(nullptr) + hold_seconds;
  if (access.deadline > Clock::now() + std::chrono::seconds(hold_seconds))
    return Invalid();
  lease.data = Object({{"format", String("q4t.input_snapshot_hold.v1")},
                       {"snapshot_id", String(root.id)}});
  root.mutate_before = lease.mutate_before = lease.expires_at;
  root.data = Object({{"format", String("q4t.input_snapshot.v1")},
                      {"binding_sha256", String(std::string(binding_sha256))},
                      {"segments", std::move(description)}});
  BinaryStage stage;
  result = binary_.Begin(access, bytes, hold_seconds, &stage);
  if (!result.ok()) {
    if (result.commit_unknown)
      *hold = {access.scope, stage.id, stage.expires, false, true};
    return result;
  }
  bool publishing = false;
  const auto save = [&]() -> ObjectReply {
    for (auto segment : segments)
      while (!segment.empty()) {
        auto active = Active(access);
        if (!active.ok()) return active;
        const auto count =
            std::min<uint64_t>(segment.size(), kBinaryChunkBytes);
        auto written = binary_.Append(access, &stage, segment.substr(0, count));
        if (!written.ok()) return written;
        segment.remove_prefix(count);
      }
    Set(&root.data, "blob", BinaryManifestJson(stage.manifest));
    *reference = {root.id, Digest(OutputJson(root.data))};
    *hold = {access.scope, lease.id, lease.expires_at, false, true};
    publishing = true;
    return binary_.Publish(access, &stage, root, std::array{lease});
  };
  result = save();
  if (result.ok()) {
    hold->commit_unknown = false;
    return result;
  }
  if (result.commit_unknown) {
    if (!publishing)
      *hold = {access.scope, stage.id, stage.expires, false, true};
    return result;
  }
  *reference = {};
  *hold = {};
  const auto aborted = binary_.Abort(Cleanup(access), &stage);
  if (!aborted.ok()) return aborted;
  return result;
}

ObjectReply InputSnapshotStore::Read(const BinaryAccess& access,
                                     const InputSnapshotRef& reference,
                                     std::string_view binding_sha256,
                                     const InputSnapshotLimits& limits,
                                     InputSnapshot* output) {
  if (!output) return Invalid();
  *output = {};
  InputSnapshotRef validated;
  if (!Limits(limits) || !Hex(binding_sha256) ||
      !ParseInputSnapshotReference(InputSnapshotReference(reference),
                                   &validated))
    return Invalid();
  auto result = Active(access);
  if (!result.ok()) return result;
  BinaryRead read;
  Json root;
  result = binary_.Open(access, ObjectKind::kInputSnapshot, reference.id, 3600,
                        &read, &root);
  if (!result.ok()) return result;
  InputSnapshot next;
  const auto load = [&]() -> ObjectReply {
    if (Digest(OutputJson(root)) != reference.sha256 ||
        root.GetString("binding_sha256") != binding_sha256)
      return Corrupt();
    const auto* segments = root.GetArray("segments");
    if (!segments || segments->array.empty() ||
        segments->array.size() > limits.max_parts + 1 ||
        read.manifest.bytes > limits.max_bytes)
      return Capacity();
    uint64_t bytes = 0;
    // Validate all allocation sizes before reading any content.
    for (size_t i = 0; i < segments->array.size(); ++i) {
      const auto& segment = segments->array[i];
      const auto size = segment.GetInt("bytes", -1);
      if (size < 0 || !Hex(segment.GetString("sha256"))) return Corrupt();
      if (static_cast<uint64_t>(size) > limits.max_bytes - bytes ||
          (i == 0 && static_cast<uint64_t>(size) > limits.max_metadata_bytes))
        return Capacity();
      bytes += size;
    }
    if (bytes != read.manifest.bytes) return Corrupt();
    size_t chunk = 0;
    for (size_t i = 0; i < segments->array.size(); ++i) {
      const auto& segment = segments->array[i];
      const uint64_t size = segment.GetInt("bytes");
      std::string content;
      content.reserve(size);
      while (content.size() < size) {
        if (chunk >= read.manifest.chunks.size() ||
            read.manifest.chunks[chunk].bytes > size - content.size())
          return Corrupt();
        std::string bytes;
        auto loaded = binary_.Read(access, read, chunk++, &bytes);
        if (!loaded.ok()) return loaded;
        content += bytes;
      }
      if (Digest(content) != segment.GetString("sha256")) return Corrupt();
      if (i == 0) {
        io::JsonParseLimits parse;
        parse.max_depth = 64;
        parse.max_values = 2 * 1024 * 1024;
        parse.max_string_bytes = limits.max_metadata_bytes;
        parse.reject_duplicate_keys = true;
        parse.allow_exact_number_text = true;
        if (!io::ParseJson(content, &next.metadata, true, parse) ||
            !next.metadata.IsObject())
          return Corrupt();
      } else {
        next.parts.push_back(std::move(content));
      }
    }
    if (chunk != read.manifest.chunks.size()) return Corrupt();
    return Active(access);
  };
  result = load();
  const auto closed = binary_.Close(Cleanup(access), &read);
  if (!closed.ok()) return closed;
  if (result.ok()) *output = std::move(next);
  return result;
}

ObjectReply InputSnapshotStore::Pin(const BinaryAccess& access,
                                    const InputSnapshotRef& reference,
                                    std::string_view binding_sha256,
                                    int hold_seconds, InputSnapshotHold* hold) {
  if (hold && hold->commit_unknown)
    return Error(503, "input_snapshot_commit_unknown",
                 "Inspect the original snapshot; do not replay", true);
  InputSnapshotRef checked;
  if (!hold || !hold->id.empty() || !Hex(binding_sha256) ||
      !ParseInputSnapshotReference(InputSnapshotReference(reference),
                                   &checked) ||
      hold_seconds < 1 || hold_seconds > 3600 ||
      access.deadline > Clock::now() + std::chrono::seconds(hold_seconds))
    return Invalid();
  auto active = Active(access);
  if (!active.ok()) return active;
  auto root = Request(access, ObjectKind::kInputSnapshot, reference.id);
  root.operation = ObjectOperation::kGet;
  auto stored = objects_.Execute(root, access.deadline, access.cancelled);
  if (!stored.ok()) return stored;
  const auto* data = stored.value.Find("data");
  if (!data || data->GetString("format") != "q4t.input_snapshot.v1" ||
      data->GetString("binding_sha256") != binding_sha256 ||
      Digest(OutputJson(*data)) != reference.sha256)
    return Corrupt();
  auto lease =
      Request(access, ObjectKind::kToolRun, NewObjectId(ObjectKind::kToolRun));
  if (lease.id.empty())
    return Error(503, "object_entropy_unavailable", "No secure snapshot ID");
  lease.expires_at = std::time(nullptr) + hold_seconds;
  lease.mutate_before = root.mutate_before = lease.expires_at;
  lease.data = Object({{"format", String("q4t.input_snapshot_hold.v1")},
                       {"snapshot_id", String(root.id)}});
  root.operation = ObjectOperation::kPut;
  root.data = *data;
  root.expected_revision = stored.value.GetInt("revision");
  root.created_at = stored.value.GetInt("created_at");
  root.expires_at = stored.value.GetInt("expires_at");
  const auto reply = objects_.ExecuteBatch(std::array{root, lease},
                                           access.deadline, access.cancelled);
  if (reply.ok() || reply.commit_unknown)
    *hold = {access.scope, lease.id, lease.expires_at, false,
             reply.commit_unknown};
  return reply;
}

ObjectReply InputSnapshotStore::Release(const BinaryAccess& access,
                                        InputSnapshotHold* hold) {
  if (!hold || hold->scope != access.scope || hold->released ||
      !ValidId(hold->id))
    return Invalid();
  if (hold->commit_unknown)
    return Error(503, "input_snapshot_commit_unknown",
                 "Inspect the original snapshot; do not replay", true);
  auto result = Active(access);
  if (!result.ok()) return result;
  auto request = Request(access, ObjectKind::kToolRun, hold->id);
  request.operation = ObjectOperation::kGet;
  result = objects_.Execute(request, access.deadline, access.cancelled);
  if (result.http_status == 404) {
    hold->released = true;
    return {};
  }
  if (!result.ok()) return result;
  const auto* data = result.value.Find("data");
  if (!data || data->GetString("format") != "q4t.input_snapshot_hold.v1" ||
      result.value.GetInt("expires_at") != hold->expires_at)
    return Corrupt();
  request.operation = ObjectOperation::kErase;
  request.expected_revision = result.value.GetInt("revision");
  result = objects_.Execute(request, access.deadline, access.cancelled);
  hold->released = result.ok();
  hold->commit_unknown = result.commit_unknown;
  return result;
}
}  // namespace q4t::server
