#include "q4t/server/file_webp.h"

#include <algorithm>
#include <cstring>

#include "file_webp_protocol.h"
#include "object_protocol.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
uint32_t Little(std::string_view bytes, size_t start, size_t length) {
  uint32_t value = 0;
  for (size_t i = 0; i < length; ++i)
    value |= uint32_t{static_cast<uint8_t>(bytes[start + i])} << (8 * i);
  return value;
}
ObjectReply Invalid() {
  return Error(400, "invalid_file_image", "Invalid WebP image container");
}
ObjectReply Capacity() {
  return Error(413, "file_image_capacity", "WebP image budget exceeded");
}
bool Limits(const FileWebpLimits& limits) {
  return limits.max_pixels && limits.max_pixels <= (256ULL << 20) &&
         limits.max_edge && limits.max_edge <= 65536 &&
         limits.max_output_bytes >= 1024 &&
         limits.max_output_bytes <= (256ULL << 20) && limits.timeout_ms >= 1 &&
         limits.timeout_ms <= 30000;
}
ObjectReply Admitted(std::string_view bytes, const FileWebpLimits& limits,
                     WebpHeader* header) {
  if (!Limits(limits))
    return Error(500, "file_image_budget_invalid", "Invalid WebP budget");
  if (bytes.size() > kFileInputBytes) return Capacity();
  auto reply = ReadWebpHeader(bytes, header);
  if (!reply.ok()) return reply;
  if (header->width > limits.max_edge || header->height > limits.max_edge ||
      uint64_t{header->width} * header->height > limits.max_pixels)
    return Capacity();
  return {};
}
bool Sha(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
bool Version(std::string_view value) {
  return !value.empty() && value.size() < 64 &&
         std::all_of(value.begin(), value.end(),
                     [](unsigned char c) { return c >= 33 && c <= 126; });
}
bool Codec(const Json* value, std::string_view name) {
  return value && value->IsObject() && value->object.size() == 3 &&
         value->GetString("name") == name &&
         Version(value->GetString("version")) &&
         Sha(value->GetString("sha256"));
}
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "WebP preparation cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "WebP preparation timed out");
  if (failure == GrammarFailure::kResource) return Capacity();
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_document_worker_capacity",
                 "Image codec slot occupied");
  return Error(503, "file_image_worker_failed", "Image codec worker failed");
}
}  // namespace

