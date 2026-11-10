#include "q4t/server/file_image.h"

#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid() {
  return Error(400, "invalid_file_image",
               "Invalid or inconsistent image input");
}
ObjectReply Corrupt() {
  return Error(503, "file_image_snapshot_inconsistent",
               "Stored image input is inconsistent");
}
ObjectReply Active(const BinaryAccess& access,
                   const GrammarControl& control = {}) {
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Error(499, "request_cancelled", "Image preparation cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Image preparation expired");
  return {};
}
bool Utf8(const std::string& value, size_t limit) {
  return value.size() <= limit && detail::NormalizeUtf8(value) == value;
}
ObjectReply Check(const FileImageInput& image, const FileImageLimits& limits,
                  vision::MediaHeader* result) {
  if (!limits.max_bytes || limits.max_bytes > kFileInputBytes ||
      !limits.max_pixels || limits.max_pixels > (256ULL << 20) ||
      !limits.max_edge || limits.max_edge > 65536)
    return Error(500, "file_image_budget_invalid", "Invalid image budget");
  const auto& source = image.source;
  if (source.bytes.size() > limits.max_bytes)
    return Error(413, "file_image_capacity", "Image byte budget exceeded");
  const auto bytes =
      std::span(reinterpret_cast<const uint8_t*>(source.bytes.data()),
                source.bytes.size());
  vision::MediaHeader header;
  if (!vision::ReadMediaHeader(bytes, &header) ||
      source.sha256 != Digest(source.bytes) || !Utf8(source.filename, 1024) ||
      !Utf8(source.mime_type, 255) ||
      (!source.file_id.empty() && !ValidId(source.file_id)) ||
      image.urls.size() > 11 ||
      (image.detail != "auto" && image.detail != "low" &&
       image.detail != "high" && image.detail != "original"))
    return Invalid();
  if (header.width > limits.max_edge || header.height > limits.max_edge ||
      uint64_t{header.width} * header.height > limits.max_pixels)
    return Error(413, "file_image_capacity", "Image pixel budget exceeded");
  for (const auto& url : image.urls)
    if (url.empty() || !Utf8(url, 8192)) return Invalid();
  *result = header;
  return {};
}
}  // namespace
ObjectReply ReadFileImage(ObjectStore* objects, const BinaryAccess& access,
                          const FileInputSource& source,
                          const FileFetchOptions& fetch,
                          const FileImageLimits& limits,
                          FileInputBudget* budget, FileImageInput* image,
                          GrammarControl control) {
  if (!image || !budget) return Invalid();
  *image = {};
  FileImageInput next;
  next.detail = source.detail;
  auto reply = Active(access, control);
  if (!reply.ok()) return reply;
  if (source.kind == FileInputSource::Kind::kUrl) {
    FileFetchReceipt fetched;
    reply = FetchFileInput(access, source, fetch, budget, &fetched, control);
    if (!reply.ok()) return reply;
    next.source = std::move(fetched.content);
    next.urls = std::move(fetched.urls);
  } else {
    auto bounded = access;
    bounded.cancelled = [&] {
      return (access.cancelled && access.cancelled()) ||
             (control.cancelled && control.cancelled());
    };
    reply = ReadLocalFileInput(objects, bounded, source, budget, &next.source);
    if (!reply.ok()) return reply;
  }
  reply = Check(next, limits, &next.header);
  if (!reply.ok()) return reply;
  if (!source.mime_type.empty() &&
      source.kind == FileInputSource::Kind::kInline &&
      source.mime_type != vision::MediaFormatMime(next.header.format))
    return Error(400, "media_format_mismatch",
                 "Image data URI type disagrees with its bytes");
  reply = Active(access, control);
  if (!reply.ok()) return reply;
  *image = std::move(next);
  return {};
}
ObjectReply SaveFileImageSnapshot(
    InputSnapshotStore& store, const BinaryAccess& access,
    std::string_view binding_sha256, const FileImageInput& image,
    const FileImageLimits& limits, const InputSnapshotLimits& storage,
    int hold_seconds, InputSnapshotRef* reference, InputSnapshotHold* hold) {
  // Check recomputes the header from bytes, not caller metadata. Avoid copying
  // compressed content just for validation.
  vision::MediaHeader checked;
  auto reply = Active(access);
  if (reply.ok()) reply = Check(image, limits, &checked);
  if (!reply.ok()) return reply;
  if (checked.width != image.header.width ||
      checked.height != image.header.height ||
      checked.format != image.header.format)
    return Invalid();
  const auto& source = image.source;
  auto urls = Array();
  for (const auto& url : image.urls) urls.array.push_back(String(url));
  const auto metadata = Object(
      {{"format", String("q4t.image_input_snapshot.v1")},
       {"filename", String(source.filename)},
       {"mime_type", String(source.mime_type)},
       {"file_id", String(source.file_id)},
       {"sha256", String(source.sha256)},
       {"urls", std::move(urls)},
       {"detail", String(image.detail)},
       {"width", Number(image.header.width)},
       {"height", Number(image.header.height)},
       {"encoding", String(vision::MediaFormatName(image.header.format))}});
  const std::string_view bytes(source.bytes);
  return store.Save(access, binding_sha256, metadata, std::span(&bytes, 1),
                    storage, hold_seconds, reference, hold);
}
ObjectReply ReadFileImageSnapshot(InputSnapshotStore& store,
                                  const BinaryAccess& access,
                                  const InputSnapshotRef& reference,
                                  std::string_view binding_sha256,
                                  const FileImageLimits& limits,
                                  const InputSnapshotLimits& storage,
                                  FileImageInput* image) {
  if (!image) return Invalid();
  *image = {};
  InputSnapshot snapshot;
  auto reply =
      store.Read(access, reference, binding_sha256, storage, &snapshot);
  if (!reply.ok()) return reply;
  const auto& data = snapshot.metadata;
  const auto* urls = data.GetArray("urls");
  if (snapshot.parts.size() != 1 || !data.IsObject() ||
      data.object.size() != 10 ||
      data.GetString("format") != "q4t.image_input_snapshot.v1" || !urls ||
      urls->array.size() > 11)
    return Corrupt();
  for (const auto* key :
       {"filename", "mime_type", "file_id", "sha256", "detail", "encoding"})
    if (!data.Find(key) || !data.Find(key)->IsString()) return Corrupt();
  FileImageInput next;
  next.source = {std::move(snapshot.parts[0]), data.GetString("filename"),
                 data.GetString("mime_type"), data.GetString("file_id"),
                 data.GetString("sha256")};
  next.detail = data.GetString("detail");
  for (const auto& url : urls->array) {
    if (!url.IsString()) return Corrupt();
    next.urls.push_back(url.str);
  }
  reply = Check(next, limits, &next.header);
  if (!reply.ok())
    return reply.code == "invalid_file_image" ? Corrupt() : reply;
  if (data.GetInt("width", -1) != next.header.width ||
      data.GetInt("height", -1) != next.header.height ||
      data.GetString("encoding") != vision::MediaFormatName(next.header.format))
    return Corrupt();
  reply = Active(access);
  if (!reply.ok()) return reply;
  *image = std::move(next);
  return {};
}
}  // namespace q4t::server
