#include "q4t/server/files_http.h"

#include <ctime>

#include "chatkit_attachments.h"
#include "containers_internal.h"
#include "q4t/io/base64.h"
#include "q4t/server/http_framing.h"
#include <charconv>

#include "object_protocol.h"
#include "q4t/server/containers.h"
#include "q4t/server/skills.h"
#include "q4t/server/request_json.h"
#include "q4t/server/application_query.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
ObjectReply SkillContentJson(std::string_view accept, bool* json) {
  if (accept.empty()) return {};
  struct Score {
    int specificity = -1;
    double quality = 0;
  };
  Score zip, text;
  while (!accept.empty()) {
    const auto comma = accept.find(',');
    auto item = http::Trim(accept.substr(0, comma));
    const auto semi = item.find(';');
    auto type = http::Lower(http::Trim(item.substr(0, semi)));
    double q = 1;
    for (auto delimiter = semi; delimiter != item.npos;
         delimiter = item.find(';')) {
      item.remove_prefix(delimiter + 1);
      auto param = http::Lower(http::Trim(item.substr(0, item.find(';'))));
      if (param.starts_with("q=")) {
        std::string_view value(param);
        value.remove_prefix(2);
        auto result = std::from_chars(value.data(), value.data() + value.size(),
                                      q, std::chars_format::fixed);
        if (result.ec != std::errc{} ||
            result.ptr != value.data() + value.size() || !(q >= 0 && q <= 1))
          return Error(400, "invalid_request", "Invalid Accept quality");
      }
    }
    const auto offer = [&](Score* score, int specificity) {
      if (specificity > score->specificity ||
          (specificity == score->specificity && q > score->quality))
        *score = {specificity, q};
    };
    if (type == "application/json") offer(&text, 2);
    if (type == "application/zip" || type == "application/binary" ||
        type == "application/octet-stream")
      offer(&zip, 2);
    if (type == "application/*" || type == "*/*") {
      const int rank = type == "*/*" ? 0 : 1;
      offer(&text, rank);
      offer(&zip, rank);
    }
    if (comma == accept.npos) break;
    accept.remove_prefix(comma + 1);
  }
  if (zip.quality == 0 && text.quality == 0)
    return Error(406, "not_acceptable",
                 "Expected a ZIP or JSON representation");
  *json = text.quality > zip.quality ||
          (text.quality == zip.quality && text.specificity > zip.specificity);
  return {};
}
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
ObjectReply FileHttpRequest::PrepareChatKit(std::string_view token,
                                            std::string_view content_type,
                                            HttpBodyStream* stream) {
  if (!IsMultipartForm(content_type))
    return Error(415, "unsupported_media_type",
                 "Expected a multipart attachment");
  chatkit_detail::Grant grant;
  const StateAccess state{access_.scope, access_.deadline, access_.cancelled};
  auto auth = chatkit_detail::Redeem(objects_, state, token, std::time(nullptr),
                                     &grant);
  if (!auth.ok()) return auth;
  uint64_t limit = 0;
  auto allowed = chatkit_detail::AttachmentLimit(grant, &limit);
  if (!allowed.ok()) return allowed;
  upload_ = std::make_unique<FileUpload>(
      objects_, access_, content_type,
      [this, grant = std::move(grant)](
          BinaryStage* stage, std::string_view name, std::string_view) {
        return chatkit_detail::PublishAttachment(
            objects_, access_, grant, stage, name, upload_->MimeType());
      },
      limit);
  if (!upload_->Result().ok()) return upload_->Result();
  PrepareFileStream(stream);
  wire_limit_ = limit + (kFileWireBytes - kFileBytes);
  stream->max_bytes = wire_limit_;
  return {};
}

