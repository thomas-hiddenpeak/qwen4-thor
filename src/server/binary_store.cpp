#include "q4t/server/binary_store.h"

#include <algorithm>
#include <array>
#include <ctime>
#include <unordered_set>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr std::string_view kBase64 =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string Encode(std::string_view bytes) {
  std::string out;
  out.reserve((bytes.size() + 2) / 3 * 4);
  for (size_t i = 0; i < bytes.size(); i += 3) {
    const auto a = static_cast<uint8_t>(bytes[i]);
    const auto b =
        i + 1 < bytes.size() ? static_cast<uint8_t>(bytes[i + 1]) : 0;
    const auto c =
        i + 2 < bytes.size() ? static_cast<uint8_t>(bytes[i + 2]) : 0;
    out += kBase64[a >> 2];
    out += kBase64[((a & 3) << 4) | (b >> 4)];
    out += i + 1 < bytes.size() ? kBase64[((b & 15) << 2) | (c >> 6)] : '=';
    out += i + 2 < bytes.size() ? kBase64[c & 63] : '=';
  }
  return out;
}
bool Decode(std::string_view text, uint64_t expected, std::string* out) {
  if (!expected || expected > kBinaryChunkBytes ||
      text.size() != (expected + 2) / 3 * 4)
    return false;
  std::string decoded;
  decoded.reserve(expected);
  for (size_t i = 0; i < text.size(); i += 4) {
    uint32_t packed = 0;
    for (size_t j = 0; j < 4; ++j) {
      const auto c = text[i + j];
      const auto digit = kBase64.find(c);
      if (digit == kBase64.npos && c != '=') return false;
      packed = (packed << 6) | (c == '=' ? 0 : digit);
    }
    for (int shift : {16, 8, 0})
      if (decoded.size() < expected)
        decoded += static_cast<char>((packed >> shift) & 255);
  }
  // Enforces canonical padding and unused bits, including '=' placement.
  if (Encode(decoded) != text) return false;
  *out = std::move(decoded);
  return true;
}
bool Hex(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
ObjectReply Invalid(std::string message) {
  return Error(400, "invalid_binary_operation", std::move(message));
}
ObjectReply Corrupt() {
  return Error(503, "binary_state_invalid",
               "Stored binary content is incomplete or inconsistent");
}
ObjectReply Access(const BinaryAccess& access) {
  if (access.scope.empty()) return Invalid("Missing authenticated scope");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(504, "binary_deadline_exceeded", "Binary IO deadline expired");
  if (access.cancelled && access.cancelled())
    return Error(499, "binary_cancelled", "Binary IO cancelled");
  return {};
}
ObjectReply State(const BinaryAccess& access, const std::string& scope,
                  const std::string& id, int64_t expires, bool closed,
                  bool unknown) {
  auto checked = Access(access);
  if (!checked.ok()) return checked;
  if (scope != access.scope || !ValidId(id) || closed)
    return Invalid("Binary handle is closed or belongs to another scope");
  if (unknown) {
    auto reply = Error(503, "binary_commit_unknown",
                       "Inspect the original resource; do not replay", true);
    reply.value = Object({{"id", String(id)}});
    return reply;
  }
  if (expires <= std::time(nullptr))
    return Error(410, "binary_handle_expired", "Binary IO handle expired");
  return {};
}
ObjectRequest Request(const BinaryAccess& access, const std::string& id,
                      ObjectOperation operation = ObjectOperation::kPut) {
  ObjectRequest request;
  request.operation = operation;
  request.kind = ObjectKind::kToolRun;
  request.scope = access.scope;
  request.id = id;
  request.ttl_seconds = 0;
  return request;
}
ObjectRequest StageRequest(const BinaryAccess& access, const BinaryStage& s) {
  auto request = Request(access, s.id);
  request.expected_revision = s.revision;
  request.created_at = s.created;
  request.expires_at = s.expires;
  request.data = Object({{"format", String("q4t.binary_stage.v1")},
                         {"max_bytes", Number(s.max_bytes)},
                         {"blob", BinaryManifestJson(s.manifest)}});
  return request;
}
void Identity(ObjectReply* reply, const std::string& id,
              const std::string& stage = {}) {
  if (!reply->value.IsObject()) reply->value = Object({});
  Set(&reply->value, "id", String(id));
  if (!stage.empty()) Set(&reply->value, "stage_id", String(stage));
}
bool Root(const ObjectRequest& root) {
  const auto format = root.data.GetString("format");
  return (root.kind == ObjectKind::kFile && format == "q4t.file.v1") ||
         (root.kind == ObjectKind::kBatch && format == "q4t.batch.v1") ||
         (root.kind == ObjectKind::kToolRun &&
          (format == "q4t.upload_part.v1" || format == "q4t.batch_result.v1")) ||
         (root.kind == ObjectKind::kInputSnapshot &&
          format == "q4t.input_snapshot.v1");
}
}  // namespace

