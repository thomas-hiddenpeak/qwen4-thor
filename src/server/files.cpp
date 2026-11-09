#include "q4t/server/files.h"

#include <algorithm>
#include <charconv>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
bool Purpose(std::string_view value) {
  for (const auto* allowed :
       {"assistants", "batch", "fine-tune", "vision", "user_data", "evals"})
    if (value == allowed) return true;
  return false;
}
ObjectRequest Request(const BinaryAccess& access, const std::string& id,
                      ObjectOperation operation) {
  ObjectRequest request;
  request.scope = access.scope;
  request.id = id;
  request.kind = ObjectKind::kFile;
  request.operation = operation;
  request.ttl_seconds = 0;
  return request;
}
ObjectReply Public(const Json& stored, bool* hidden = nullptr,
                   bool metadata_only = false) {
  if (hidden) *hidden = false;
  const auto* data = stored.Find("data");
  if (!data || data->GetString("format") != "q4t.file.v1") {
    if (hidden) *hidden = true;
    return Error(404, "file_not_found", "File not found");
  }
  const auto* file = data->Find("file");
  const auto* blob = data->Find("blob");
  BinaryManifest manifest;
  const bool valid_binary = metadata_only
                                ? data->GetInt("binary_bytes", -1) >= 0
                                : blob && ParseBinaryManifest(*blob, &manifest);
  const auto binary_bytes = metadata_only
                                ? data->GetInt("binary_bytes", -1)
                                : static_cast<int64_t>(manifest.bytes);
  if (!file || !file->IsObject() || !valid_binary ||
      file->GetString("id") != stored.GetString("id") ||
      file->GetString("object") != "file" ||
      file->GetInt("bytes", -1) != binary_bytes ||
      file->GetInt("created_at", -1) != stored.GetInt("created_at") ||
      file->GetInt("expires_at", 0) != stored.GetInt("expires_at") ||
      file->GetString("filename").empty() || file->GetString("purpose").empty())
    return Error(503, "file_state_invalid", "Stored file metadata is invalid");
  const auto expires = stored.GetInt("expires_at");
  if (expires > 0 && expires <= std::time(nullptr)) {
    if (hidden) *hidden = true;
    return Error(404, "file_not_found", "File has expired");
  }
  ObjectReply reply;
  reply.value = *file;
  return reply;
}
}  // namespace