ObjectReply FileHttpRequest::Prepare(std::string_view method,
                                     std::string_view target,
                                     std::string_view content_type,
                                     HttpBodyStream* stream,
                                     std::string_view accept) {
  if (IsSkillTarget(target)) {
    const auto route = ParseSkillRoute(target);
    if (method == "GET" && (route.kind == SkillRouteKind::kContent ||
                            route.kind == SkillRouteKind::kVersionContent)) {
      auto negotiated = SkillContentJson(accept, &skill_json_content_);
      if (!negotiated.ok()) return negotiated;
    }
    skill_target_ = target;
    method_ = method;
    return PrepareSkill(content_type, stream);
  }
  if (IsContainerTarget(target)) {
    container_target_ = target;
    method_ = method;
    return PrepareContainer(content_type, stream);
  }
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
    PrepareFileStream(stream);
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
void FileHttpRequest::PrepareFileStream(HttpBodyStream* stream) {
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
}

ObjectReply FileHttpRequest::PrepareSkill(std::string_view type,
                                          HttpBodyStream* stream) {
  using Kind = SkillRouteKind;
  const auto route = ParseSkillRoute(skill_target_);
  if (!SkillMethodAllowed(method_, skill_target_))
    return Error(405, "method_not_allowed", "Skill method not allowed");
  const bool creation = IsSkillCreation(method_, skill_target_);
  const bool version = route.kind == Kind::kVersions;
  if (creation && skill_target_.find('?') != skill_target_.npos)
    return Error(400, "invalid_request", "Unexpected skill upload query");
  if (creation && version) {
    auto parent = HandleSkills(
        objects_, {access_.scope, access_.deadline, access_.cancelled}, "GET",
        "/v1/skills/" + std::string(route.skill), "");
    if (!parent.ok()) return parent;
  }
  if (creation && IsMultipartForm(type)) {
    skill_upload_ = std::make_unique<SkillUpload>(type, version);
    if (!skill_upload_->Result().ok()) return skill_upload_->Result();
    stream->max_bytes = wire_limit_ = kSkillWireBytes;
    stream->reservation_bytes = kSkillWireBytes;
    stream->max_metadata_bytes = 16ULL << 20;
    stream->consume = [this, stream](auto bytes, auto) {
      auto reply = skill_upload_->Feed(bytes);
      if (!reply.ok()) {
        stream->failure_status = reply.http_status;
        stream->failure_code = reply.ClientCode();
      }
      return reply.ok();
    };
  } else if (method_ == "POST") {
    std::string media(type);
    std::ranges::transform(media, media.begin(), [](unsigned char c) {
      return static_cast<char>(c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c);
    });
    const auto end = media.find(';');
    auto main = std::string_view(media).substr(0, end);
    main = main.substr(0, main.find_last_not_of(" \t") + 1);
    auto option = end == media.npos ? std::string_view()
                                    : std::string_view(media).substr(end + 1);
    option.remove_prefix(
        std::min(option.size(), option.find_first_not_of(" \t")));
    if (main != "application/json" ||
        (!option.empty() && option != "charset=utf-8" &&
         option != "charset=\"utf-8\""))
      return Error(415, "unsupported_media_type",
                   "Expected UTF-8 JSON or skill files");
    stream->max_bytes = wire_limit_ = creation ? kChatBodyBytes : 1ULL << 20;
    stream->reservation_bytes = creation ? kSkillWireBytes : wire_limit_;
    stream->consume = [this](auto bytes, auto) {
      json_body_.append(bytes);
      return true;
    };
  } else {
    stream->max_bytes = wire_limit_ = 1;
    stream->reservation_bytes =
        (route.kind == Kind::kContent || route.kind == Kind::kVersionContent)
            ? kFileWorkingBytes
            : kFileListWorkingBytes;
    stream->failure_status = 400;
    stream->failure_code = "unexpected_body";
    stream->consume = [](auto, auto) { return false; };
  }
  return {};
}

void FileHttpRequest::ServeSkill(int fd) {
  const StateAccess access{access_.scope, access_.deadline, access_.cancelled};
  if (IsSkillCreation(method_, skill_target_)) {
    SkillPackage input, package;
    bool make_default = false;
    auto parsed = skill_upload_
                      ? skill_upload_->Finish(&input, &make_default)
                      : DecodeSkillJson(json_body_,
                                        ParseSkillRoute(skill_target_).kind ==
                                            SkillRouteKind::kVersions,
                                        access_, &input, &make_default);
    std::string().swap(json_body_);
    if (parsed.ok())
      parsed = ProcessSkillPackage(std::move(input), false, access_, &package);
    if (parsed.ok())
      parsed = HandleSkills(objects_, access, method_, skill_target_, "",
                            &package, make_default);
    Reply(fd, parsed, access_);
    return;
  }
  BinaryRead pin;
  auto result = HandleSkills(objects_, access, method_, skill_target_,
                             json_body_, nullptr, false, &pin);
  if (result.ok() && !pin.id.empty()) {
    BinaryStore binary(objects_);
    SendPinnedBinary(
        fd, binary, access_, pin,
        skill_json_content_ ? "application/json" : "application/zip");
  } else
    Reply(fd, result, access_);
}

ObjectReply FileHttpRequest::PrepareContainer(std::string_view content_type,
                                              HttpBodyStream* stream) {
  using Kind = ContainerRouteKind;
  const auto route = ParseContainerRoute(container_target_);
  if (!ContainerMethodAllowed(method_, container_target_)) {
    auto reply =
        Error(405, "method_not_allowed", "Container method not allowed");
    reply.value = Object(
        {{"allow",
          String(route.kind == Kind::kCollection || route.kind == Kind::kFiles
                     ? "GET, POST"
                 : route.kind == Kind::kContent ? "GET"
                                                : "GET, DELETE")}});
    return reply;
  }
  std::string media(content_type);
  std::transform(
      media.begin(), media.end(), media.begin(), [](unsigned char c) {
        return c >= 'A' && c <= 'Z' ? static_cast<char>(c + ('a' - 'A'))
                                    : static_cast<char>(c);
      });
  auto type = std::string_view(media).substr(0, media.find(';'));
  while (!type.empty() && (type.back() == ' ' || type.back() == '\t'))
    type.remove_suffix(1);
  if (method_ == "POST" && route.kind == Kind::kFiles &&
      IsMultipartForm(content_type)) {
    const StateAccess access{access_.scope, access_.deadline,
                             access_.cancelled};
    const std::string_view target = container_target_;
    const auto query = target.find('?');
    if (query != target.npos && query + 1 != target.size())
      return container_detail::Invalid("Unknown query for upload", "query");
    container_detail::Stored parent;
    auto metadata =
        container_detail::Read(objects_, access, route.container, &parent);
    if (!metadata.ok()) return metadata;
    auto workspace = container_detail::OpenWorkspace(objects_, access, &parent);
    if (!workspace.ok()) return workspace;
    upload_ = std::make_unique<FileUpload>(
        objects_, access_, content_type,
        [this, access](BinaryStage* stage, std::string_view name,
                       std::string_view id) {
          std::string body;
          if (!stage)
            body = OutputJson(Object({{"file_id", String(std::string(id))}}));
          return HandleContainers(objects_, access, method_, container_target_,
                                  body, nullptr, stage, name);
        });
    if (!upload_->Result().ok()) return upload_->Result();
    wire_limit_ = kFileWireBytes;
    PrepareFileStream(stream);
    return {};
  }
  if (method_ == "POST") {
    const auto split = media.find(';');
    auto charset = split == media.npos
                       ? std::string_view{}
                       : std::string_view(media).substr(split + 1);
    while (!charset.empty() &&
           (charset.front() == ' ' || charset.front() == '\t'))
      charset.remove_prefix(1);
    if (type != "application/json" ||
        (!charset.empty() && charset != "charset=utf-8" &&
         charset != "charset=\"utf-8\""))
      return Error(415, "unsupported_media_type",
                   "Expected application/json with UTF-8 or a file form");
    stream->max_bytes = wire_limit_ = kChatBodyBytes;
    stream->reservation_bytes = kChatBodyBytes;
    stream->consume = [this](auto bytes, auto) {
      json_body_.append(bytes);
      return true;
    };
  } else {
    stream->max_bytes = wire_limit_ = 1;
    stream->reservation_bytes =
        route.kind == Kind::kContent ? kFileWorkingBytes
        : route.kind == Kind::kCollection || route.kind == Kind::kFiles
            ? kFileListWorkingBytes
            : 65536;
    stream->failure_status = 400;
    stream->failure_code = "unexpected_body";
    stream->consume = [](auto, auto) { return false; };
  }
  return {};
}
void FileHttpRequest::Serve(int fd) {
  if (!skill_target_.empty()) { ServeSkill(fd); return; }
  if (!upload_action_.empty()) {
    ServeUpload(fd);
    return;
  }
  if (upload_) {
    Reply(fd, upload_->Finish(), access_);
    return;
  }
  if (!container_target_.empty()) {
    const StateAccess access{access_.scope, access_.deadline,
                             access_.cancelled};
    BinaryRead read;
    auto result = HandleContainers(objects_, access, method_, container_target_,
                                   json_body_, &read);
    if (result.ok() && !read.id.empty()) {
      BinaryStore binary(objects_);
      SendPinnedBinary(fd, binary, access_, read);
    } else {
      Reply(fd, result, access_);
    }
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
  SendPinnedBinary(fd, binary, access_, read);
}
void SendPinnedBinary(int fd, BinaryStore& binary, const BinaryAccess& owner,
                      BinaryRead& read, std::string_view mime_type) {
  ScopeExit cleanup([&] {
    try {
      auto access = owner;
      access.deadline = Clock::now() + 200ms;
      access.cancelled = {};
      binary.Close(access, &read);
    } catch (...) {
      // The bounded read lease expires independently of this cleanup attempt.
    }
  });
  std::string first;
  if (!read.manifest.chunks.empty()) {
    auto checked = binary.Read(owner, read, 0, &first);
    if (!checked.ok()) {
      Reply(fd, checked, owner);
      return;
    }
  }
  size_t index = 0;
  const bool encode_json = mime_type == "application/json";
  const auto length = encode_json ? 2 + ((read.manifest.bytes + 2) / 3) * 4
                                  : read.manifest.bytes;
  const auto fetch = [&](std::string* bytes) {
    if (index == 0) {
      *bytes = std::move(first);
    } else {
      auto checked = binary.Read(owner, read, index, bytes);
      if (!checked.ok()) {
        if (auto* context = detail::HttpResponseContext::Current(fd))
          context->ObserveError(checked.http_status, checked.ClientMessage(), checked.ClientCode());
        return false;
      }
    }
    ++index;
    return true;
  };
  std::string remainder, encoded;
  size_t offset = 0;
  detail::SendBinaryResponse(fd, length, [&](std::string* output) {
    if (!encode_json) return fetch(output);
    if (read.manifest.chunks.empty()) {
      *output = "\"\"";
      return true;
    }
    while (offset == encoded.size()) {
      std::string bytes;
      if (index >= read.manifest.chunks.size() || !fetch(&bytes)) return false;
      bytes.insert(0, remainder);
      const bool last = index == read.manifest.chunks.size();
      const auto complete = last ? bytes.size() : bytes.size() / 3 * 3;
      remainder = bytes.substr(complete);
      if (!io::EncodeBase64(std::string_view(bytes).substr(0, complete),
                            2 * kBinaryChunkBytes, &encoded)) return false;
      if (index == 1) encoded.insert(0, 1, '"');
      if (last) encoded.push_back('"');
      offset = 0;
    }
    output->assign(encoded, offset, kBinaryChunkBytes);
    offset += output->size();
    return true;
  }, {owner.deadline}, mime_type);
}
}  // namespace q4t::server
