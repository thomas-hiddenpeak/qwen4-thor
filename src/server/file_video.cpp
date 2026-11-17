#include "q4t/server/file_video.h"

#include <algorithm>

#include "file_video_affine.h"
#include "file_video_geometry.h"
#include "file_video_protocol.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/media_admission.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
ObjectReply Invalid() {
  return Error(400, "invalid_file_video", "Invalid video container or frames");
}
ObjectReply Capacity() {
  return Error(413, "file_video_capacity", "Video preparation budget exceeded");
}
ObjectReply Dependency() {
  return Error(503, "file_video_dependency_unavailable",
               "Video codec unavailable");
}
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Video preparation cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Video preparation timed out");
  if (failure == GrammarFailure::kResource) return Capacity();
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_document_worker_capacity",
                 "Video codec slot occupied");
  return Error(503, "file_video_worker_failed", "Video codec worker failed");
}
bool Sha(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= 'a' && c <= 'f') || (c >= '0' && c <= '9');
         });
}
bool Matrices(const Json& display, video_detail::DisplayMatrices* output) {
  for (const auto& [name, matrix] :
       {std::pair{"movie_matrix", &output->movie},
        std::pair{"track_matrix", &output->track}}) {
    const auto* raw = display.GetArray(name);
    if (!raw || raw->array.size() != matrix->size()) return false;
    for (size_t i = 0; i < matrix->size(); ++i) {
      const auto& value = raw->array[i];
      if (!IsInputInteger(value) || value.number < INT32_MIN ||
          value.number > INT32_MAX)
        return false;
      (*matrix)[i] = int32_t(value.number);
    }
    if (!video_detail::IsAffine(*matrix)) return false;
  }
  return true;
}
uint64_t Big(std::string_view bytes) {
  uint64_t value = 0;
  for (unsigned char byte : bytes) value = (value << 8) | byte;
  return value;
}
// Only container lengths are inspected here. Codecs still own demuxing and
// payload validation; this prevents a known incomplete header becoming a
// stalled downstream sink with no video pad.
bool Element(std::string_view bytes, size_t* offset, uint64_t* id,
             uint64_t* length, bool* unknown) {
  const auto vint = [&](bool identifier, uint64_t* value,
                        size_t* width) -> bool {
    if (*offset >= bytes.size()) return false;
    const auto first = static_cast<unsigned char>(bytes[*offset]);
    unsigned char mask = 0x80;
    *width = 1;
    while (mask && !(first & mask)) {
      mask >>= 1;
      ++*width;
    }
    if (!mask || *width > (identifier ? 4 : 8) ||
        *width > bytes.size() - *offset)
      return false;
    *value = Big(bytes.substr(*offset, *width));
    if (!identifier) *value &= (uint64_t{1} << (7 * *width)) - 1;
    *offset += *width;
    return true;
  };
  size_t width;
  if (!vint(true, id, &width) || !vint(false, length, &width)) return false;
  *unknown = *length == (uint64_t{1} << (7 * width)) - 1;
  return true;
}
bool ContainerLengths(std::string_view bytes) {
  size_t offset = 0, elements = 0;
  if (bytes.size() >= 12 && bytes.substr(4, 4) == "ftyp") {
    bool movie = false, media = false;
    while (offset < bytes.size()) {
      if (++elements > 65536 || bytes.size() - offset < 8) return false;
      auto size = Big(bytes.substr(offset, 4));
      const auto type = bytes.substr(offset + 4, 4);
      size_t header = 8;
      if (size == 1) {
        if (bytes.size() - offset < 16) return false;
        size = Big(bytes.substr(offset + 8, 8));
        header = 16;
      } else if (!size) {
        size = bytes.size() - offset;
      }
      if (size < header || size > bytes.size() - offset ||
          (!offset && size < header + 8))
        return false;
      movie |= type == "moov";
      media |= type == "mdat";
      offset += size;
    }
    return movie && media;
  }
  uint64_t id = 0, length = 0;
  bool unknown = false;
  if (!Element(bytes, &offset, &id, &length, &unknown) || id != 0x1a45dfa3 ||
      unknown || length > bytes.size() - offset)
    return false;
  offset += length;
  while (offset < bytes.size()) {
    if (++elements > 65536 ||
        !Element(bytes, &offset, &id, &length, &unknown) ||
        (!unknown && length > bytes.size() - offset))
      return false;
    if (id == 0x18538067) {
      const size_t end = unknown ? bytes.size() : offset + length;
      while (offset < end) {
        if (++elements > 65536 ||
            !Element(bytes.substr(0, end), &offset, &id, &length, &unknown) ||
            (!unknown && length > end - offset))
          return false;
        // Streaming Clusters can have an unknown size. The codec must parse
        // their children and owns the remaining bounded deadline.
        if (unknown) return id == 0x1f43b675;
        offset += length;
      }
      return offset == bytes.size();
    }
    if (unknown || (id != 0xec && id != 0xbf)) return false;
    offset += length;
  }
  return false;
}
}  // namespace
bool IsVideoContainer(std::string_view bytes) {
  if (bytes.size() >= 12 && bytes.substr(4, 4) == "ftyp") {
    const auto brand = bytes.substr(8, 4);
    for (auto image :
         {"avif", "avis", "heic", "heix", "hevc", "hevx", "mif1", "msf1"})
      if (brand == image) return false;
    return true;
  }
  return bytes.size() >= 4 && bytes.substr(0, 4) == "\x1a\x45\xdf\xa3";
}
bool ValidFileVideoLimits(const FileVideoLimits& l) {
  return l.max_source_bytes && l.max_source_bytes <= kFileInputBytes &&
         l.max_edge && l.max_edge <= 8192 && l.max_frame_pixels &&
         l.max_frame_pixels <= (32ULL << 20) && l.max_decoded_pixels &&
         l.max_decoded_pixels <= (4ULL << 30) && l.max_output_bytes >= 1024 &&
         l.max_output_bytes <= (128ULL << 20) && l.max_duration_ns &&
         l.max_duration_ns <= 600000000000ULL && l.max_scanned_frames >= 2 &&
         l.max_scanned_frames <= 18000 && l.sample_frames >= 2 &&
         l.sample_frames <= 16 && l.max_thread_creations <= 64 &&
         l.timeout_ms >= 1 && l.timeout_ms <= 30000;
}
ObjectReply ValidateVideoPixels(std::string_view source,
                                const FileVideoLimits& limits,
                                const FileVideoPixels& pixels) {
  if (!IsVideoContainer(source) ||
      pixels.conversion.GetInt("source_bytes", -1) !=
          static_cast<int64_t>(source.size()))
    return Invalid();
  if (pixels.conversion.GetString("format") == "q4t.cpu_video_png.v4") {
    const auto* display = pixels.conversion.Find("display_geometry");
    video_detail::DisplayMatrices recorded;
    if (!display || !Matrices(*display, &recorded)) return Invalid();
    try {
      const auto actual = video_detail::MovieDisplay().ReadMatrices(source);
      if (!actual || *actual != recorded) return Invalid();
    } catch (...) {
      return Invalid();
    }
  }
  return ValidateVideoSnapshot(Digest(source), limits, pixels);
}
ObjectReply ValidateVideoSnapshot(std::string_view source_sha256,
                                  const FileVideoLimits& limits,
                                  const FileVideoPixels& pixels) {
  if (!ValidFileVideoLimits(limits))
    return Error(500, "file_video_budget_invalid", "Invalid video budget");
  if (!Sha(source_sha256) || !pixels.width || !pixels.height ||
      !pixels.duration_ns || pixels.frames.size() < 2 ||
      pixels.scanned_frames < pixels.frames.size())
    return Invalid();
  const auto* source_bytes = pixels.conversion.Find("source_bytes");
  if (!source_bytes || !IsInputInteger(*source_bytes) ||
      source_bytes->number <= 0)
    return Invalid();
  if (source_bytes->number > limits.max_source_bytes ||
      pixels.width > limits.max_edge || pixels.height > limits.max_edge ||
      uint64_t{pixels.width} * pixels.height > limits.max_frame_pixels ||
      pixels.duration_ns > limits.max_duration_ns ||
      pixels.scanned_frames > limits.max_scanned_frames ||
      uint64_t{pixels.width} * pixels.height >
          limits.max_decoded_pixels / pixels.scanned_frames ||
      pixels.frames.size() > limits.sample_frames)
    return Capacity();
  const auto& conversion = pixels.conversion;
  if (OutputJson(conversion).size() > 16384) return Invalid();
  const auto* outputs = conversion.GetArray("frames");
  const auto* modules = conversion.GetArray("modules");
  const bool affine = conversion.GetString("format") == "q4t.cpu_video_png.v4";
  const bool geometry =
      affine || conversion.GetString("format") == "q4t.cpu_video_png.v3";
  const bool intervals =
      geometry || conversion.GetString("format") == "q4t.cpu_video_png.v2";
  if ((!intervals &&
       conversion.GetString("format") != "q4t.cpu_video_png.v1") ||
      (!geometry && conversion.Has("display_geometry")) ||
      conversion.GetString("source_sha256") != source_sha256 ||
      conversion.GetString("sampling") !=
          "uniform-presentation-time-with-endpoints" ||
      !outputs || outputs->array.size() != pixels.frames.size() || !modules ||
      modules->array.empty() || modules->array.size() > 16)
    return Invalid();
  if (geometry) {
    const auto* display = conversion.Find("display_geometry");
    if (!display || !display->IsObject() ||
        display->object.size() != (affine ? 7U : 5U) ||
        display->GetString("policy") !=
            (affine ? "affine-square-pixels-tight-bilinear16"
                    : "source-orientation-square-pixels"))
      return Invalid();
    for (const char* name : {"coded_width", "coded_height", "pixel_aspect_num",
                             "pixel_aspect_den"}) {
      const auto* value = display->Find(name);
      if (!value || !IsInputInteger(*value)) return Invalid();
    }
    video_detail::DisplayGeometry shape;
    const auto failure = video_detail::SquarePixelGeometry(
        display->GetInt("coded_width"), display->GetInt("coded_height"),
        display->GetInt("pixel_aspect_num"),
        display->GetInt("pixel_aspect_den"), limits, &shape);
    if (failure == video_detail::Failure::kCapacity) return Capacity();
    if (failure != video_detail::Failure::kNone) return Invalid();
    if (uint64_t(shape.square_width) * shape.square_height >
        limits.max_decoded_pixels / pixels.scanned_frames)
      return Capacity();
    if (affine) {
      video_detail::DisplayMatrices matrices;
      video_detail::AffineGeometry transformed;
      if (!Matrices(*display, &matrices)) return Invalid();
      const auto status = video_detail::MakeAffineGeometry(
          matrices, shape.square_width, shape.square_height, limits,
          &transformed);
      if (status == video_detail::Failure::kCapacity) return Capacity();
      if (status != video_detail::Failure::kNone ||
          transformed.width != pixels.width ||
          transformed.height != pixels.height)
        return Invalid();
    } else if (!((pixels.width == shape.square_width &&
                  pixels.height == shape.square_height) ||
                 (pixels.width == shape.square_height &&
                  pixels.height == shape.square_width)))
      return Invalid();
  }
  for (const auto& [name, expected] :
       std::vector<std::pair<const char*, uint64_t>>{
           {"width", pixels.width},
           {"height", pixels.height},
           {"duration_ns", pixels.duration_ns},
           {"scanned_frames", pixels.scanned_frames}}) {
    const auto* value = conversion.Find(name);
    if (!value || !IsInputInteger(*value) || value->number != expected)
      return Invalid();
  }
  for (const auto& module : modules->array) {
    const auto name = module.GetString("name"),
               version = module.GetString("version");
    if (name.empty() || name.size() > 64 || version.empty() ||
        version.size() > 64 || !Sha(module.GetString("sha256")))
      return Invalid();
  }
  uint64_t bytes = 0, previous_time = 0, previous_index = 0;
  for (size_t i = 0; i < pixels.frames.size(); ++i) {
    const auto& frame = pixels.frames[i];
    vision::MediaHeader header;
    if (frame.png.size() > limits.max_output_bytes - bytes) return Capacity();
    bytes += frame.png.size();
    if (!vision::ReadMediaHeader(
            {reinterpret_cast<const uint8_t*>(frame.png.data()),
             frame.png.size()},
            &header) ||
        header.format != vision::MediaFormat::kPng ||
        header.width != pixels.width || header.height != pixels.height ||
        frame.timestamp_ns >= pixels.duration_ns ||
        frame.duration_ns > pixels.duration_ns - frame.timestamp_ns ||
        frame.source_index >= pixels.scanned_frames ||
        (i && (frame.timestamp_ns <= previous_time ||
               frame.source_index <= previous_index)) ||
        outputs->array[i].GetString("sha256") != Digest(frame.png) ||
        outputs->array[i].GetInt("timestamp_ns", -1) !=
            static_cast<int64_t>(frame.timestamp_ns) ||
        outputs->array[i].GetInt("source_index", -1) !=
            static_cast<int64_t>(frame.source_index))
      return Invalid();
    for (const auto* name : {"timestamp_ns", "source_index"}) {
      const auto* value = outputs->array[i].Find(name);
      if (!value || !IsInputInteger(*value)) return Invalid();
    }
    const auto* duration = outputs->array[i].Find("duration_ns");
    if (intervals) {
      if (!duration || !IsInputInteger(*duration) || duration->number < 0 ||
          duration->number != frame.duration_ns)
        return Invalid();
    } else if (duration || frame.duration_ns) {
      return Invalid();
    }
    previous_time = frame.timestamp_ns;
    previous_index = frame.source_index;
  }
  if (pixels.frames.front().source_index != 0 ||
      pixels.frames.back().source_index + 1 != pixels.scanned_frames)
    return Invalid();
  return {};
}
Status MapPreparedVideoContent(const io::Json& part, io::Json* mapped,
                               std::string* error_param) {
  if (!mapped) return Status::Fail("Missing prepared video owner");
  *mapped = {};
  auto status = InputFields(part,
                            {"type", "video_frames", "video_timestamps",
                             "detail", "prompt_cache_breakpoint"},
                            error_param);
  const auto* frames = part.GetArray("video_frames");
  if (!status) return status;
  status = VideoInputDetail(part);
  if (!status) {
    if (error_param) *error_param = "detail";
    return status;
  }
  if (part.GetString("type") != "input_video_frames" || !frames ||
      frames->array.size() < 2 || frames->array.size() > 16 ||
      !part.Has("video_timestamps"))
    return Status::Fail("Invalid prepared video frames");
  for (const auto& frame : frames->array)
    if (!frame.IsString() || !frame.str.starts_with("data:image/png;base64,") ||
        frame.str.size() <= 22)
      return Status::Fail("Prepared video requires immutable PNG data");
  std::string field;
  status = VideoFrameTimes(part, frames->array.size(), nullptr, &field);
  if (!status) {
    if (error_param) *error_param = std::move(field);
    return status;
  }
  *mapped = part;
  Set(mapped, "type", String("video_frames"));
  return {};
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetVideoWorkerEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif
ObjectReply DecodeVideoContainer(std::string_view source,
                                 const FileVideoLimits& limits,
                                 const BinaryAccess& access,
                                 FileVideoPixels* pixels,
                                 GrammarControl control) {
  if (!pixels) return Invalid();
  *pixels = {};
  if (!ValidFileVideoLimits(limits))
    return Error(500, "file_video_budget_invalid", "Invalid video budget");
  if (source.size() > limits.max_source_bytes) return Capacity();
  if (!IsVideoContainer(source) || !ContainerLengths(source)) return Invalid();
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated video scope required");
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
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  const char* entry = "--file-video-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
    return Failed(failure);
  video_detail::Request request{video_detail::kMagic, source.size(), limits};
  video_detail::Ready ready{};
  auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, deadline, cancelled);
  };
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(source.data()), source.size(), true) ||
      !transfer(&ready, sizeof(ready), false))
    return Failed(process->Failure());
  if (ready == video_detail::Ready::kDependency) return Dependency();
  if (ready == video_detail::Ready::kSandbox)
    return Error(503, "file_video_isolation_unavailable",
                 "Video isolation unavailable");
  if (ready == video_detail::Ready::kInvalid) return Invalid();
  if (ready != video_detail::Ready::kReady)
    return Failed(GrammarFailure::kWorker);
  if (!process->ReceiveIoBudget(
          {.write_bytes = 0,
           .file_creations = 0,
           .thread_creations = limits.max_thread_creations,
           .notifications = 128},
          deadline, cancelled))
    return Failed(process->Failure());
  if (control.worker_ready) control.worker_ready();
  video_detail::Result result;
  if (!transfer(&result, sizeof(result), false))
    return Failed(process->Failure());
  if (result.magic != video_detail::kMagic)
    return Failed(GrammarFailure::kWorker);
  if (result.failure == video_detail::Failure::kDependency) return Dependency();
  if (result.failure == video_detail::Failure::kCapacity) return Capacity();
  if (result.failure == video_detail::Failure::kInvalid) return Invalid();
  if (result.failure != video_detail::Failure::kNone || result.frames < 2 ||
      result.frames > limits.sample_frames || !result.metadata_bytes ||
      result.metadata_bytes > 16384 || result.width > limits.max_edge ||
      result.height > limits.max_edge)
    return Failed(GrammarFailure::kWorker);
  std::string metadata(result.metadata_bytes, '\0');
  if (!transfer(metadata.data(), metadata.size(), false))
    return Failed(process->Failure());
  FileVideoPixels next;
  if (!io::ParseJson(metadata, &next.conversion, true,
                     {.max_depth = 8,
                      .max_values = 512,
                      .max_string_bytes = 16384,
                      .reject_duplicate_keys = true}))
    return Failed(GrammarFailure::kWorker);
  next.width = result.width;
  next.height = result.height;
  next.duration_ns = result.duration_ns;
  next.scanned_frames = result.scanned_frames;
  uint64_t bytes = 0;
  for (size_t i = 0; i < result.frames; ++i) {
    video_detail::Frame frame;
    if (!transfer(&frame, sizeof(frame), false))
      return Failed(process->Failure());
    if (!frame.bytes || frame.bytes > limits.max_output_bytes - bytes)
      return Failed(GrammarFailure::kWorker);
    bytes += frame.bytes;
    FileVideoFrame decoded{std::string(frame.bytes, '\0'), frame.timestamp_ns,
                           frame.source_index, frame.duration_ns};
    if (!transfer(decoded.png.data(), decoded.png.size(), false))
      return Failed(process->Failure());
    next.frames.push_back(std::move(decoded));
  }
  std::string trailing;
  if (!process->CollectOutput(&trailing, 0, deadline, cancelled))
    return Failed(process->Failure());
  const auto valid = ValidateVideoPixels(source, limits, next);
  if (!valid.ok()) return Failed(GrammarFailure::kWorker);
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  *pixels = std::move(next);
  return {};
}
}  // namespace q4t::server