FileUpload::FileUpload(ObjectStore& objects, BinaryAccess access,
                       std::string_view content_type)
    : binary_(objects), access_(std::move(access)) {
  std::string boundary;
  const auto checked = MultipartBoundary(content_type, &boundary);
  if (!checked) {
    result_ = Invalid(checked.message(), "Content-Type");
    return;
  }
  MultipartCallbacks callbacks{
      [this](const auto& part) { return BeginPart(part); },
      [this](auto bytes) { return Data(bytes); }, [this] { return EndPart(); }};
  parser_ = std::make_unique<MultipartDecoder>(
      std::move(boundary), kFileWireBytes, 4, std::move(callbacks));
}
Status FileUpload::Failure(ObjectReply reply) {
  if (result_.ok()) result_ = std::move(reply);
  return Status::Fail(result_.message);
}
Status FileUpload::BeginPart(const MultipartPart& part) {
  field_ = part.name;
  text_.clear();
  if (!seen_.insert(field_).second)
    return Failure(Invalid("Duplicate multipart field", field_));
  if (field_ == "file") {
    if (!part.has_filename || part.filename.empty() ||
        part.filename.size() > 1024 ||
        detail::NormalizeUtf8(part.filename) != part.filename)
      return Failure(
          Invalid("file requires a UTF-8 filename of 1..1024 bytes", "file"));
    // This string is metadata only, even if it contains path separators.
    filename_ = part.filename;
    mime_type_ = part.content_type;
    auto result = binary_.Begin(access_, kFileBytes, 3600, &stage_);
    if (!result.ok()) return Failure(std::move(result));
    buffer_.reserve(kBinaryChunkBytes);
    return {};
  }
  if ((field_ != "purpose" && field_ != "expires_after[anchor]" &&
       field_ != "expires_after[seconds]") ||
      part.has_filename)
    return Failure(Invalid("Unknown or non-text multipart field", field_));
  return {};
}
Status FileUpload::Flush() {
  if (buffer_.empty()) return {};
  auto result = binary_.Append(access_, &stage_, buffer_);
  if (!result.ok()) return Failure(std::move(result));
  buffer_.clear();
  return {};
}
Status FileUpload::Data(std::string_view bytes) {
  if (field_ != "file") {
    if (bytes.size() > 4096 - text_.size())
      return Failure(
          Invalid("Multipart text field exceeds byte limit", field_));
    text_.append(bytes);
    return {};
  }
  while (!bytes.empty()) {
    const auto count =
        std::min<uint64_t>(kBinaryChunkBytes - buffer_.size(), bytes.size());
    buffer_.append(bytes.substr(0, count));
    bytes.remove_prefix(count);
    if (buffer_.size() == kBinaryChunkBytes) {
      auto status = Flush();
      if (!status) return status;
    }
  }
  return {};
}
Status FileUpload::EndPart() {
  if (field_ == "file") {
    auto flushed = Flush();
    if (!flushed) return flushed;
    file_ended_ = true;
  } else {
    if (detail::NormalizeUtf8(text_) != text_ || text_.find('\0') != text_.npos)
      return Failure(Invalid("Multipart text field requires UTF-8", field_));
    if (field_ == "purpose") {
      if (!Purpose(text_))
        return Failure(Invalid("Invalid file purpose", "purpose"));
      purpose_ = text_;
    } else if (field_ == "expires_after[anchor]") {
      anchor_ = text_;
    } else if (field_ == "expires_after[seconds]") {
      seconds_ = text_;
    }
  }
  return {};
}
ObjectReply FileUpload::Feed(std::string_view bytes,
                             std::chrono::steady_clock::time_point deadline) {
  if (!result_.ok()) return result_;
  if (finished_) return Invalid("File upload is already finished", "file");
  access_.deadline = std::min(access_.deadline, deadline);
  const auto status = parser_->Feed(bytes);
  if (!status && result_.ok()) result_ = Invalid(status.message(), "body");
  return result_;
}
ObjectReply FileUpload::Finish() {
  if (!result_.ok()) return result_;
  if (finished_) return Invalid("File upload is already finished", "file");
  finished_ = true;
  const auto parsed = parser_->Finish();
  if (!parsed) {
    if (result_.ok()) result_ = Invalid(parsed.message(), "body");
    return result_;
  }
  if (!file_ended_ || purpose_.empty()) {
    result_ = Invalid("file and purpose are required",
                      !file_ended_ ? "file" : "purpose");
    return result_;
  }
  int64_t ttl = purpose_ == "batch" ? 2592000 : 0;
  if (seen_.contains("expires_after[anchor]") ||
      seen_.contains("expires_after[seconds]")) {
    const auto integer = std::from_chars(
        seconds_.data(), seconds_.data() + seconds_.size(), ttl);
    if (anchor_ != "created_at" || seconds_.empty() ||
        integer.ec != std::errc{} ||
        integer.ptr != seconds_.data() + seconds_.size() || ttl < 3600 ||
        ttl > 2592000) {
      result_ = Invalid(
          "expires_after requires created_at and 3600..2592000 "
          "integer seconds",
          "expires_after");
      return result_;
    }
  }
  if ((purpose_ == "batch" || purpose_ == "fine-tune") &&
      !filename_.ends_with(".jsonl")) {
    result_ = Invalid("This purpose requires a .jsonl file", "file");
    return result_;
  }
  if (purpose_ == "batch" && stage_.manifest.bytes > (200ULL << 20)) {
    result_ = Error(413, "file_too_large", "Batch input exceeds 200 MiB");
    return result_;
  }
  const auto id = NewObjectId(ObjectKind::kFile);
  if (id.empty()) {
    result_ = Error(503, "object_entropy_unavailable", "No secure file ID");
    return result_;
  }
  auto root = Request(access_, id, ObjectOperation::kPut);
  root.created_at = std::time(nullptr);
  root.expires_at = ttl ? root.created_at + ttl : 0;
  auto file = Object({{"id", String(id)},
                      {"object", String("file")},
                      {"bytes", Number(stage_.manifest.bytes)},
                      {"created_at", Number(root.created_at)},
                      {"filename", String(filename_)},
                      {"purpose", String(purpose_)},
                      {"status", String("processed")}});
  if (root.expires_at) Set(&file, "expires_at", Number(root.expires_at));
  root.data = Object({{"format", String("q4t.file.v1")},
                      {"file", file},
                      {"mime_type", String(mime_type_)}});
  result_ = binary_.Publish(access_, &stage_, std::move(root));
  if (result_.ok()) result_.value = std::move(file);
  return result_;
}
ObjectReply FileUpload::Abort(const BinaryAccess& cleanup) {
  if (stage_.id.empty() || stage_.closed) return {};
  return binary_.Abort(cleanup, &stage_);
}

