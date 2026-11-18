#include "q4t/server/speech_encode.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "speech_encode_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace speech_encode_detail;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
ObjectReply Invalid() {
  return Error(400, "invalid_speech_pcm", "Invalid speech PCM or format");
}
ObjectReply Capacity() {
  return Error(413, "speech_encoding_capacity",
               "Speech encoding budget exceeded");
}
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Speech encoding cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded", "Speech encoding timed out");
  if (failure == GrammarFailure::kResource) return Capacity();
  if (failure == GrammarFailure::kDependency)
    return Error(503, "speech_codec_unavailable", "Speech codec unavailable");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "speech_codec_capacity", "Speech codec slot occupied");
  return Error(503, "speech_codec_failed", "Speech codec worker failed");
}
}  // namespace
bool ValidSpeechEncodeLimits(const SpeechEncodeLimits& limits) {
  return limits.max_samples && limits.max_samples <= 4096ULL * 1920 * 4 &&
         limits.max_output_bytes && limits.max_output_bytes <= (128ULL << 20) &&
         limits.max_thread_creations <= 64 && limits.timeout_ms > 0 &&
         limits.timeout_ms <= 30000;
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetSpeechEncodeEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif
ObjectReply EncodeSpeech(std::span<const float> pcm, SpeechFormat format,
                         const SpeechEncodeLimits& limits,
                         const BinaryAccess& access, EncodedSpeech* out,
                         GrammarControl control) {
  if (!out || pcm.empty() || SpeechFormatName(format).empty()) return Invalid();
  if (!ValidSpeechEncodeLimits(limits))
    return Error(500, "speech_encoding_budget_invalid",
                 "Invalid encoding budget");
  if (pcm.size() > limits.max_samples) return Capacity();
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated speech scope required");
  const auto cancelled = [&] {
    return (access.cancelled && access.cancelled()) ||
           (control.cancelled && control.cancelled());
  };
  const auto deadline =
      std::min({access.deadline, control.compile_deadline,
                Clock::now() + std::chrono::milliseconds(limits.timeout_ms)});
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  const bool native =
      format == SpeechFormat::kPcm || format == SpeechFormat::kWav;
  std::string source;
  if (native && pcm.size() * 2 + (format == SpeechFormat::kWav ? 44 : 0) >
                    limits.max_output_bytes)
    return Capacity();
  // Fixed sample bound makes this multiplication safe; compressed output
  // budget is enforced separately, never applied to the PCM staging bytes.
  if (!audio::EncodeSpeechPcm(pcm, format == SpeechFormat::kWav,
                              native ? limits.max_output_bytes : pcm.size() * 2,
                              &source))
    return Invalid();
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  EncodedSpeech next;
  if (native) {
    next.bytes = std::move(source);
    next.encoding =
        Object({{"format", String("q4t.speech_encode.v1")},
                {"codec", String(std::string(SpeechFormatName(format)))},
                {"samples", Number(pcm.size())},
                {"sample_rate", Number(24000)},
                {"channels", Number(1)},
                {"output_sha256", String(Digest(next.bytes))},
                {"output_bytes", Number(next.bytes.size())},
                {"modules", Array()}});
  } else {
    control.domain = IsolatedWorkerDomain::kFileParse;
    control.compile_deadline = deadline;
    std::unique_ptr<detail::GrammarProcess> process;
    GrammarFailure failure;
    const char* entry = "--speech-encode-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    if (!test_entry.empty()) entry = test_entry.c_str();
#endif
    if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
      return Failed(failure);
    Request request{kMagic, pcm.size(), limits, format};
    Ready ready{};
    const auto transfer = [&](void* data, size_t bytes, bool write) {
      return process->Transfer(data, bytes, write, deadline, cancelled);
    };
    if (!transfer(&request, sizeof(request), true) ||
        !transfer(source.data(), source.size(), true) ||
        !transfer(&ready, sizeof(ready), false))
      return Failed(process->Failure());
    if (ready == Ready::kDependency) return Failed(GrammarFailure::kDependency);
    if (ready == Ready::kSandbox)
      return Error(503, "speech_codec_isolation_unavailable",
                   "Speech codec isolation unavailable");
    if (ready != Ready::kReady) return Failed(GrammarFailure::kWorker);
    if (!process->ReceiveIoBudget(
            {.write_bytes = 0,
             .file_creations = 0,
             .thread_creations = limits.max_thread_creations,
             .notifications = 128},
            deadline, cancelled))
      return Failed(process->Failure());
    if (control.worker_ready) control.worker_ready();
    Result result;
    if (!transfer(&result, sizeof(result), false))
      return Failed(process->Failure());
    if (result.magic != kMagic) return Failed(GrammarFailure::kWorker);
    if (result.failure == Failure::kDependency)
      return Failed(GrammarFailure::kDependency);
    if (result.failure == Failure::kCapacity) return Capacity();
    if (result.failure != Failure::kNone || !result.bytes ||
        result.bytes > limits.max_output_bytes || !result.metadata_bytes ||
        result.metadata_bytes > 16384)
      return Failed(GrammarFailure::kWorker);
    std::string metadata(result.metadata_bytes, '\0');
    next.bytes.resize(result.bytes);
    if (!transfer(metadata.data(), metadata.size(), false) ||
        !transfer(next.bytes.data(), next.bytes.size(), false))
      return Failed(process->Failure());
    if (!io::ParseJson(metadata, &next.encoding, true,
                       {.max_depth = 8,
                        .max_values = 512,
                        .max_string_bytes = 16384,
                        .reject_duplicate_keys = true}))
      return Failed(GrammarFailure::kWorker);
    const auto& meta = next.encoding;
    const auto* modules = meta.GetArray("modules");
    if (meta.GetString("format") != "q4t.speech_encode.v1" ||
        meta.GetString("codec") != SpeechFormatName(format) ||
        meta.GetInt("samples", -1) != int64_t(pcm.size()) ||
        meta.GetInt("sample_rate", -1) != 24000 ||
        meta.GetInt("channels", -1) != 1 ||
        meta.GetString("source_pcm16_sha256") != Digest(source) ||
        meta.GetString("output_sha256") != Digest(next.bytes) ||
        meta.GetInt("output_bytes", -1) != int64_t(next.bytes.size()) ||
        !modules || modules->array.size() < 3 || modules->array.size() > 32)
      return Failed(GrammarFailure::kWorker);
    if (format == SpeechFormat::kAac &&
        (meta.GetInt("encoder_tail_samples", -1) !=
             int64_t(EncoderTailSamples(format, pcm.size())) ||
         meta.GetInt("encoder_input_samples", -1) !=
             int64_t(pcm.size() + EncoderTailSamples(format, pcm.size()))))
      return Failed(GrammarFailure::kWorker);
    std::string trailing;
    if (!process->CollectOutput(&trailing, 0, deadline, cancelled))
      return Failed(process->Failure());
  }
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  *out = std::move(next);
  return {};
}
}  // namespace q4t::server
