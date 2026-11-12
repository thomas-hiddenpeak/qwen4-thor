#include "q4t/server/uploads.h"

#include <algorithm>
#include <array>
#include <ctime>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/native_crypto.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Corrupt() {
  return Error(503, "upload_state_invalid", "Stored upload is inconsistent");
}
ObjectReply Fields(const Json& value,
                   std::initializer_list<std::string_view> allowed) {
  if (!value.IsObject()) return Invalid("Expected a JSON object", "body");
  std::unordered_set<std::string> seen;
  for (const auto& [name, ignored] : value.object) {
    (void)ignored;
    if (std::find(allowed.begin(), allowed.end(), name) == allowed.end() ||
        !seen.insert(name).second)
      return Invalid("Unknown or duplicate field", name);
  }
  return {};
}
bool Text(const Json& body, const std::string& key, size_t maximum) {
  const auto* field = body.Find(key);
  return field && field->IsString() && !field->str.empty() &&
         field->str.size() <= maximum &&
         field->str.find('\0') == field->str.npos &&
         detail::NormalizeUtf8(field->str) == field->str;
}
bool Purpose(std::string_view purpose) {
  for (const auto* name :
       {"assistants", "batch", "fine-tune", "vision", "user_data", "evals"})
    if (purpose == name) return true;
  return false;
}
bool Mime(std::string_view mime) {
  const auto slash = mime.find('/');
  if (slash == mime.npos || slash == 0 || slash + 1 == mime.size())
    return false;
  for (const char c : mime)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '/' || c == '!' || c == '#' ||
          c == '$' || c == '&' || c == '^' || c == '_' || c == '.' ||
          c == '+' || c == '-'))
      return false;
  return mime.find('/', slash + 1) == mime.npos;
}
bool PurposeMime(std::string_view purpose, std::string_view mime) {
  if (purpose == "vision")
    return mime == "image/png" || mime == "image/jpeg" ||
           mime == "image/webp" || mime == "image/gif";
  if (purpose != "assistants") return true;
  // Official file-search supported MIME types; content decoding belongs to
  // the actual consumer, never inferred solely from the caller's MIME label.
  for (const auto* value :
       {"text/x-c",
        "text/x-c++",
        "text/x-csharp",
        "text/css",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml."
        "document",
        "text/x-golang",
        "text/html",
        "text/x-java",
        "text/javascript",
        "application/json",
        "text/markdown",
        "application/pdf",
        "text/x-php",
        "application/vnd.openxmlformats-officedocument.presentationml."
        "presentation",
        "text/x-python",
        "text/x-script.python",
        "text/x-ruby",
        "application/x-sh",
        "text/x-tex",
        "application/typescript",
        "text/plain"})
    if (mime == value) return true;
  return false;
}
std::string Id(std::string_view prefix) {
  const auto random = NewObjectId(ObjectKind::kToolRun);
  return random.empty() ? "" : std::string(prefix) + random.substr(9);
}
ObjectRequest Request(const BinaryAccess& access, const std::string& id,
                      ObjectOperation op = ObjectOperation::kGet) {
  ObjectRequest request;
  request.operation = op;
  request.scope = access.scope;
  request.kind = ObjectKind::kToolRun;
  request.id = id;
  request.ttl_seconds = 0;
  return request;
}
ObjectReply Stored(ObjectStore& objects, const BinaryAccess& access,
                   const std::string& id) {
  if (!ValidId(id)) return Invalid("Invalid Upload identity", "upload_id");
  auto result =
      objects.Execute(Request(access, id), access.deadline, access.cancelled);
  if (!result.ok()) return result;
  const auto* data = result.value.Find("data");
  if (!data || data->GetString("format") != "q4t.upload.v1")
    return Error(404, "upload_not_found", "Upload not found");
  const auto* upload = data->Find("upload");
  const auto* parts = data->GetArray("parts");
  if (!upload || !upload->IsObject() || upload->GetString("id") != id ||
      upload->GetString("object") != "upload" ||
      upload->GetInt("created_at", -1) != result.value.GetInt("created_at") ||
      upload->GetInt("expires_at", -1) != upload->GetInt("created_at") + 3600 ||
      result.value.GetInt("expires_at") !=
          upload->GetInt("expires_at") + 86400 ||
      upload->GetInt("bytes", -1) < 0 ||
      upload->GetInt("bytes") > static_cast<int64_t>(kBinaryMaxBytes) ||
      !Text(*upload, "filename", 1024) ||
      !Purpose(upload->GetString("purpose")) || !parts ||
      parts->array.size() > kUploadMaxParts ||
      data->GetInt("received_bytes", -1) < 0 ||
      data->GetInt("received_bytes") > static_cast<int64_t>(kBinaryMaxBytes) ||
      !Text(*data, "mime_type", 255) || data->GetInt("file_ttl", -1) < 0 ||
      data->GetInt("file_ttl") > 2592000)
    return Corrupt();
  const auto status = upload->GetString("status");
  if (status != "pending" && status != "completed" && status != "cancelled" &&
      status != "expired")
    return Corrupt();
  std::unordered_set<std::string> seen;
  for (const auto& part : parts->array)
    if (!part.IsString() || !ValidId(part.str) || !seen.insert(part.str).second)
      return Corrupt();
  if (status == "completed" &&
      (!upload->Find("file") || !upload->Find("file")->IsObject() ||
       !data->GetArray("completed_parts")))
    return Corrupt();
  return result;
}
Json Public(const Json& stored) {
  auto upload = *stored.Find("data")->Find("upload");
  if (upload.GetString("status") == "pending" &&
      upload.GetInt("expires_at") <= std::time(nullptr))
    Set(&upload, "status", String("expired"));
  return upload;
}
ObjectReply Pending(const Json& stored) {
  const auto upload = Public(stored);
  const auto status = upload.GetString("status");
  if (status != "pending")
    return Error(409, "upload_not_pending", "Upload is " + status);
  return {};
}
ObjectRequest Update(const BinaryAccess& access, const Json& stored,
                     bool require_pending = true) {
  auto request = Request(access, stored.GetString("id"), ObjectOperation::kPut);
  request.expected_revision = stored.GetInt("revision");
  request.created_at = stored.GetInt("created_at");
  request.expires_at = stored.GetInt("expires_at");
  request.data = *stored.Find("data");
  if (require_pending)
    request.mutate_before = request.data.Find("upload")->GetInt("expires_at");
  return request;
}
void Identity(ObjectReply* reply, const std::string& id,
              const std::string& file = {}) {
  if (!reply->value.IsObject()) reply->value = Object();
  Set(&reply->value, "id", String(id));
  if (!file.empty()) Set(&reply->value, "file_id", String(file));
}
ObjectReply ChecksumError() {
  return Error(503, "upload_checksum_unavailable",
               "Requested MD5 checksum is unavailable; upload is unchanged");
}
}  // namespace

