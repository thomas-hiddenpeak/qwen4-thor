#include "q4t/server/file_audio.h"

#include <algorithm>
#include <bit>
#include <cmath>

#include "file_audio_protocol.h"
#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
ObjectReply Invalid() {
  return Error(400, "invalid_file_audio", "Invalid audio container or samples");
}
ObjectReply Capacity() {
  return Error(413, "file_audio_capacity", "Audio preparation budget exceeded");
}
ObjectReply Dependency() {
  return Error(503, "file_audio_dependency_unavailable",
               "Audio codec unavailable");
}
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Audio preparation cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Audio preparation timed out");
  if (failure == GrammarFailure::kResource) return Capacity();
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_audio_worker_capacity",
                 "Audio codec slot occupied");
  return Error(503, "file_audio_worker_failed", "Audio codec worker failed");
}
uint64_t Little(std::string_view bytes) {
  uint64_t value = 0;
  for (size_t i = 0; i < bytes.size(); ++i)
    value |= uint64_t{static_cast<unsigned char>(bytes[i])} << (i * 8);
  return value;
}
}  // namespace
namespace audio_detail {
Container Detect(std::string_view source) {
  if (source.size() < 12) return Container::kUnknown;
  if (source.substr(0, 4) == "RIFF" && source.substr(8, 4) == "WAVE")
    return Container::kWave;
  if (source.starts_with("fLaC")) return Container::kFlac;
  if (source.starts_with("OggS")) return Container::kOgg;
  if (source.substr(4, 4) == "ftyp") return Container::kMp4;
  if (source.starts_with("\x1a\x45\xdf\xa3")) return Container::kWebm;
  if (source.starts_with("ID3")) return Container::kMpeg;
  const auto a = static_cast<unsigned char>(source[0]);
  const auto b = static_cast<unsigned char>(source[1]);
  if (a == 0xff && (b & 0xf6) == 0xf0) return Container::kAdts;
  if (a == 0xff && (b & 0xe0) == 0xe0 && (b & 0x06)) return Container::kMpeg;
  return Container::kUnknown;
}
uint16_t WaveTag(std::string_view source) {
  if (Detect(source) != Container::kWave ||
      Little(source.substr(4, 4)) != source.size() - 8)
    return 0;
  uint16_t tag = 0;
  bool data = false;
  size_t offset = 12, chunks = 0;
  while (offset < source.size()) {
    if (++chunks > 4096 || source.size() - offset < 8) return 0;
    const auto type = source.substr(offset, 4);
    const auto size = Little(source.substr(offset + 4, 4));
    offset += 8;
    if (size > source.size() - offset) return 0;
    if (type == "fmt ") {
      if (tag || size < 16) return 0;
      tag = Little(source.substr(offset, 2));
      if (tag == 0xfffe) {
        if (size < 40 || Little(source.substr(offset + 16, 2)) < 22) return 0;
        constexpr char suffix[] = "\0\0\0\0\x10\0\x80\0\0\xaa\0\x38\x9b\x71";
        if (source.substr(offset + 26, 14) != std::string_view(suffix, 14))
          return 0;
        tag = Little(source.substr(offset + 24, 2));
      }
    } else if (type == "data") {
      if (data || !size) return 0;
      data = true;
    }
    const auto padded = size + (size & 1);
    if (padded > source.size() - offset) return 0;
    offset += padded;
  }
  return data ? tag : 0;
}
}  // namespace audio_detail
bool IsAudioContainer(std::string_view bytes) {
  const auto kind = audio_detail::Detect(bytes);
  return kind != audio_detail::Container::kUnknown &&
         (kind != audio_detail::Container::kWave ||
          audio_detail::WaveTag(bytes));
}
bool ValidFileAudioLimits(const FileAudioLimits& l) {
  return l.max_source_bytes && l.max_source_bytes <= (128ULL << 20) &&
         l.max_decoded_values && l.max_decoded_values <= (512ULL << 20) &&
         l.max_output_samples && l.max_output_samples <= 9600000 &&
         l.max_duration_ns && l.max_duration_ns <= 600000000000ULL &&
         l.max_sample_rate >= 8000 && l.max_sample_rate <= 384000 &&
         l.max_channels && l.max_channels <= 8 &&
         l.max_thread_creations <= 64 && l.timeout_ms > 0 &&
         l.timeout_ms <= 30000;
}
ObjectReply ValidateAudioPcm(std::string_view source,
                             const FileAudioLimits& limits,
                             const FileAudioPcm& pcm) {
  static_assert(std::endian::native == std::endian::little);
  if (!ValidFileAudioLimits(limits))
    return Error(500, "file_audio_budget_invalid", "Invalid audio budget");
  if (!IsAudioContainer(source) || pcm.samples.empty()) return Invalid();
  if (source.size() > limits.max_source_bytes ||
      pcm.samples.size() > limits.max_output_samples ||
      pcm.duration_ns > limits.max_duration_ns)
    return Capacity();
  const auto& meta = pcm.conversion;
  const auto rate = meta.GetInt("source_sample_rate", 0);
  const auto channels = meta.GetInt("source_channels", 0);
  const auto frames = meta.GetInt("source_frames", 0);
  const auto* modules = meta.GetArray("modules");
  const std::string_view bytes(
      reinterpret_cast<const char*>(pcm.samples.data()),
      pcm.samples.size() * sizeof(float));
  if (pcm.duration_ns != pcm.samples.size() * 62500ULL ||
      meta.GetString("format") != "q4t.cpu_audio_f32le.v1" ||
      meta.GetString("source_sha256") != Digest(source) ||
      meta.GetInt("source_bytes", -1) != int64_t(source.size()) ||
      meta.GetString("pcm_sha256") != Digest(bytes) ||
      meta.GetInt("samples", -1) != int64_t(pcm.samples.size()) ||
      meta.GetInt("sample_rate", -1) != audio_detail::kOutputRate ||
      meta.GetString("mono_policy") != "resample-channels-f64-mean" ||
      meta.GetInt("resample_quality", -1) != 10 ||
      meta.GetString("resample_table") != "auto-1MiB-linear" || rate < 8000 ||
      meta.GetString("resample_boundary") != "zero-extend-to-source-ceil" ||
      rate > limits.max_sample_rate || channels <= 0 ||
      channels > limits.max_channels || frames <= 0 ||
      uint64_t(frames) > limits.max_decoded_values / channels || !modules ||
      modules->array.size() < 3 || modules->array.size() > 32 ||
      !std::all_of(pcm.samples.begin(), pcm.samples.end(),
                   [](float v) { return std::isfinite(v); }))
    return Invalid();
  if (pcm.samples.size() !=
      (uint64_t(frames) * audio_detail::kOutputRate + rate - 1) / rate)
    return Invalid();
  return {};
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetAudioWorkerEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif
ObjectReply DecodeAudioContainer(std::string_view source,
                                 const FileAudioLimits& limits,
                                 const BinaryAccess& access, FileAudioPcm* pcm,
                                 GrammarControl control) {
  if (!pcm) return Invalid();
  *pcm = {};
  if (!ValidFileAudioLimits(limits))
    return Error(500, "file_audio_budget_invalid", "Invalid audio budget");
  if (source.size() > limits.max_source_bytes) return Capacity();
  if (!IsAudioContainer(source)) return Invalid();
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated audio scope required");
  const auto cancelled = [&] {
    return (access.cancelled && access.cancelled()) ||
           (control.cancelled && control.cancelled());
  };
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  const auto deadline =
      std::min({access.deadline, control.compile_deadline,
                Clock::now() + std::chrono::milliseconds(limits.timeout_ms)});
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline = deadline;
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  const char* entry = "--file-audio-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
    return Failed(failure);
  audio_detail::Request request{audio_detail::kMagic, source.size(), limits};
  audio_detail::Ready ready{};
  const auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, deadline, cancelled);
  };
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(source.data()), source.size(), true) ||
      !transfer(&ready, sizeof(ready), false))
    return Failed(process->Failure());
  if (ready == audio_detail::Ready::kDependency) return Dependency();
  if (ready == audio_detail::Ready::kSandbox)
    return Error(503, "file_audio_isolation_unavailable",
                 "Audio isolation unavailable");
  if (ready != audio_detail::Ready::kReady)
    return Failed(GrammarFailure::kWorker);
  if (!process->ReceiveIoBudget(
          {.write_bytes = 0,
           .file_creations = 0,
           .thread_creations = limits.max_thread_creations,
           .notifications = 128},
          deadline, cancelled))
    return Failed(process->Failure());
  if (control.worker_ready) control.worker_ready();
  audio_detail::Result result;
  if (!transfer(&result, sizeof(result), false))
    return Failed(process->Failure());
  if (result.magic != audio_detail::kMagic)
    return Failed(GrammarFailure::kWorker);
  if (result.failure == audio_detail::Failure::kDependency) return Dependency();
  if (result.failure == audio_detail::Failure::kCapacity) return Capacity();
  if (result.failure == audio_detail::Failure::kInvalid) return Invalid();
  if (result.failure != audio_detail::Failure::kNone || !result.samples ||
      result.samples > limits.max_output_samples ||
      result.samples > limits.max_duration_ns / 62500ULL ||
      !result.metadata_bytes || result.metadata_bytes > 16384)
    return Failed(GrammarFailure::kWorker);
  std::string metadata(result.metadata_bytes, '\0');
  FileAudioPcm next;
  next.samples.resize(result.samples);
  next.duration_ns = result.samples * 62500ULL;
  if (!transfer(metadata.data(), metadata.size(), false) ||
      !transfer(next.samples.data(), next.samples.size() * sizeof(float),
                false))
    return Failed(process->Failure());
  if (!io::ParseJson(metadata, &next.conversion, true,
                     {.max_depth = 8,
                      .max_values = 512,
                      .max_string_bytes = 16384,
                      .reject_duplicate_keys = true}))
    return Failed(GrammarFailure::kWorker);
  std::string trailing;
  if (!process->CollectOutput(&trailing, 0, deadline, cancelled))
    return Failed(process->Failure());
  if (!ValidateAudioPcm(source, limits, next).ok())
    return Failed(GrammarFailure::kWorker);
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  *pcm = std::move(next);
  return {};
}
}  // namespace q4t::server
