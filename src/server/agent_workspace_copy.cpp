#include "agent_workspace_copy.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::environment_detail {
namespace {
using namespace object_detail;
constexpr uint64_t kSourceBytes = 50ULL << 20;
constexpr size_t kSmallCopyBytes = 256 << 10;
constexpr InputSnapshotLimits kStorage{110ULL << 20, 4096, 50};
constexpr std::string_view kFormat = "q4t.workspace_file.v1";

ObjectReply Corrupt() {
  return Error(503, "environment_file_state_invalid",
               "Invalid stored workspace file");
}
BinaryAccess Binary(const StateAccess& access) {
  return {access.scope, access.deadline, access.cancelled};
}
BinaryAccess Cleanup(const StateAccess& access) {
  return {access.scope,
          std::chrono::steady_clock::now() + std::chrono::seconds(1),
          {}};
}
std::string Binding(const std::string& workspace, const std::string& path,
                    const std::string& installed) {
  return Digest(
      OutputJson(Array({String(std::string(kFormat)), String(workspace),
                        String(path), String(installed)})));
}
StateCipherBinding Part(const std::string& binding, size_t index) {
  return {"q4t.agents", "workspace", "file_part",
          Digest(binding + "/" + std::to_string(index))};
}
}  // namespace

WorkspaceCopies::~WorkspaceCopies() {
  if (release_sources_) ReleaseSources();
  auto access = Cleanup(access_);
  InputSnapshotStore store(objects_);
  for (auto& hold : holds_) {
    if (hold.id.empty() || hold.commit_unknown) continue;
    try {
      store.Release(access, &hold);
    } catch (...) {
      // The worker independently expires the hold.
    }
  }
}
ObjectReply WorkspaceCopies::PinSources(const Json& files, Json* receipts,
                                       int64_t* deadline) {
  *receipts = Array();
  *deadline = std::time(nullptr) + 3600;
  for (size_t index = 0; index < files.array.size(); ++index) {
    const auto& file = files.array[index];
    if (file.GetString("type") != "file_id") continue;
    const auto id = file.GetString("file_id");
    if (sources_.contains(id)) continue;
    auto& pin = sources_[id];
    auto result = Files(objects_).Open(Binary(access_), id, &pin, nullptr,
                                       kSourceBytes, 3600);
    if (result.code == "file_input_capacity_exceeded") {
      auto invalid = Error(400, "invalid_request", "Files source exceeds 50 MiB");
      invalid.value = Object({{"param", String("environment.files[" +
          std::to_string(index) + "].file_id")}});
      return invalid;
    }
    if (!result.ok()) return result;
    *deadline = std::min(*deadline, pin.expires);
    receipts->array.push_back(
        Object({{"source", String(id)}, {"pin", String(pin.id)}}));
  }
  return {};
}

ObjectReply WorkspaceCopies::ResumeSources(const Json& receipts) {
  // The pending row owns the pins until completion commits. Losing a CAS
  // race must leave all of them available for the winning worker.
  release_sources_ = false;
  if (!receipts.IsArray() || receipts.array.empty() || receipts.array.size() > 50)
    return Corrupt();
  for (const auto& receipt : receipts.array) {
    const auto id = receipt.GetString("source");
    if (!id.starts_with("file_") || !ValidId(id) || sources_.contains(id))
      return Corrupt();
    auto result = BinaryStore(objects_).ResumeRead(
        Binary(access_), ObjectKind::kFile, id, receipt.GetString("pin"),
        &sources_[id]);
    if (!result.ok()) return result;
  }
  return {};
}

void WorkspaceCopies::ReleaseSources(const Json* receipts) {
  const auto cleanup = Cleanup(access_);
  BinaryStore binary(objects_);
  // Some handles may not have opened when one failed or the job expired.
  // Release surviving handles only after matching their original Files ID.
  if (receipts) {
    for (const auto& receipt : receipts->array) {
      if (std::chrono::steady_clock::now() >= cleanup.deadline) break;
      const auto source = receipt.GetString("source");
      auto& pin = sources_[source];
      if (!pin.id.empty()) continue;
      binary.ResumeRead(cleanup, ObjectKind::kFile, source,
                        receipt.GetString("pin"), &pin);
    }
  }
  for (auto& [id, pin] : sources_) {
    if (pin.id.empty() || pin.closed || pin.commit_unknown) continue;
    try {
      binary.Close(cleanup, &pin);
    } catch (...) {
      // An interrupted cleanup is bounded by the original pin expiration.
    }
  }
  sources_.clear();
}

void WorkspaceCopies::Settle(const ObjectReply& result) {
  if (result.commit_unknown) holds_.clear();
  if (result.ok() || result.commit_unknown) release_sources_ = false;
}
void WorkspaceCopies::Bind(Json* record, std::string_view replaced) const {
  if (!record->Has("input_snapshots") && references_.empty()) return;
  auto inputs = record->Has("input_snapshots")
                    ? Field(*record, "input_snapshots")
                    : Array();
  if (!replaced.empty())
    std::erase_if(inputs.array, [&](const auto& row) {
      return row.GetString("path") == replaced;
    });
  inputs.array.insert(inputs.array.end(), references_.begin(),
                      references_.end());
  Set(record, "input_snapshots", std::move(inputs));
}