ObjectReply Uploads::Create(const BinaryAccess& access, const Json& body) {
  auto valid = Fields(
      body, {"bytes", "filename", "mime_type", "purpose", "expires_after"});
  if (!valid.ok()) return valid;
  const auto bytes = body.GetInt("bytes", -1);
  if (bytes < 0) return Invalid("bytes must be a nonnegative integer", "bytes");
  if (bytes > static_cast<int64_t>(kBinaryMaxBytes))
    return Error(413, "upload_too_large", "Upload exceeds 8 GiB");
  for (const auto& [name, limit] :
       {std::pair{"filename", 1024}, {"mime_type", 255}, {"purpose", 32}})
    if (!Text(body, name, limit))
      return Invalid("Expected a bounded nonempty UTF-8 string", name);
  const auto purpose = body.GetString("purpose");
  if (!Purpose(purpose)) return Invalid("Invalid file purpose", "purpose");
  auto mime = body.GetString("mime_type");
  std::transform(mime.begin(), mime.end(), mime.begin(), [](char c) {
    return c >= 'A' && c <= 'Z' ? static_cast<char>(c + 32) : c;
  });
  if (!Mime(mime) || !PurposeMime(purpose, mime))
    return Invalid("Invalid MIME type for this file purpose", "mime_type");
  const auto filename = body.GetString("filename");
  if ((purpose == "batch" || purpose == "fine-tune") &&
      !filename.ends_with(".jsonl"))
    return Invalid("This purpose requires a .jsonl file", "filename");
  if (purpose == "batch" && bytes > (200LL << 20))
    return Error(413, "file_too_large", "Batch input exceeds 200 MiB");
  int64_t ttl = purpose == "batch" ? 2592000 : 0;
  if (const auto* expiry = body.Find("expires_after")) {
    valid = Fields(*expiry, {"anchor", "seconds"});
    ttl = expiry->GetInt("seconds", -1);
    if (!valid.ok() || expiry->GetString("anchor") != "created_at" ||
        ttl < 3600 || ttl > 2592000)
      return Invalid(
          "expires_after requires created_at and 3600..2592000 "
          "integer seconds",
          "expires_after");
  }
  const auto id = Id("upload_");
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No secure Upload ID");
  auto request = Request(access, id, ObjectOperation::kPut);
  request.created_at = std::time(nullptr);
  const auto expires = request.created_at + 3600;
  // Receipt-only grace supports reconciliation; parts stop being retained at
  // the public one-hour deadline, even if no client sends a cancel request.
  request.expires_at = expires + 86400;
  const auto upload = Object({{"id", String(id)},
                              {"object", String("upload")},
                              {"bytes", Number(bytes)},
                              {"created_at", Number(request.created_at)},
                              {"expires_at", Number(expires)},
                              {"filename", String(filename)},
                              {"purpose", String(purpose)},
                              {"status", String("pending")}});
  request.data = Object({{"format", String("q4t.upload.v1")},
                         {"upload", upload},
                         {"mime_type", String(mime)},
                         {"file_ttl", Number(ttl)},
                         {"parts", Array()},
                         {"received_bytes", Number(0)}});
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (result.ok())
    result.value = upload;
  else
    Identity(&result, id);
  return result;
}
ObjectReply Uploads::Retrieve(const BinaryAccess& access,
                              const std::string& id) {
  auto result = Stored(objects_, access, id);
  if (result.ok()) result.value = Public(result.value);
  return result;
}
ObjectReply Uploads::Cancel(const BinaryAccess& access, const std::string& id) {
  for (int attempt = 0; attempt < 32; ++attempt) {
    auto stored = Stored(objects_, access, id);
    if (!stored.ok()) return stored;
    auto upload = Public(stored.value);
    const auto status = upload.GetString("status");
    if (status == "completed")
      return Error(409, "upload_already_completed", "Upload is completed");
    if (status == "cancelled" || status == "expired") {
      stored.value = std::move(upload);
      return stored;
    }
    auto request = Update(access, stored.value);
    Set(&upload, "status", String("cancelled"));
    Set(&request.data, "upload", upload);
    Set(&request.data, "parts", Array());
    auto result = objects_.Execute(request, access.deadline, access.cancelled);
    if (result.ok())
      result.value = std::move(upload);
    else
      Identity(&result, id);
    if (result.commit_unknown || result.code != "object_revision_conflict")
      return result;
  }
  return Error(409, "upload_busy", "Upload changed during cancellation");
}