ObjectReply Files::Stored(const BinaryAccess& access, const std::string& id) {
  if (!ValidId(id)) return Invalid("Invalid file identity", "file_id");
  auto stored = objects_.Execute(Request(access, id, ObjectOperation::kGet),
                                 access.deadline, access.cancelled);
  if (!stored.ok()) return stored;
  auto checked = Public(stored.value);
  return checked.ok() ? stored : checked;
}
ObjectReply Files::Retrieve(const BinaryAccess& access, const std::string& id) {
  auto stored = Stored(access, id);
  return stored.ok() ? Public(stored.value) : stored;
}
ObjectReply Files::Erase(const BinaryAccess& access, const std::string& id) {
  auto stored = Stored(access, id);
  if (!stored.ok()) return stored;
  auto request = Request(access, id, ObjectOperation::kErase);
  request.expected_revision = stored.value.GetInt("revision");
  auto reply = objects_.Execute(request, access.deadline, access.cancelled);
  if (reply.ok())
    reply.value = Object({{"id", String(id)},
                          {"object", String("file")},
                          {"deleted", Boolean(true)}});
  else if (reply.commit_unknown)
    reply.value = Object({{"id", String(id)}});
  return reply;
}
ObjectReply Files::Open(const BinaryAccess& access, const std::string& id,
                        BinaryRead* read) {
  auto metadata = Retrieve(access, id);
  if (!metadata.ok()) return metadata;
  BinaryStore binary(objects_);
  const auto remaining = std::chrono::ceil<std::chrono::seconds>(
                             access.deadline - std::chrono::steady_clock::now())
                             .count();
  auto pinned = binary.Open(access, ObjectKind::kFile, id,
                            std::clamp<int64_t>(remaining + 1, 1, 3600), read);
  return pinned.ok() ? metadata : pinned;
}
ObjectReply Files::List(const BinaryAccess& access, const Json& query) {
  std::string param;
  const auto checked =
      InputFields(query, {"after", "limit", "order", "purpose"}, &param);
  if (!checked) return Invalid(checked.message(), param);
  const auto limit = query.GetInt("limit", query.Has("limit") ? -1 : 10000);
  const auto order = query.GetString("order", query.Has("order") ? "" : "desc");
  if (limit < 1 || limit > 10000)
    return Invalid("limit must be 1..10000", "limit");
  if (order != "asc" && order != "desc")
    return Invalid("order requires asc or desc", "order");
  for (const auto* name : {"after", "purpose"})
    if (const auto* field = query.Find(name); field && !field->IsString())
      return Invalid(std::string(name) + " requires a string", name);
  auto request = Request(access, "", ObjectOperation::kList);
  request.descending = order == "desc";
  request.limit = 100;
  request.file_metadata_only = true;
  request.after = query.GetString("after");
  if (query.Has("after")) {
    if (!ValidId(request.after)) return Invalid("Invalid file cursor", "after");
    auto cursor = Stored(access, request.after);
    if (!cursor.ok()) return cursor;
  }
  auto files = Array();
  bool more = false;
  size_t scanned = 0;
  for (;;) {
    auto page = objects_.Execute(request, access.deadline, access.cancelled);
    if (!page.ok()) return page;
    const auto* rows = page.value.GetArray("data");
    if (!rows) return Error(503, "file_state_invalid", "Invalid file list");
    for (const auto& stored : rows->array) {
      if (++scanned > 100000)
        return Error(503, "file_list_capacity", "File scan capacity exceeded");
      bool hidden = false;
      auto file = Public(stored, &hidden, true);
      if (hidden) continue;
      if (!file.ok()) return file;
      if (query.Has("purpose") &&
          file.value.GetString("purpose") != query.GetString("purpose"))
        continue;
      if (files.array.size() == static_cast<size_t>(limit)) {
        more = true;
        break;
      }
      files.array.push_back(std::move(file.value));
    }
    if (more || !page.value.GetBool("has_more")) break;
    const auto next = page.value.GetString("last_id");
    if (rows->array.empty() || next.empty() || next == request.after)
      return Error(503, "file_state_invalid", "Invalid file list progress");
    request.after = next;
  }
  ObjectReply reply;
  const auto first =
      files.array.empty() ? "" : files.array.front().GetString("id");
  const auto last =
      files.array.empty() ? "" : files.array.back().GetString("id");
  reply.value = Object({{"object", String("list")},
                        {"data", std::move(files)},
                        {"has_more", Boolean(more)},
                        {"first_id", String(first)},
                        {"last_id", String(last)}});
  return reply;
}
}  // namespace q4t::server