ObjectReply WorkspaceCopies::CopyFile(const Json& source,
                                      const std::string& installed_id,
                                      std::string param, uint64_t* size,
                                      Json* content) {
  const auto access = Binary(access_);
  BinaryStore binary(objects_);
  BinaryRead local;
  const auto id = source.GetString("file_id");
  const auto found = sources_.find(id);
  const bool borrowed = found != sources_.end();
  if (!release_sources_ && !borrowed) return Corrupt();
  auto& read = borrowed ? found->second : local;
  ScopeExit cleanup([&] {
    if (!borrowed && !read.id.empty() && !read.closed && !read.commit_unknown) {
      try {
        binary.Close(Cleanup(access_), &read);
      } catch (...) {
      }
    }
  });
  const auto path = source.GetString("path");
  ObjectReply result;
  if (!borrowed)
    result = Files(objects_).Open(access, id, &read, nullptr, kSourceBytes);
  if (!result.ok()) {
    if (result.code == "file_input_capacity_exceeded") {
      result = Error(400, "invalid_request", "Files source exceeds 50 MiB");
      result.value = Object({{"param", String(std::move(param))}});
    }
    return result;
  }
  *size = read.manifest.bytes;
  const auto binding = Binding(workspace_, path, installed_id);
  StateCipher cipher(objects_);
  std::vector<std::string> sealed;
  std::string buffer;
  const auto flush = [&] {
    std::string part;
    auto written =
        cipher.Seal(buffer, Part(binding, sealed.size()), access_, &part);
    if (written.ok()) {
      sealed.push_back(std::move(part));
      buffer.clear();
    }
    return written;
  };
  for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
    std::string chunk;
    result = binary.Read(access, read, i, &chunk);
    // The caller already opened and pinned this owned file. Its manifest
    // references immutable chunks that must exist for the life of the pin.
    if (result.http_status == 404 && !result.commit_unknown)
      return Error(503, "binary_state_invalid", "Stored file chunk is missing");
    if (!result.ok()) return result;
    std::string_view bytes(chunk);
    while (!bytes.empty()) {
      const size_t n =
          std::min<size_t>(bytes.size(), kBinaryChunkBytes - buffer.size());
      buffer.append(bytes.substr(0, n));
      bytes.remove_prefix(n);
      if (buffer.size() == kBinaryChunkBytes) {
        result = flush();
        if (!result.ok()) return result;
      }
    }
  }
  if (*size <= kSmallCopyBytes) {
    std::string base64;
    if (!io::EncodeBase64(buffer, 2 * kSmallCopyBytes, &base64))
      return Corrupt();
    *content = Object({{"type", String("inline")},
                       {"path", String(path)},
                       {"data", String(std::move(base64))}});
    return {};
  }
  if (!buffer.empty()) {
    result = flush();
    if (!result.ok()) return result;
  }
  std::vector<std::string_view> parts(sealed.begin(), sealed.end());
  InputSnapshotRef reference;
  auto& hold = holds_.emplace_back();
  auto metadata = Object(
      {{"format", String(std::string(kFormat))}, {"bytes", Number(*size)}});
  result = InputSnapshotStore(objects_).Save(access, binding, metadata, parts,
                                             kStorage, 600, &reference, &hold);
  if (!result.ok()) return result;
  references_.push_back(
      Object({{"path", String(path)},
              {"binding_sha256", String(binding)},
              {"snapshot", InputSnapshotReference(reference)}}));
  *content = source;
  return {};
}

ObjectReply ReadWorkspaceCopy(
    ObjectStore& objects, const StateAccess& access,
    const std::string& workspace, const Json& record, const Json& file,
    const std::function<ObjectReply(std::string_view)>& consume) {
  const auto path = file.GetString("path");
  const auto binding = Binding(workspace, path, file.GetString("id"));
  const auto* inputs = record.GetArray("input_snapshots");
  if (!inputs) return Corrupt();
  const auto it = std::ranges::find_if(inputs->array, [&](const Json& row) {
    return row.GetString("path") == path;
  });
  InputSnapshotRef reference;
  if (it == inputs->array.end() || it->GetString("binding_sha256") != binding ||
      !it->Find("snapshot") ||
      !ParseInputSnapshotReference(*it->Find("snapshot"), &reference))
    return Corrupt();
  InputSnapshot snapshot;
  auto result = InputSnapshotStore(objects).Read(Binary(access), reference,
                                                 binding, kStorage, &snapshot);
  if (!result.ok()) return result;
  const auto bytes = file.GetInt("size_bytes", -1);
  if (snapshot.metadata.GetString("format") != kFormat || bytes < 0 ||
      bytes != snapshot.metadata.GetInt("bytes", -1) ||
      static_cast<uint64_t>(bytes) > kSourceBytes ||
      snapshot.parts.size() !=
          (static_cast<uint64_t>(bytes) + kBinaryChunkBytes - 1) /
              kBinaryChunkBytes)
    return Corrupt();
  uint64_t remaining = bytes;
  StateCipher cipher(objects);
  for (size_t i = 0; i < snapshot.parts.size(); ++i) {
    std::string plain;
    result = cipher.Open(snapshot.parts[i], Part(binding, i), access, &plain);
    if (!result.ok()) return result.http_status == 400 ? Corrupt() : result;
    if (plain.size() != std::min(remaining, kBinaryChunkBytes))
      return Corrupt();
    result = consume(plain);
    if (!result.ok()) return result;
    remaining -= plain.size();
  }
  return {};
}
}  // namespace q4t::server::environment_detail
