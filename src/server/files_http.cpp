#include "q4t/server/files_http.h"

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
void Reply(int fd, const ObjectReply& reply, const BinaryAccess& access) {
  if (reply.ok())
    detail::SendSimple(fd, 200, "OK", OutputJson(reply.value),
                       "application/json", {access.deadline});
  else
    detail::SendError(fd, reply.http_status, reply.ClientMessage(),
                      {access.deadline}, reply.ClientCode(),
                      reply.value.GetString("param"),
                      reply.value.GetString("allow"));
}
void ListReply(int fd, const ObjectReply& reply, const BinaryAccess& access) {
  if (!reply.ok()) {
    Reply(fd, reply, access);
    return;
  }
  const auto& rows = reply.value.GetArray("data")->array;
  const std::string prefix = "{\"object\":\"list\",\"data\":[";
  const std::string suffix =
      "],\"has_more\":" + OutputJson(*reply.value.Find("has_more")) +
      ",\"first_id\":" + OutputJson(*reply.value.Find("first_id")) +
      ",\"last_id\":" + OutputJson(*reply.value.Find("last_id")) + "}";
  uint64_t bytes = prefix.size() + suffix.size();
  for (size_t i = 0; i < rows.size(); ++i)
    bytes += OutputJson(rows[i]).size() + (i != 0);
  // Serialize one public metadata object at a time, avoiding full body plus
  // response copies for the standard 10000-entry page.
  size_t index = 0;
  detail::SendBinaryResponse(
      fd, bytes,
      [&](std::string* output) {
        if (index == 0)
          *output = prefix;
        else if (index <= rows.size())
          *output = (index > 1 ? "," : "") + OutputJson(rows[index - 1]);
        else
          *output = suffix;
        ++index;
        return true;
      },
      {access.deadline}, "application/json");
}
}  // namespace

FileHttpRequest::~FileHttpRequest() {
  // Known private staging only. A lost publication receipt refuses cleanup;
  // the existing TTL and reference graph retain its exact durable decision.
  if (upload_ || part_) {
    try {
      auto cleanup = access_;
      cleanup.deadline = Clock::now() + 200ms;
      cleanup.cancelled = {};
      if (upload_) upload_->Abort(cleanup);
      if (part_) part_->Abort(cleanup);
    } catch (...) {
      // No auxiliary cleanup failure may escape the HTTP lifetime.
    }
  }
}
ObjectReply FileHttpRequest::Prepare(std::string_view method,
                                     std::string_view target,
                                     std::string_view content_type,
                                     HttpBodyStream* stream) {
  if (IsUploadTarget(target))
    return PrepareUpload(method, target, content_type, stream);
  method_ = method;
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  if (!IsFileTarget(path))
    return Error(404, "route_not_found", "File route not found");
  collection_ = path == "/v1/files";
  if (!collection_) {
    auto suffix = path.substr(10);
    const auto slash = suffix.find('/');
    id_ = suffix.substr(0, slash);
    if (!ValidId(id_) ||
        (slash != suffix.npos && suffix.substr(slash) != "/content"))
      return Error(404, "route_not_found", "File route not found");
    content_ = slash != suffix.npos;
  }
  if (!FileMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value = Object({{"param", String("method")},
                          {"allow", String(collection_ ? "GET, POST"
                                           : content_  ? "GET"
                                                       : "GET, DELETE")}});
    return reply;
  }
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!(collection_ && method == "GET") && !query.value.object.empty()) {
    auto reply = Error(400, "invalid_request", "Query not allowed here");
    reply.value = Object({{"param", String("query")}});
    return reply;
  }
  query_ = std::move(query.value);
  if (collection_ && method == "POST") {
    wire_limit_ = kFileWireBytes;
    upload_ = std::make_unique<FileUpload>(objects_, access_, content_type);
    if (!upload_->Result().ok()) return upload_->Result();
    stream->max_bytes = kFileWireBytes;
    stream->reservation_bytes = kFileWorkingBytes;
    stream->max_metadata_bytes = 16ULL << 20;
    stream->consume = [this, stream](auto bytes, auto deadline) {
      const auto result = upload_->Feed(bytes, deadline);
      if (!result.ok()) {
        stream->failure_status = result.http_status;
        stream->failure_code = result.ClientCode();
      }
      return result.ok();
    };
  } else {
    // No body is valid here. A streaming reservation also accounts for the
    // response working set before any durable list/read work starts.
    stream->max_bytes = 1;
    stream->reservation_bytes = collection_ ? kFileListWorkingBytes
                                : content_  ? kFileWorkingBytes
                                            : 65536;
    stream->failure_status = 400;
    stream->failure_code = "unexpected_body";
    stream->consume = [](auto, auto) { return false; };
  }
  return {};
}
void FileHttpRequest::Serve(int fd) {
  if (!upload_action_.empty()) {
    ServeUpload(fd);
    return;
  }
  if (upload_) {
    Reply(fd, upload_->Finish(), access_);
    return;
  }
  if (content_) {
    Content(fd);
    return;
  }
  Files files(objects_);
  auto reply = collection_        ? files.List(access_, query_)
               : method_ == "GET" ? files.Retrieve(access_, id_)
                                  : files.Erase(access_, id_);
  if (collection_)
    ListReply(fd, reply, access_);
  else
    Reply(fd, reply, access_);
}

