#include "q4t/server/application_payload.h"

#include <algorithm>
#include <charconv>
#include <set>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using Path = std::vector<std::string>;
constexpr uint64_t kMaximumString = 128ULL << 20;
constexpr size_t kMaximumReferences = 4096;
constexpr auto kFormat = "q4t.application_string.v1";
const std::string kField(kApplicationPayloadField);
ObjectReply Invalid() {
  return Error(503, "application_payload_invalid",
               "Private application payload is inconsistent");
}
ObjectReply Capacity() {
  return Error(413, "application_payload_capacity",
               "Application payload exceeds its resource budget");
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.scope.empty()) return Invalid();
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Application payload cancelled");
  if (Clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Application payload deadline expired");
  return {};
}
bool Limits(const ApplicationPayloadLimits& limits) {
  return limits.max_data_bytes && limits.max_data_bytes <= (1ULL << 30) &&
         limits.max_string_bytes && limits.max_string_bytes <= kMaximumString &&
         limits.max_staged_bytes && limits.max_staged_bytes <= (512ULL << 20) &&
         limits.inline_bytes >= 1024 &&
         limits.inline_bytes <= limits.max_string_bytes &&
         limits.max_references && limits.max_references <= kMaximumReferences;
}
bool Fields(const Json& value, std::initializer_list<std::string_view> names) {
  if (!value.IsObject() || value.object.size() != names.size()) return false;
  std::set<std::string_view> seen;
  for (const auto& [name, _] : value.object)
    if (std::find(names.begin(), names.end(), name) == names.end() ||
        !seen.insert(name).second)
      return false;
  return true;
}
bool Hex(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](unsigned char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
std::string Binding(std::string_view digest) {
  return Digest(std::string(kFormat) + ":" + std::string(digest));
}
bool Charge(uint64_t amount, uint64_t* remaining) {
  if (amount > *remaining) return false;
  *remaining -= amount;
  return true;
}
struct Candidate {
  Path path;
  const Json* value;
};
bool Scan(const Json& value, const ApplicationPayloadLimits& limits, Path* path,
          uint64_t* remaining, size_t* nodes, std::vector<Candidate>* strings) {
  if (path->size() > 64 || !*nodes || !Charge(32, remaining) ||
      !Charge(value.str.size(), remaining))
    return false;
  --*nodes;
  if (value.IsString()) {
    if (value.str.size() > limits.max_string_bytes) return false;
    if (value.str.size() > limits.inline_bytes) {
      if (strings->size() >= limits.max_references) return false;
      size_t path_bytes = 0;
      for (const auto& part : *path)
        if ((path_bytes += part.size()) > 4096) return false;
      strings->push_back({*path, &value});
    }
  }
  if (value.IsArray()) {
    for (size_t i = 0; i < value.array.size(); ++i) {
      path->push_back(std::to_string(i));
      if (!Scan(value.array[i], limits, path, remaining, nodes, strings))
        return false;
      path->pop_back();
    }
  }
  if (value.IsObject()) {
    std::set<std::string_view> names;
    for (const auto& [name, child] : value.object) {
      if (path->empty() && name == kField) continue;
      if (!names.insert(name).second || !Charge(name.size(), remaining) ||
          name.size() > 4096)
        return false;
      path->push_back(name);
      if (!Scan(child, limits, path, remaining, nodes, strings)) return false;
      path->pop_back();
    }
  }
  return true;
}
Json Clone(const Json& value, uint64_t threshold, bool root = true) {
  if (value.IsString() && value.str.size() > threshold) return {};
  if (value.IsArray()) {
    auto result = Array();
    result.array.reserve(value.array.size());
    for (const auto& child : value.array)
      result.array.push_back(Clone(child, threshold, false));
    return result;
  }
  if (value.IsObject()) {
    auto result = Object();
    for (const auto& [key, child] : value.object) {
      if (root && key == kField) continue;
      result.object.emplace_back(key, Clone(child, threshold, false));
    }
    return result;
  }
  return value;
}
template <typename Value>
Value* At(Value* value, const Path& path) {
  for (const auto& part : path) {
    if (value->IsObject()) {
      Value* found = nullptr;
      for (auto& [name, child] : value->object)
        if (name == part) {
          if (found) return nullptr;
          found = &child;
        }
      if (!found) return nullptr;
      value = found;
    } else if (value->IsArray()) {
      size_t index = 0;
      const auto [next, error] =
          std::from_chars(part.data(), part.data() + part.size(), index);
      if (error != std::errc{} || next != part.data() + part.size() ||
          (part.size() > 1 && part.front() == '0') ||
          index >= value->array.size())
        return nullptr;
      value = &value->array[index];
    } else {
      return nullptr;
    }
  }
  return value;
}
Json Description(const ApplicationStringRef& reference) {
  auto path = Array();
  for (const auto& part : reference.path) path.array.push_back(String(part));
  return Object({{"path", std::move(path)},
                 {"bytes", Number(reference.bytes)},
                 {"sha256", String(reference.sha256)},
                 {"snapshot", InputSnapshotReference(reference.snapshot)}});
}
InputSnapshotLimits Storage(const ApplicationPayloadLimits& limits) {
  return {.max_bytes = limits.max_string_bytes + 16384,
          .max_metadata_bytes = 16384,
          .max_parts = 1};
}
ObjectReply Read(ObjectStore& objects, const BinaryAccess& access,
                 const ApplicationStringRef& ref,
                 const ApplicationPayloadLimits& limits, std::string* bytes) {
  if (ref.bytes > limits.max_string_bytes) return Capacity();
  InputSnapshotStore store(objects);
  InputSnapshot snapshot;
  auto reply = store.Read(access, ref.snapshot, Binding(ref.sha256),
                          Storage(limits), &snapshot);
  if (!reply.ok()) return reply;
  const auto& metadata = snapshot.metadata;
  if (!Fields(metadata, {"format", "bytes", "sha256"}) ||
      metadata.GetString("format") != kFormat ||
      metadata.GetInt("bytes", -1) != static_cast<int64_t>(ref.bytes) ||
      metadata.GetString("sha256") != ref.sha256 ||
      snapshot.parts.size() != 1 || snapshot.parts[0].size() != ref.bytes ||
      Digest(snapshot.parts[0]) != ref.sha256)
    return Invalid();
  *bytes = std::move(snapshot.parts[0]);
  return {};
}
}  // namespace