bool IsWebpImage(std::string_view bytes) {
  return bytes.size() >= 12 && bytes.substr(0, 4) == "RIFF" &&
         bytes.substr(8, 4) == "WEBP";
}
ObjectReply ReadWebpHeader(std::string_view bytes, WebpHeader* header) {
  if (!header) return Invalid();
  *header = {};
  if (!IsWebpImage(bytes) || uint64_t{Little(bytes, 4, 4)} + 8 != bytes.size())
    return Invalid();
  WebpHeader canvas, image;
  bool extended = false, alpha = false, have_image = false;
  uint8_t flags = 0;
  size_t chunks = 0;
  for (size_t position = 12; position < bytes.size();) {
    if (++chunks > 65536 || bytes.size() - position < 8) return Invalid();
    const auto type = bytes.substr(position, 4);
    const uint64_t size = Little(bytes, position + 4, 4);
    position += 8;
    if (size + (size & 1) > bytes.size() - position ||
        ((size & 1) && bytes[position + size] != 0))
      return Invalid();
    const auto data = bytes.substr(position, size);
    position += size + (size & 1);
    if (type == "VP8X") {
      if (extended || chunks != 1 || size != 10) return Invalid();
      flags = static_cast<uint8_t>(data[0]);
      if ((flags & 0xc1) || Little(data, 1, 3)) return Invalid();
      if (flags & 2)
        return Error(400, "media_animation_unsupported",
                     "Animated WebP is not a still image");
      canvas = {Little(data, 4, 3) + 1, Little(data, 7, 3) + 1};
      extended = true;
    } else if (type == "ANIM" || type == "ANMF") {
      return Error(400, "media_animation_unsupported",
                   "Animated WebP is not a still image");
    } else if (type == "ALPH") {
      if (!extended || !(flags & 0x10) || alpha || have_image || !size)
        return Invalid();
      alpha = true;
    } else if (type == "VP8 " || type == "VP8L") {
      if (have_image) return Invalid();
      if (type == "VP8 ") {
        if (size < 10 || (data[0] & 1) || data.substr(3, 3) != "\x9d\x01\x2a")
          return Invalid();
        image = {Little(data, 6, 2) & 0x3fff, Little(data, 8, 2) & 0x3fff};
      } else {
        if (alpha || size < 5 || static_cast<uint8_t>(data[0]) != 0x2f)
          return Invalid();
        const uint32_t bits = Little(data, 1, 4);
        if (bits >> 29) return Invalid();
        image = {(bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1};
      }
      if (!image.width || !image.height) return Invalid();
      have_image = true;
    }
  }
  if (!have_image || (extended && (canvas.width != image.width ||
                                   canvas.height != image.height)))
    return Invalid();
  *header = image;
  return {};
}

ObjectReply ValidateWebpPixels(std::string_view source,
                               const FileWebpLimits& limits,
                               const FileWebpPixels& pixels,
                               WebpHeader* header) {
  if (!header) return Invalid();
  *header = {};
  WebpHeader expected;
  auto reply = Admitted(source, limits, &expected);
  if (!reply.ok()) return reply;
  if (pixels.png.size() > limits.max_output_bytes) return Capacity();
  vision::MediaHeader image;
  if (!vision::ReadMediaHeader(
          {reinterpret_cast<const uint8_t*>(pixels.png.data()),
           pixels.png.size()},
          &image) ||
      image.format != vision::MediaFormat::kPng ||
      image.width != expected.width || image.height != expected.height)
    return Invalid();
  const auto& data = pixels.conversion;
  if (!data.IsObject() || data.object.size() != 7 ||
      data.GetString("format") != "q4t.webp_rgb_png.v1" ||
      data.GetString("source_sha256") != Digest(source) ||
      data.GetString("output_sha256") != Digest(pixels.png) ||
      data.GetInt("width", -1) != expected.width ||
      data.GetInt("height", -1) != expected.height ||
      !Codec(data.Find("decoder"), "libwebp") ||
      !Codec(data.Find("encoder"), "zlib"))
    return Invalid();
  *header = expected;
  return {};
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetWebpWorkerEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif
ObjectReply DecodeWebpImage(std::string_view source,
                            const FileWebpLimits& limits,
                            const BinaryAccess& access, FileWebpPixels* pixels,
                            GrammarControl control) {
  if (!pixels) return Invalid();
  *pixels = {};
  WebpHeader header;
  auto reply = Admitted(source, limits, &header);
  if (!reply.ok()) return reply;
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated image scope required");
  const auto cancelled = [&] {
    return (access.cancelled && access.cancelled()) ||
           (control.cancelled && control.cancelled());
  };
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  const auto deadline =
      std::min(access.deadline,
               Clock::now() + std::chrono::milliseconds(limits.timeout_ms));
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline = deadline;
  // Remains independent of the CUDA owner and uses the existing fixed child
  // slot, cancellation, parent-death signal and unreaped cleanup ownership.
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  const char* entry = "--file-webp-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
    return Failed(failure);
  webp_detail::Request request{webp_detail::kMagic, source.size(),
                               limits.max_pixels, limits.max_edge,
                               limits.max_output_bytes};
  webp_detail::Result result;
  webp_detail::Ready ready{};
  auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, deadline, cancelled);
  };
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(source.data()), source.size(), true) ||
      !transfer(&ready, sizeof(ready), false))
    return Failed(process->Failure());
  if (ready == webp_detail::Ready::kDependency)
    return Error(503, "file_image_dependency_unavailable",
                 "WebP codec unavailable");
  if (ready == webp_detail::Ready::kSandbox)
    return Error(503, "file_image_isolation_unavailable",
                 "WebP isolation unavailable");
  if (ready == webp_detail::Ready::kCapacity) return Capacity();
  if (ready == webp_detail::Ready::kInvalid) return Invalid();
  if (ready != webp_detail::Ready::kReady)
    return Failed(GrammarFailure::kWorker);
  if (control.worker_ready) control.worker_ready();
  if (!transfer(&result, sizeof(result), false))
    return Failed(process->Failure());
  if (result.magic != webp_detail::kMagic)
    return Failed(GrammarFailure::kWorker);
  if (result.failure == webp_detail::Failure::kDependency)
    return Error(503, "file_image_dependency_unavailable",
                 "WebP codec unavailable");
  if (result.failure == webp_detail::Failure::kSandbox)
    return Error(503, "file_image_isolation_unavailable",
                 "WebP isolation unavailable");
  if (result.failure == webp_detail::Failure::kCapacity) return Capacity();
  if (result.failure == webp_detail::Failure::kInvalid) return Invalid();
  if (result.failure != webp_detail::Failure::kNone || !result.bytes ||
      result.bytes > limits.max_output_bytes ||
      strnlen(result.decoder_version, 64) >= 64 ||
      strnlen(result.encoder_version, 64) >= 64 ||
      strnlen(result.decoder_sha256, 65) != 64 ||
      strnlen(result.encoder_sha256, 65) != 64)
    return Failed(GrammarFailure::kWorker);
  FileWebpPixels next;
  next.png.resize(result.bytes);
  if (!transfer(next.png.data(), next.png.size(), false))
    return Failed(process->Failure());
  std::string trailing;
  if (!process->CollectOutput(&trailing, 0, deadline, cancelled))
    return Failed(process->Failure());
  const auto codec = [](const char* name, const char* version,
                        const char* sha) {
    return Object({{"name", String(name)},
                   {"version", String(version)},
                   {"sha256", String(sha)}});
  };
  next.conversion =
      Object({{"format", String("q4t.webp_rgb_png.v1")},
              {"source_sha256", String(Digest(source))},
              {"output_sha256", String(Digest(next.png))},
              {"width", Number(header.width)},
              {"height", Number(header.height)},
              {"decoder",
               codec("libwebp", result.decoder_version, result.decoder_sha256)},
              {"encoder",
               codec("zlib", result.encoder_version, result.encoder_sha256)}});
  reply = ValidateWebpPixels(source, limits, next, &header);
  if (!reply.ok()) return Failed(GrammarFailure::kWorker);
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  *pixels = std::move(next);
  return {};
}
}  // namespace q4t::server
