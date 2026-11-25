#include "q4t/server/video_characters.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/file_video.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/multipart.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr std::string_view kFormat = "q4t.video_character.v1";

bool ValidName(std::string_view name) {
  if (name.empty() || name.size() > 320 || detail::NormalizeUtf8(name) != name)
    return false;
  return std::ranges::count_if(
             name, [](unsigned char c) { return (c & 0xc0) != 0x80; }) <= 80;
}

ObjectReply ReadForm(std::string_view type, std::string_view body,
                     std::string* name, std::string* video) {
  std::string boundary;
  if (!IsMultipartForm(type))
    return Error(415, "unsupported_media_type", "Expected a video form");
  if (!MultipartBoundary(type, &boundary))
    return Error(400, "invalid_request", "Invalid multipart boundary");
  const auto limit = FileVideoLimits{}.max_source_bytes;
  bool saw_name = false, saw_video = false, oversized = false;
  std::string* field = nullptr;
  MultipartCallbacks callbacks;
  callbacks.begin = [&](const MultipartPart& part) {
    if (part.name == "name" && !saw_name && !part.has_filename) {
      saw_name = true;
      field = name;
    } else if (part.name == "video" && !saw_video) {
      saw_video = true;
      field = video;
    } else {
      return Status::Fail("Duplicate or unknown character field");
    }
    return Status{};
  };
  callbacks.data = [&](std::string_view bytes) {
    const auto max = field == name ? 320 : limit;
    if (!field || bytes.size() > max - field->size()) {
      oversized = field == video;
      return Status::Fail("Character field exceeds its byte limit");
    }
    field->append(bytes);
    return Status{};
  };
  callbacks.end = [&] {
    field = nullptr;
    return Status{};
  };
  MultipartDecoder decoder(std::move(boundary), limit + 65536, 2,
                           std::move(callbacks));
  auto parsed = decoder.Feed(body);
  if (parsed) parsed = decoder.Finish();
  if (!parsed || !ValidName(*name) || video->empty()) {
    if (oversized || body.size() > limit + 65536)
      return Error(413, "request_too_large", "Video exceeds the upload limit");
    return Error(400, "invalid_request",
                 "Expected a video and a name of 1 to 80 characters");
  }
  return {};
}
}  // namespace

ObjectReply HandleVideoCharacter(ObjectStore& objects,
                                 const StateAccess& access,
                                 std::string_view method, std::string_view path,
                                 std::string_view content_type,
                                 std::string_view body) {
  if (path.find('?') != path.npos || !VideoCharacterMethodAllowed(method, path))
    return Error(400, "invalid_request", "Invalid character request");
  ObjectRequest request;
  request.kind = ObjectKind::kVideoCharacter;
  request.scope = access.scope;
  if (method == "GET") {
    if (!body.empty())
      return Error(400, "invalid_request", "Character retrieval has no body");
    request.id = VideoCharacterId(path);
    auto row = objects.Execute(request, access.deadline, access.cancelled);
    if (!row.ok()) return row;
    const auto* data = row.value.Find("data");
    const auto* resource = data ? data->Find("resource") : nullptr;
    BinaryManifest manifest;
    const auto* blob = data ? data->Find("blob") : nullptr;
    const auto hash = data ? data->GetString("source_sha256") : std::string{};
    if (!resource || resource->object.size() != 3 || !blob ||
        !ParseBinaryManifest(*blob, &manifest) || !manifest.bytes ||
        hash.size() != 64 ||
        hash.find_first_not_of("0123456789abcdef") != hash.npos ||
        data->GetInt("width") < 1 || data->GetInt("height") < 1 ||
        data->GetInt("duration_ns") <= 0 ||
        data->GetString("format") != kFormat ||
        resource->GetString("id") != request.id ||
        !ValidName(resource->GetString("name")) ||
        resource->GetInt("created_at") <= 0 ||
        resource->GetInt("created_at") != row.value.GetInt("created_at"))
      return Error(503, "character_record_invalid", "Invalid stored character");
    ObjectReply result;
    result.value = *resource;
    return result;
  }
  std::string name, source;
  auto form = ReadForm(content_type, body, &name, &source);
  if (!form.ok()) return form;
  const BinaryAccess owner{access.scope, access.deadline, access.cancelled};
  FileVideoLimits limits;
  FileVideoPixels decoded;
  auto valid = DecodeVideoContainer(source, limits, owner, &decoded);
  if (!valid.ok()) return valid;
  request.id = NewObjectId(request.kind);
  if (request.id.empty())
    return Error(503, "character_id_unavailable",
                 "Could not create character ID");
  request.operation = ObjectOperation::kPut;
  request.expected_revision = 0;
  request.ttl_seconds = 0;
  request.created_at = std::time(nullptr);
  auto resource = Object({{"id", String(request.id)},
                          {"name", String(name)},
                          {"created_at", Number(request.created_at)}});
  // Keep original encoded bytes so subsequent video operations can condition
  // on the entire reference clip, including frames not used for inspection.
  request.data = Object({{"format", String(std::string(kFormat))},
                         {"resource", resource},
                         {"source_sha256", String(Digest(source))},
                         {"width", Number(decoded.width)},
                         {"height", Number(decoded.height)},
                         {"duration_ns", Number(decoded.duration_ns)}});
  BinaryStore store(objects);
  BinaryStage stage;
  auto result = store.Begin(owner, limits.max_source_bytes, 600, &stage);
  if (!result.ok()) return result;
  ScopeExit cleanup([&] {
    if (!stage.closed && !stage.commit_unknown) store.Abort(owner, &stage);
  });
  std::string_view remaining(source);
  while (!remaining.empty()) {
    auto chunk = remaining.substr(0, kBinaryChunkBytes);
    result = store.Append(owner, &stage, chunk);
    if (!result.ok()) return result;
    remaining.remove_prefix(chunk.size());
  }
  result = store.Publish(owner, &stage, std::move(request));
  if (result.ok()) result.value = std::move(resource);
  return result;
}
}  // namespace q4t::server