bool ParseApplicationPayloadReferences(
    const Json& data, std::vector<ApplicationStringRef>* references) {
  if (!references || !data.IsObject()) return false;
  references->clear();
  if (std::count_if(data.object.begin(), data.object.end(),
                    [](const auto& item) { return item.first == kField; }) > 1)
    return false;
  const auto* manifest = data.Find(kField);
  if (!manifest) return true;
  if (!Fields(*manifest, {"version", "strings"}) ||
      manifest->GetInt("version") != 1)
    return false;
  const auto* strings = manifest->GetArray("strings");
  if (!strings || strings->array.empty() ||
      strings->array.size() > kMaximumReferences)
    return false;
  std::vector<ApplicationStringRef> parsed;
  std::set<Path> paths;
  for (const auto& item : strings->array) {
    if (!Fields(item, {"path", "bytes", "sha256", "snapshot"})) return false;
    const auto* path = item.GetArray("path");
    const auto bytes = item.GetInt("bytes", -1);
    ApplicationStringRef next;
    if (!path || path->array.empty() || path->array.size() > 64 || bytes <= 0 ||
        static_cast<uint64_t>(bytes) > kMaximumString ||
        !Hex(item.GetString("sha256")) ||
        !ParseInputSnapshotReference(*item.Find("snapshot"), &next.snapshot))
      return false;
    size_t path_bytes = 0;
    for (const auto& part : path->array) {
      if (!part.IsString() || (path_bytes += part.str.size()) > 4096)
        return false;
      next.path.push_back(part.str);
    }
    if (next.path.front() == kField || !paths.insert(next.path).second)
      return false;
    next.bytes = bytes;
    next.sha256 = item.GetString("sha256");
    parsed.push_back(std::move(next));
  }
  *references = std::move(parsed);
  return true;
}

ObjectReply RestoreApplicationPayload(ObjectStore& objects,
                                      const BinaryAccess& access,
                                      const Json& encoded,
                                      const ApplicationPayloadLimits& limits,
                                      Json* restored) {
  if (!restored || !Limits(limits)) return Invalid();
  *restored = {};
  auto reply = Active(access);
  if (!reply.ok()) return reply;
  std::vector<ApplicationStringRef> references;
  if (!ParseApplicationPayloadReferences(encoded, &references))
    return Invalid();
  if (references.size() > limits.max_references) return Capacity();
  uint64_t remaining = limits.max_data_bytes;
  size_t nodes = 262144;
  Path path;
  std::vector<Candidate> unused;
  if (!Scan(encoded, limits, &path, &remaining, &nodes, &unused))
    return Capacity();
  // Validate every target and total expansion BEFORE reading any snapshot.
  for (const auto& ref : references) {
    const auto* target = At(&encoded, ref.path);
    if (!target || (!target->IsNull() &&
                    (!target->IsString() || target->str.size() != ref.bytes ||
                     Digest(target->str) != ref.sha256)))
      return Invalid();
    if (ref.bytes > limits.max_string_bytes ||
        (target->IsNull() && !Charge(ref.bytes, &remaining)))
      return Capacity();
  }
  auto next = encoded;
  std::map<std::string, std::string> loaded;
  for (const auto& ref : references) {
    reply = Active(access);
    if (!reply.ok()) return reply;
    auto* target = At(&next, ref.path);
    if (!target->IsNull()) continue;
    const auto key = ref.snapshot.id + ":" + ref.snapshot.sha256;
    auto found = loaded.find(key);
    if (found == loaded.end()) {
      std::string bytes;
      reply = Read(objects, access, ref, limits, &bytes);
      if (!reply.ok()) return reply;
      found = loaded.emplace(key, std::move(bytes)).first;
    }
    // The same snapshot cannot be relabelled with a different length/hash.
    if (found->second.size() != ref.bytes ||
        Digest(found->second) != ref.sha256)
      return Invalid();
    *target = String(found->second);
  }
  reply = Active(access);
  if (reply.ok()) *restored = std::move(next);
  return reply;
}