UploadPart::UploadPart(ObjectStore& objects, BinaryAccess access,
                       std::string upload_id, std::string_view content_type)
    : objects_(objects),
      binary_(objects),
      access_(std::move(access)),
      upload_id_(std::move(upload_id)) {
  std::string boundary;
  auto checked = MultipartBoundary(content_type, &boundary);
  if (!checked) {
    result_ = Invalid(checked.message(), "Content-Type");
    return;
  }
  auto stored = Stored(objects_, access_, upload_id_);
  result_ = stored.ok() ? Pending(stored.value) : stored;
  if (!result_.ok()) return;
  expires_ = stored.value.Find("data")->Find("upload")->GetInt("expires_at");
  MultipartCallbacks callbacks{
      [this](const auto& part) { return BeginPart(part); },
      [this](auto bytes) { return Data(bytes); },
      [this] {
        const auto result = Flush();
        ended_ = result.ok();
        return result;
      }};
  parser_ = std::make_unique<MultipartDecoder>(
      std::move(boundary), kUploadPartWireBytes, 1, std::move(callbacks));
}
Status UploadPart::Failure(ObjectReply reply) {
  if (result_.ok()) result_ = std::move(reply);
  return Status::Fail(result_.message);
}
Status UploadPart::BeginPart(const MultipartPart& part) {
  if (seen_ || part.name != "data")
    return Failure(Invalid("Exactly one data part is required", "data"));
  seen_ = true;
  const auto lifetime = expires_ - std::time(nullptr);
  if (lifetime <= 0)
    return Failure(Error(409, "upload_not_pending", "Upload is expired"));
  auto result = binary_.Begin(access_, kUploadPartBytes, lifetime, &stage_);
  if (!result.ok()) return Failure(std::move(result));
  buffer_.reserve(kBinaryChunkBytes);
  return {};
}
Status UploadPart::Flush() {
  if (buffer_.empty()) return {};
  auto result = binary_.Append(access_, &stage_, buffer_);
  if (!result.ok()) return Failure(std::move(result));
  buffer_.clear();
  return {};
}
Status UploadPart::Data(std::string_view bytes) {
  while (!bytes.empty()) {
    const auto count =
        std::min<uint64_t>(kBinaryChunkBytes - buffer_.size(), bytes.size());
    buffer_.append(bytes.substr(0, count));
    bytes.remove_prefix(count);
    if (buffer_.size() == kBinaryChunkBytes) {
      auto result = Flush();
      if (!result) return result;
    }
  }
  return {};
}
ObjectReply UploadPart::Feed(std::string_view bytes,
                             Clock::time_point deadline) {
  if (!result_.ok()) return result_;
  if (finished_) return Invalid("Part is already finished", "data");
  access_.deadline = std::min(access_.deadline, deadline);
  const auto parsed = parser_->Feed(bytes);
  if (!parsed && result_.ok()) result_ = Invalid(parsed.message(), "body");
  return result_;
}
ObjectReply UploadPart::Finish() {
  if (!result_.ok()) return result_;
  if (finished_) return Invalid("Part is already finished", "data");
  finished_ = true;
  const auto parsed = parser_->Finish();
  if (!parsed || !seen_ || !ended_) {
    if (result_.ok()) result_ = Invalid("Incomplete multipart data", "data");
    return result_;
  }
  const auto id = Id("part_");
  if (id.empty()) {
    result_ = Error(503, "object_entropy_unavailable", "No secure Part ID");
    return result_;
  }
  const auto now = std::time(nullptr);
  const auto part = Object({{"id", String(id)},
                            {"object", String("upload.part")},
                            {"created_at", Number(now)},
                            {"upload_id", String(upload_id_)}});
  for (int attempt = 0; attempt < 32; ++attempt) {
    auto stored = Stored(objects_, access_, upload_id_);
    result_ = stored.ok() ? Pending(stored.value) : stored;
    if (!result_.ok()) break;
    auto parent = Update(access_, stored.value);
    auto& parts = Field(parent.data, "parts");
    const auto received = parent.data.GetInt("received_bytes");
    if (parts.array.size() >= kUploadMaxParts ||
        stage_.manifest.bytes > kBinaryMaxBytes - received) {
      result_ = Error(413, "upload_capacity_exceeded",
                      "Upload parts or total uploaded bytes exceed capacity");
      break;
    }
    parts.array.push_back(String(id));
    Set(&parent.data, "received_bytes",
        Number(received + stage_.manifest.bytes));
    auto root = Request(access_, id, ObjectOperation::kPut);
    root.created_at = now;
    root.data =
        Object({{"format", String("q4t.upload_part.v1")}, {"part", part}});
    const std::array parents{std::move(parent)};
    result_ = binary_.Publish(access_, &stage_, std::move(root), parents);
    if (result_.ok()) result_.value = part;
    if (result_.commit_unknown || result_.code != "object_revision_conflict")
      return result_;
  }
  return result_;
}
ObjectReply UploadPart::Abort(const BinaryAccess& cleanup) {
  if (stage_.id.empty() || stage_.closed) return {};
  return binary_.Abort(cleanup, &stage_);
}