Json BinaryManifestJson(const BinaryManifest& manifest) {
  auto chunks = Array();
  for (const auto& chunk : manifest.chunks)
    chunks.array.push_back(Object({{"id", String(chunk.id)},
                                   {"bytes", Number(chunk.bytes)},
                                   {"sha256", String(chunk.sha256)}}));
  return Object(
      {{"bytes", Number(manifest.bytes)}, {"chunks", std::move(chunks)}});
}
bool ParseBinaryManifest(const Json& value, BinaryManifest* manifest) {
  const auto bytes = value.GetInt("bytes", -1);
  const auto* chunks = value.GetArray("chunks");
  if (!value.IsObject() || bytes < 0 ||
      static_cast<uint64_t>(bytes) > kBinaryMaxBytes || !chunks ||
      chunks->array.size() > kBinaryMaxChunks)
    return false;
  BinaryManifest parsed;
  std::unordered_set<std::string> ids;
  for (const auto& chunk : chunks->array) {
    const auto id = chunk.GetString("id"), hash = chunk.GetString("sha256");
    const auto count = chunk.GetInt("bytes", -1);
    if (!chunk.IsObject() || !ValidId(id) || !Hex(hash) || count <= 0 ||
        static_cast<uint64_t>(count) > kBinaryChunkBytes ||
        !ids.insert(id).second ||
        parsed.bytes > kBinaryMaxBytes - static_cast<uint64_t>(count))
      return false;
    parsed.bytes += count;
    parsed.chunks.push_back({id, hash, static_cast<uint64_t>(count)});
  }
  if (parsed.bytes != static_cast<uint64_t>(bytes)) return false;
  *manifest = std::move(parsed);
  return true;
}