ApplicationPayloads::ApplicationPayloads(ObjectStore& objects,
                                         BinaryAccess access,
                                         ApplicationPayloadLimits limits)
    : objects_(objects), access_(std::move(access)), limits_(limits) {}
ApplicationPayloads::~ApplicationPayloads() { Published(uncertain_); }

ObjectReply ApplicationPayloads::Encode(const Json& data, Json* encoded) {
  if (!encoded || !Limits(limits_) || published_) return Invalid();
  *encoded = {};
  if (uncertain_) return unknown_result_;
  auto reply = Active(access_);
  if (!reply.ok()) return reply;
  std::vector<ApplicationStringRef> previous;
  if (!ParseApplicationPayloadReferences(data, &previous)) return Invalid();
  uint64_t remaining = limits_.max_data_bytes;
  size_t nodes = 262144;
  Path path;
  std::vector<Candidate> strings;
  if (!Scan(data, limits_, &path, &remaining, &nodes, &strings))
    return Capacity();
  for (const auto& ref : previous) {
    const auto* target = At(&data, ref.path);
    // An encoded record must be restored before being written again.
    if (target && target->IsNull()) return Invalid();
  }
  auto next = Clone(data, limits_.inline_bytes);
  auto descriptions = Array();
  InputSnapshotStore store(objects_);
  for (const auto& candidate : strings) {
    reply = Active(access_);
    if (!reply.ok()) return reply;
    const auto& bytes = candidate.value->str;
    const auto digest = Digest(bytes);
    auto found = known_.find(digest);
    if (found == known_.end()) {
      const auto old =
          std::find_if(previous.begin(), previous.end(), [&](const auto& ref) {
            return ref.path == candidate.path && ref.bytes == bytes.size() &&
                   ref.sha256 == digest;
          });
      InputSnapshotRef reference;
      if (old != previous.end()) {
        // Atomic root pin protects reuse even if its previous parent is
        // concurrently deleted before our new parent is committed.
        InputSnapshotHold pin;
        reply = store.Pin(access_, old->snapshot, Binding(digest), 3600, &pin);
        if (!pin.id.empty()) holds_.push_back(std::move(pin));
        if (!reply.ok()) {
          uncertain_ = reply.commit_unknown;
          if (uncertain_) unknown_result_ = reply;
          return reply;
        }
        std::string verified;
        reply = Read(objects_, access_, *old, limits_, &verified);
        if (!reply.ok()) return reply;
        reference = old->snapshot;
      } else {
        if (bytes.size() > limits_.max_staged_bytes - staged_bytes_)
          return Capacity();
        staged_bytes_ += bytes.size();
        InputSnapshotHold hold;
        const std::string_view segment = bytes;
        reply = store.Save(access_, Binding(digest),
                           Object({{"format", String(kFormat)},
                                   {"bytes", Number(bytes.size())},
                                   {"sha256", String(digest)}}),
                           std::span(&segment, 1), Storage(limits_), 3600,
                           &reference, &hold);
        if (!hold.id.empty()) holds_.push_back(std::move(hold));
        if (!reply.ok()) {
          uncertain_ = reply.commit_unknown;
          if (uncertain_) unknown_result_ = reply;
          return reply;
        }
      }
      found = known_.emplace(digest, std::move(reference)).first;
    }
    descriptions.array.push_back(
        Description({candidate.path, bytes.size(), digest, found->second}));
  }
  if (!descriptions.array.empty())
    Set(&next, kField,
        Object({{"version", Number(1)}, {"strings", std::move(descriptions)}}));
  reply = Active(access_);
  if (reply.ok()) *encoded = std::move(next);
  return reply;
}

void ApplicationPayloads::Published(bool commit_unknown) noexcept {
  uncertain_ |= commit_unknown;
  published_ = true;
  if (uncertain_) return;
  try {
    const BinaryAccess cleanup{
        access_.scope, Clock::now() + std::chrono::milliseconds(250), {}};
    InputSnapshotStore store(objects_);
    for (auto& hold : holds_)
      if (!hold.released && !hold.commit_unknown &&
          Clock::now() < cleanup.deadline)
        store.Release(cleanup, &hold);
    holds_.clear();
  } catch (...) {
    // Holds have absolute expiry. Cleanup failure cannot undo publication.
  }
}
}  // namespace q4t::server