ObjectReply Uploads::Complete(const BinaryAccess& access, const std::string& id,
                              const Json& body) {
  auto checked = Fields(body, {"part_ids", "md5"});
  if (!checked.ok()) return checked;
  const auto* parts = body.GetArray("part_ids");
  if (!parts || parts->array.size() > kUploadMaxParts)
    return Invalid("part_ids requires a bounded array", "part_ids");
  for (const auto& part : parts->array)
    if (!part.IsString() || !ValidId(part.str))
      return Invalid("Invalid Part identity", "part_ids");
  std::string expected_md5;
  if (const auto* value = body.Find("md5")) {
    if (!value->IsString() || value->str.size() != 32 ||
        !std::all_of(value->str.begin(), value->str.end(), [](char c) {
          return (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F') ||
                 (c >= '0' && c <= '9');
        }))
      return Invalid("md5 requires 32 hexadecimal digits", "md5");
    expected_md5 = value->str;
    std::transform(expected_md5.begin(), expected_md5.end(),
                   expected_md5.begin(),
                   [](char c) { return c >= 'A' && c <= 'F' ? c + 32 : c; });
  }
  auto stored = Stored(objects_, access, id);
  if (!stored.ok()) return stored;
  const auto& data = *stored.value.Find("data");
  auto upload = Public(stored.value);
  if (upload.GetString("status") == "completed") {
    // Resolve the same complete operation without creating another File.
    if (OutputJson(*data.GetArray("completed_parts")) != OutputJson(*parts) ||
        data.GetString("completed_md5") != expected_md5)
      return Error(409, "upload_already_completed",
                   "Upload was completed with different parameters");
    stored.value = std::move(upload);
    return stored;
  }
  checked = Pending(stored.value);
  if (!checked.ok()) return checked;
  std::unordered_set<std::string> owned;
  for (const auto& part : data.GetArray("parts")->array) owned.insert(part.str);
  for (const auto& part : parts->array)
    if (!owned.contains(part.str))
      return Invalid("Part does not belong to this Upload", "part_ids");
  CryptoMd5 md5;
  if (!expected_md5.empty() && md5.Begin() != CryptoResult::kOk)
    return ChecksumError();
  BinaryStore binary(objects_);
  BinaryManifest assembled;
  BinaryStage copies;
  std::unordered_set<std::string> chunks;
  ScopeExit discard_copies([&] {
    if (copies.id.empty() || copies.closed || copies.commit_unknown) return;
    try {
      auto bounded = access;
      bounded.deadline = Clock::now() + 200ms;
      bounded.cancelled = {};
      binary.Abort(bounded, &copies);
    } catch (...) {
    }
  });
  // The pending Upload protects all parts. Each read also pins exact chunks
  // while checking them; the final parent CAS defeats concurrent cancel/add.
  for (const auto& part : parts->array) {
    auto metadata = objects_.Execute(Request(access, part.str), access.deadline,
                                     access.cancelled);
    if (!metadata.ok()) return metadata;
    const auto* part_data = metadata.value.Find("data");
    const auto* public_part = part_data ? part_data->Find("part") : nullptr;
    if (!part_data || part_data->GetString("format") != "q4t.upload_part.v1" ||
        !public_part || public_part->GetString("id") != part.str ||
        public_part->GetString("object") != "upload.part" ||
        public_part->GetString("upload_id") != id ||
        public_part->GetInt("created_at", -1) !=
            metadata.value.GetInt("created_at"))
      return Corrupt();
    BinaryRead read;
    checked = binary.Open(access, ObjectKind::kToolRun, part.str, 3600, &read);
    if (!checked.ok()) {
      Identity(&checked, id);
      return checked;
    }
    ScopeExit cleanup([&] {
      try {
        auto bounded = access;
        bounded.deadline = Clock::now() + 200ms;
        bounded.cancelled = {};
        binary.Close(bounded, &read);
      } catch (...) {
      }
    });
    if (read.manifest.bytes > kBinaryMaxBytes - assembled.bytes ||
        read.manifest.chunks.size() >
            kBinaryMaxChunks - assembled.chunks.size())
      return Error(413, "upload_capacity_exceeded",
                   "Assembled file is too large");
    // Validate actual chunk bytes even without the optional MD5. Metadata
    // alone cannot authorize publishing corrupted content as a complete File.
    for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
      std::string bytes;
      checked = binary.Read(access, read, i, &bytes);
      if (!checked.ok()) return checked;
      if (!expected_md5.empty() && md5.Update(bytes) != CryptoResult::kOk)
        return ChecksumError();
      auto chunk = read.manifest.chunks[i];
      if (!chunks.insert(chunk.id).second) {
        // Durable file v1 requires unique chunk identities, including in the
        // retained rollback engine. Repeated Parts need accounted immutable
        // copies, not a newer manifest silently unreadable by v11.
        if (copies.id.empty()) {
          checked = binary.Begin(access, kBinaryMaxBytes, 3600, &copies);
          if (!checked.ok()) return checked;
        }
        checked = binary.Append(access, &copies, bytes);
        if (!checked.ok()) return checked;
        chunk = copies.manifest.chunks.back();
      }
      assembled.chunks.push_back(std::move(chunk));
    }
    assembled.bytes += read.manifest.bytes;
  }
  if (assembled.bytes != static_cast<uint64_t>(upload.GetInt("bytes")))
    return Invalid("Completed byte count does not match declared bytes",
                   "part_ids");
  if (!expected_md5.empty()) {
    std::string actual;
    if (md5.Finish(&actual) != CryptoResult::kOk) return ChecksumError();
    if (actual != expected_md5)
      return Invalid("File MD5 does not match", "md5");
  }
  auto parent = Update(access, stored.value);
  const auto file_id = NewObjectId(ObjectKind::kFile);
  if (file_id.empty())
    return Error(503, "object_entropy_unavailable", "No secure File ID");
  auto root = Request(access, file_id, ObjectOperation::kPut);
  root.kind = ObjectKind::kFile;
  root.created_at = std::time(nullptr);
  const auto ttl = data.GetInt("file_ttl");
  root.expires_at = ttl ? root.created_at + ttl : 0;
  auto file = Object({{"id", String(file_id)},
                      {"object", String("file")},
                      {"bytes", Number(assembled.bytes)},
                      {"created_at", Number(root.created_at)},
                      {"filename", *upload.Find("filename")},
                      {"purpose", *upload.Find("purpose")},
                      {"status", String("processed")}});
  if (root.expires_at) Set(&file, "expires_at", Number(root.expires_at));
  root.data = Object({{"format", String("q4t.file.v1")},
                      {"file", file},
                      {"mime_type", *data.Find("mime_type")},
                      {"blob", BinaryManifestJson(assembled)}});
  Set(&upload, "status", String("completed"));
  Set(&upload, "file", std::move(file));
  Set(&parent.data, "upload", upload);
  Set(&parent.data, "parts", Array());
  Set(&parent.data, "completed_parts", *parts);
  Set(&parent.data, "completed_md5", String(expected_md5));
  std::vector requests{std::move(root), std::move(parent)};
  if (!copies.id.empty()) {
    auto erase = Request(access, copies.id, ObjectOperation::kErase);
    erase.expected_revision = copies.revision;
    requests.push_back(std::move(erase));
  }
  auto result =
      objects_.ExecuteBatch(requests, access.deadline, access.cancelled);
  if (result.ok()) copies.closed = true;
  if (result.commit_unknown) copies.commit_unknown = true;
  if (result.ok())
    result.value = std::move(upload);
  else
    Identity(&result, id, file_id);
  return result;
}
}  // namespace q4t::server