ObjectReply FileHttpRequest::PrepareUpload(std::string_view method,
                                           std::string_view target,
                                           std::string_view content_type,
                                           HttpBodyStream* stream) {
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  if (!UploadMethodAllowed("POST", path))
    return Error(404, "route_not_found", "Upload route not found");
  if (method != "POST") {
    auto result = Error(405, "method_not_allowed", "Method not allowed");
    result.value =
        Object({{"param", String("method")}, {"allow", String("POST")}});
    return result;
  }
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!query.value.object.empty())
    return Error(400, "invalid_request", "Upload queries are not allowed");
  if (path == "/v1/uploads") {
    upload_action_ = "create";
  } else {
    const auto tail = path.substr(12);
    const auto slash = tail.find('/');
    id_ = tail.substr(0, slash);
    upload_action_ = tail.substr(slash + 1);
  }
  if (upload_action_ == "parts") {
    part_ = std::make_unique<UploadPart>(objects_, access_, id_, content_type);
    if (!part_->Result().ok()) return part_->Result();
    stream->max_bytes = wire_limit_ = kUploadPartWireBytes;
    stream->reservation_bytes = kFileWorkingBytes;
    stream->max_metadata_bytes = 16ULL << 20;
    stream->consume = [this, stream](auto bytes, auto deadline) {
      const auto result = part_->Feed(bytes, deadline);
      if (!result.ok()) {
        stream->failure_status = result.http_status;
        stream->failure_code = result.ClientCode();
      }
      return result.ok();
    };
    return {};
  }
  std::string media(content_type);
  for (char& c : media)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  const auto semi = media.find(';');
  auto charset = semi == media.npos ? "" : media.substr(semi + 1);
  media.resize(semi == media.npos ? media.size() : semi);
  while (!media.empty() && (media.back() == ' ' || media.back() == '\t'))
    media.pop_back();
  while (!charset.empty() &&
         (charset.front() == ' ' || charset.front() == '\t'))
    charset.erase(0, 1);
  if (!((upload_action_ == "cancel" && content_type.empty()) ||
        (media == "application/json" &&
         (charset.empty() || charset == "charset=utf-8" ||
          charset == "charset=\"utf-8\""))))
    return Error(415, "unsupported_media_type",
                 "Upload control requires application/json with UTF-8");
  const bool body_allowed = !content_type.empty();
  stream->max_bytes = wire_limit_ =
      upload_action_ == "complete" ? 2ULL << 20 : 16ULL << 10;
  stream->reservation_bytes = upload_action_ == "complete"
                                  ? kUploadCompleteWorkingBytes
                                  : kFileWorkingBytes;
  stream->failure_status = 415;
  stream->failure_code = "unsupported_media_type";
  stream->consume = [this, body_allowed](auto bytes, auto) {
    if (!body_allowed) return false;
    json_body_.append(bytes);
    return true;
  };
  return {};
}

void FileHttpRequest::ServeUpload(int fd) {
  if (part_) {
    Reply(fd, part_->Finish(), access_);
    return;
  }
  Json body = Object();
  if (!json_body_.empty() || upload_action_ != "cancel") {
    io::JsonParseLimits limits;
    limits.max_depth = 6;
    limits.max_values =
        upload_action_ == "complete" ? kUploadMaxParts + 32 : 32;
    limits.max_string_bytes = wire_limit_;
    limits.reject_duplicate_keys = true;
    if (!io::ParseJson(json_body_, &body, true, limits) || !body.IsObject()) {
      Reply(fd, Error(400, "invalid_request", "Invalid Upload JSON object"),
            access_);
      return;
    }
  }
  Uploads uploads(objects_);
  ObjectReply result;
  if (upload_action_ == "create") {
    result = uploads.Create(access_, body);
  } else if (upload_action_ == "complete") {
    result = uploads.Complete(access_, id_, body);
  } else {
    result = body.object.empty()
                 ? uploads.Cancel(access_, id_)
                 : Error(400, "invalid_request", "Cancel has no body fields");
  }
  Reply(fd, result, access_);
}
void FileHttpRequest::Content(int fd) {
  Files files(objects_);
  BinaryStore binary(objects_);
  BinaryRead read;
  const auto opened = files.Open(access_, id_, &read);
  if (!opened.ok()) {
    Reply(fd, opened, access_);
    return;
  }
  ScopeExit cleanup([&] {
    try {
      auto access = access_;
      access.deadline = Clock::now() + 200ms;
      access.cancelled = {};
      binary.Close(access, &read);
    } catch (...) {
      // The bounded read lease expires independently of this cleanup attempt.
    }
  });
  std::string first;
  if (!read.manifest.chunks.empty()) {
    auto checked = binary.Read(access_, read, 0, &first);
    if (!checked.ok()) {
      Reply(fd, checked, access_);
      return;
    }
  }
  size_t index = 0;
  detail::SendBinaryResponse(
      fd, read.manifest.bytes,
      [&](std::string* output) {
        if (index++ == 0) {
          *output = std::move(first);
          return true;
        }
        auto checked = binary.Read(access_, read, index - 1, output);
        if (!checked.ok())
          if (auto* context = detail::HttpResponseContext::Current(fd))
            context->ObserveError(checked.http_status, checked.ClientMessage(),
                                  checked.ClientCode());
        return checked.ok();
      },
      {access_.deadline});
}
}  // namespace q4t::server