ObjectReply BinaryStore::Begin(const BinaryAccess& access, uint64_t max_bytes,
                               int lifetime_seconds, BinaryStage* stage) {
  auto checked = Access(access);
  if (!checked.ok()) return checked;
  if (!stage || !stage->id.empty() || max_bytes > kBinaryMaxBytes ||
      lifetime_seconds < 1 || lifetime_seconds > 3600)
    return Invalid("Invalid binary staging capacity or lifetime");
  BinaryStage next;
  next.scope = access.scope;
  next.id = NewObjectId(ObjectKind::kToolRun);
  if (next.id.empty())
    return Error(503, "object_entropy_unavailable", "No secure resource ID");
  next.created = std::time(nullptr);
  next.expires = next.created + lifetime_seconds;
  next.max_bytes = max_bytes;
  auto reply = objects_.Execute(StageRequest(access, next), access.deadline,
                                access.cancelled);
  Identity(&reply, next.id);
  if (reply.ok() || reply.commit_unknown) {
    next.revision = 1;
    next.commit_unknown = reply.commit_unknown;
    *stage = std::move(next);
  }
  return reply;
}
ObjectReply BinaryStore::Append(const BinaryAccess& access, BinaryStage* stage,
                                std::string_view bytes) {
  if (!stage) return Invalid("Missing staging handle");
  auto checked = State(access, stage->scope, stage->id, stage->expires,
                       stage->closed, stage->commit_unknown);
  if (!checked.ok()) return checked;
  if (bytes.empty() || bytes.size() > kBinaryChunkBytes ||
      stage->manifest.chunks.size() >= kBinaryMaxChunks ||
      stage->manifest.bytes > stage->max_bytes ||
      bytes.size() > stage->max_bytes - stage->manifest.bytes)
    return Error(413, "binary_capacity_exceeded", "Binary capacity exceeded");
  auto chunk = Request(access, NewObjectId(ObjectKind::kToolRun));
  if (chunk.id.empty())
    return Error(503, "object_entropy_unavailable", "No secure chunk ID");
  const auto digest = Digest(bytes);
  chunk.data = Object({{"format", String("q4t.binary_chunk.v1")},
                       {"bytes", Number(bytes.size())},
                       {"sha256", String(digest)},
                       {"base64", String(Encode(bytes))}});
  auto next = *stage;
  next.manifest.bytes += bytes.size();
  next.manifest.chunks.push_back({chunk.id, digest, bytes.size()});
  std::array requests{std::move(chunk), StageRequest(access, next)};
  auto reply =
      objects_.ExecuteBatch(requests, access.deadline, access.cancelled);
  Identity(&reply, stage->id);
  Set(&reply.value, "chunk_id", String(next.manifest.chunks.back().id));
  if (reply.ok()) {
    ++next.revision;
    *stage = std::move(next);
  } else if (reply.commit_unknown) {
    stage->commit_unknown = true;
  }
  return reply;
}
ObjectReply BinaryStore::Publish(const BinaryAccess& access, BinaryStage* stage,
                                 ObjectRequest root,
                                 std::span<const ObjectRequest> parents) {
  if (!stage) return Invalid("Missing staging handle");
  auto checked = State(access, stage->scope, stage->id, stage->expires,
                       stage->closed, stage->commit_unknown);
  if (!checked.ok()) return checked;
  if (root.operation != ObjectOperation::kPut || root.expected_revision ||
      root.scope != access.scope || !ValidId(root.id) || root.id == stage->id ||
      !Root(root) || parents.size() > 128)
    return Invalid("Invalid binary publication root");
  for (const auto& parent : parents)
    if (parent.scope != access.scope || parent.id == stage->id ||
        parent.id == root.id)
      return Invalid("Invalid binary publication parent");
  Set(&root.data, "blob", BinaryManifestJson(stage->manifest));
  auto erase = Request(access, stage->id, ObjectOperation::kErase);
  erase.expected_revision = stage->revision;
  const auto id = root.id;
  std::vector requests{std::move(root), std::move(erase)};
  requests.insert(requests.end(), parents.begin(), parents.end());
  auto reply =
      objects_.ExecuteBatch(requests, access.deadline, access.cancelled);
  Identity(&reply, id, stage->id);
  if (reply.ok()) stage->closed = true;
  if (reply.commit_unknown) stage->commit_unknown = true;
  return reply;
}
ObjectReply BinaryStore::Abort(const BinaryAccess& access, BinaryStage* stage) {
  if (!stage) return Invalid("Missing staging handle");
  auto checked = State(access, stage->scope, stage->id, stage->expires,
                       stage->closed, stage->commit_unknown);
  if (!checked.ok()) return checked;
  auto erase = Request(access, stage->id, ObjectOperation::kErase);
  erase.expected_revision = stage->revision;
  auto reply = objects_.Execute(erase, access.deadline, access.cancelled);
  Identity(&reply, stage->id);
  if (reply.ok()) stage->closed = true;
  if (reply.commit_unknown) stage->commit_unknown = true;
  return reply;
}
ObjectReply BinaryStore::Open(const BinaryAccess& access, ObjectKind kind,
                              const std::string& id, int lifetime_seconds,
                              BinaryRead* read, Json* pinned_data) {
  if (pinned_data) *pinned_data = {};
  auto checked = Access(access);
  if (!checked.ok()) return checked;
  if (!read || !read->id.empty() || lifetime_seconds < 1 ||
      lifetime_seconds > 3600 ||
      (kind != ObjectKind::kFile && kind != ObjectKind::kToolRun &&
       kind != ObjectKind::kBatch &&
       kind != ObjectKind::kInputSnapshot))
    return Invalid("Invalid binary read capacity or lifetime");
  auto request = Request(access, id, ObjectOperation::kGet);
  request.kind = kind;
  auto stored = objects_.Execute(request, access.deadline, access.cancelled);
  if (!stored.ok()) return stored;
  const auto* data = stored.value.Find("data");
  const auto* blob = data ? data->Find("blob") : nullptr;
  BinaryRead next;
  if (!data || !blob || !ParseBinaryManifest(*blob, &next.manifest))
    return Corrupt();
  request.data = *data;
  if (!Root(request)) return Corrupt();
  request.operation = ObjectOperation::kPut;
  request.expected_revision = stored.value.GetInt("revision");
  request.created_at = stored.value.GetInt("created_at");
  request.expires_at = stored.value.GetInt("expires_at");
  next.scope = access.scope;
  next.expires = std::time(nullptr) + lifetime_seconds;
  next.id = NewObjectId(ObjectKind::kToolRun);
  if (next.id.empty())
    return Error(503, "object_entropy_unavailable", "No secure read ID");
  auto lease = Request(access, next.id);
  lease.expires_at = next.expires;
  lease.data = Object({{"format", String("q4t.binary_read.v1")},
                       {"blob", BinaryManifestJson(next.manifest)}});
  // CAS the source in the same transaction: deletion/expiry between the read
  // and pin cannot publish a dangling reader after unreferenced chunks retire.
  std::array requests{std::move(request), std::move(lease)};
  auto reply =
      objects_.ExecuteBatch(requests, access.deadline, access.cancelled);
  Identity(&reply, next.id);
  if (reply.ok() || reply.commit_unknown) {
    next.commit_unknown = reply.commit_unknown;
    *read = std::move(next);
  }
  if (reply.ok() && pinned_data) *pinned_data = *data;
  return reply;
}
ObjectReply BinaryStore::Read(const BinaryAccess& access,
                              const BinaryRead& read, size_t chunk,
                              std::string* bytes) {
  if (!bytes) return Invalid("Missing binary read buffer");
  bytes->clear();
  auto checked = State(access, read.scope, read.id, read.expires, read.closed,
                       read.commit_unknown);
  if (!checked.ok()) return checked;
  if (chunk >= read.manifest.chunks.size())
    return Invalid("Invalid binary chunk index");
  const auto& entry = read.manifest.chunks[chunk];
  auto reply =
      objects_.Execute(Request(access, entry.id, ObjectOperation::kGet),
                       access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  const auto* data = reply.value.Find("data");
  std::string decoded;
  if (!data || data->GetString("format") != "q4t.binary_chunk.v1" ||
      data->GetInt("bytes", -1) != static_cast<int64_t>(entry.bytes) ||
      data->GetString("sha256") != entry.sha256 ||
      !Decode(data->GetString("base64"), entry.bytes, &decoded) ||
      Digest(decoded) != entry.sha256)
    return Corrupt();
  *bytes = std::move(decoded);
  return {};
}
ObjectReply BinaryStore::Close(const BinaryAccess& access, BinaryRead* read) {
  if (!read) return Invalid("Missing binary read handle");
  auto checked = State(access, read->scope, read->id, read->expires,
                       read->closed, read->commit_unknown);
  if (!checked.ok()) return checked;
  auto reply =
      objects_.Execute(Request(access, read->id, ObjectOperation::kErase),
                       access.deadline, access.cancelled);
  Identity(&reply, read->id);
  if (reply.ok()) read->closed = true;
  if (reply.commit_unknown) read->commit_unknown = true;
  return reply;
}
}  // namespace q4t::server
