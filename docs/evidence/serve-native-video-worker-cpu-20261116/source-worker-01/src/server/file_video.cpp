#include "q4t/server/file_video.h"

#include <algorithm>

#include "file_video_protocol.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
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
}  // namespace
bool IsVideoContainer(std::string_view bytes) {
  return (bytes.size() >= 12 && bytes.substr(4, 4) == "ftyp") ||
         (bytes.size() >= 4 && bytes.substr(0, 4) == "\x1a\x45\xdf\xa3");
}
bool ValidFileVideoLimits(const FileVideoLimits& l) {
  return l.max_source_bytes && l.max_source_bytes <= kFileInputBytes &&
         l.max_edge && l.max_edge <= 8192 && l.max_frame_pixels &&
         l.max_frame_pixels <= (32ULL << 20) && l.max_decoded_pixels &&
         l.max_decoded_pixels <= (4ULL << 30) && l.max_output_bytes >= 1024 &&
         l.max_output_bytes <= (128ULL << 20) && l.max_duration_ns &&
         l.max_duration_ns <= 600000000000ULL && l.max_scanned_frames >= 2 &&
         l.max_scanned_frames <= 18000 && l.sample_frames >= 2 &&
         l.sample_frames <= 16 && l.timeout_ms >= 1 && l.timeout_ms <= 30000;
}
ObjectReply ValidateVideoPixels(std::string_view source,
                                const FileVideoLimits& limits,
                                const FileVideoPixels& pixels) {
  if (!ValidFileVideoLimits(limits))
    return Error(500, "file_video_budget_invalid", "Invalid video budget");
  if (!IsVideoContainer(source) || !pixels.width || !pixels.height ||
      !pixels.duration_ns || pixels.frames.size() < 2 ||
      pixels.scanned_frames < pixels.frames.size())
    return Invalid();
  if (source.size() > limits.max_source_bytes ||
      pixels.width > limits.max_edge || pixels.height > limits.max_edge ||
      uint64_t{pixels.width} * pixels.height > limits.max_frame_pixels ||
      pixels.duration_ns > limits.max_duration_ns ||
      pixels.scanned_frames > limits.max_scanned_frames ||
      uint64_t{pixels.width} * pixels.height >
          limits.max_decoded_pixels / pixels.scanned_frames ||
      pixels.frames.size() > limits.sample_frames)
    return Capacity();
  const auto& conversion = pixels.conversion;
  const auto* outputs = conversion.GetArray("frames");
  const auto* modules = conversion.GetArray("modules");
  if (conversion.GetString("format") != "q4t.cpu_video_png.v1" ||
      conversion.GetString("source_sha256") != Digest(source) ||
      conversion.GetString("sampling") !=
          "uniform-presentation-time-with-endpoints" ||
      !outputs || outputs->array.size() != pixels.frames.size() || !modules ||
      modules->array.empty() || modules->array.size() > 16)
    return Invalid();
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
    previous_time = frame.timestamp_ns;
    previous_index = frame.source_index;
  }
  if (pixels.frames.front().source_index != 0 ||
      pixels.frames.back().source_index + 1 != pixels.scanned_frames)
    return Invalid();
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
  if (!IsVideoContainer(source)) return Invalid();
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
                           frame.source_index};
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
