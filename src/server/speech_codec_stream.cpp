#include "q4t/server/speech_codec_stream.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/trace/sha256.h"
#include "speech_codec_stream_protocol.h"

namespace q4t::server {
namespace {
using namespace speech_stream_detail;
using object_detail::Error;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
ObjectReply Invalid() {
  return Error(400, "invalid_speech_stream",
               "Invalid speech encoding stream operation");
}
ObjectReply Capacity() {
  return Error(413, "speech_encoding_capacity",
               "Speech encoding budget exceeded");
}
ObjectReply Failed(GrammarFailure failure) {
  switch (failure) {
    case GrammarFailure::kCancelled:
      return Error(499, "request_cancelled", "Speech encoding cancelled");
    case GrammarFailure::kTimeout:
      return Error(408, "request_deadline_exceeded",
                   "Speech encoding timed out");
    case GrammarFailure::kResource:
      return Capacity();
    case GrammarFailure::kDependency:
      return Error(503, "speech_codec_unavailable", "Speech codec unavailable");
    case GrammarFailure::kUnavailable:
      return Error(503, "speech_codec_capacity", "Speech codec slot occupied");
    default:
      return Error(503, "speech_codec_failed", "Speech codec worker failed");
  }
}
std::span<const uint8_t> Bytes(std::string_view text) {
  return {reinterpret_cast<const uint8_t*>(text.data()), text.size()};
}
}  // namespace

struct SpeechCodecStream::Impl {
  SpeechFormat format;
  SpeechEncodeLimits limits;
  BinaryAccess access;
  std::unique_ptr<detail::GrammarProcess> process;
  trace::Sha256Stream input_hash, output_hash;
  std::string output;
  io::Json metadata;
  bool need_input = false, final = false, done = false, failed = false;
  Impl(SpeechFormat f, const SpeechEncodeLimits& l, const BinaryAccess& a)
      : format(f),
        limits(l),
        access(a),
        input_hash(l.max_samples * 2),
        output_hash(l.max_output_bytes) {}
  Clock::time_point Deadline() const {
    return std::min(access.deadline, Clock::now() + std::chrono::milliseconds(
                                                        limits.timeout_ms));
  }
  ObjectReply Fail(GrammarFailure why) {
    failed = true;
    need_input = false;
    output.clear();
    process.reset();
    return Failed(why);
  }
  ObjectReply Active() {
    if (failed || done) return Invalid();
    if (access.cancelled && access.cancelled())
      return Fail(GrammarFailure::kCancelled);
    if (Clock::now() >= access.deadline) return Fail(GrammarFailure::kTimeout);
    return {};
  }
};
SpeechCodecStream::SpeechCodecStream() = default;
SpeechCodecStream::~SpeechCodecStream() = default;

ObjectReply SpeechCodecStream::Start(SpeechFormat format,
                                     const SpeechEncodeLimits& limits,
                                     const BinaryAccess& access,
                                     GrammarControl control) {
  if (impl_ || !ValidSpeechEncodeLimits(limits) || access.scope.empty() ||
      SpeechFormatName(format).empty() || format == SpeechFormat::kPcm ||
      format == SpeechFormat::kWav)
    return Invalid();
  auto next = std::make_unique<Impl>(format, limits, access);
  auto state = next->Active();
  if (!state.ok()) return state;
  if (control.cancelled && control.cancelled())
    return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= control.compile_deadline)
    return Failed(GrammarFailure::kTimeout);
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline =
      std::min(control.compile_deadline, next->Deadline());
  const auto caller_cancelled = control.cancelled;
  control.cancelled = [caller_cancelled, access] {
    return (caller_cancelled && caller_cancelled()) ||
           (access.cancelled && access.cancelled());
  };
  const auto* entry = "--speech-codec-stream-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  GrammarFailure failure;
  if (!detail::GrammarProcess::Start(entry, control, &next->process, &failure))
    return Failed(failure);
  Request request{kMagic, limits, format};
  Ready ready{};
  const auto deadline = control.compile_deadline;
  const auto transfer = [&](void* bytes, size_t count, bool write) {
    return next->process->Transfer(bytes, count, write, deadline,
                                   access.cancelled);
  };
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(&ready, sizeof(ready), false))
    return Failed(next->process->Failure());
  if (ready == Ready::kDependency) return Failed(GrammarFailure::kDependency);
  if (ready == Ready::kSandbox)
    return Error(503, "speech_codec_isolation_unavailable",
                 "Speech codec isolation unavailable");
  if (ready != Ready::kReady) return Failed(GrammarFailure::kWorker);
  if (!next->process->ReceiveIoBudget(
          {.write_bytes = 0,
           .file_creations = 0,
           .thread_creations = limits.max_thread_creations,
           .notifications = 128},
          deadline, access.cancelled))
    return Failed(next->process->Failure());
  if (control.worker_ready) control.worker_ready();
  impl_ = std::move(next);
  return {};
}

ObjectReply SpeechCodecStream::Append(std::span<const float> pcm, bool final) {
  if (!impl_) return Invalid();
  auto& s = *impl_;
  if (auto active = s.Active(); !active.ok()) return active;
  if (!s.need_input || s.final || pcm.size() > kInputSamples ||
      (pcm.empty() && !final))
    return Invalid();
  if (pcm.size() > s.limits.max_samples - s.input_hash.bytes() / 2)
    return Capacity();
  std::string bytes;
  if (!pcm.empty() &&
      !audio::EncodeSpeechPcm(pcm, false, pcm.size() * 2, &bytes))
    return Invalid();
  Input input{kMagic, pcm.size(), final ? 1UL : 0UL};
  const auto deadline = s.Deadline();
  if (!s.process->Transfer(&input, sizeof(input), true, deadline,
                           s.access.cancelled) ||
      !s.process->Transfer(bytes.data(), bytes.size(), true, deadline,
                           s.access.cancelled))
    return s.Fail(s.process->Failure());
  if (!s.input_hash.Add(Bytes(bytes))) return s.Fail(GrammarFailure::kResource);
  s.need_input = false;
  s.final = final;
  s.output.clear();
  return {};
}

ObjectReply SpeechCodecStream::Advance(SpeechCodecStep* step,
                                       std::string_view* output) {
  if (!impl_ || !step || !output) return Invalid();
  auto& s = *impl_;
  if (auto active = s.Active(); !active.ok()) return active;
  if (s.need_input) return Invalid();
  s.output.clear();
  *output = {};
  Event event;
  const auto deadline = s.Deadline();
  const auto transfer = [&](void* data, size_t count) {
    return s.process->Transfer(data, count, false, deadline,
                               s.access.cancelled);
  };
  if (!transfer(&event, sizeof(event))) return s.Fail(s.process->Failure());
  if (event.magic != kMagic) return s.Fail(GrammarFailure::kWorker);
  if (event.kind == EventKind::kFailure) {
    if (event.bytes) return s.Fail(GrammarFailure::kWorker);
    return s.Fail(
        event.failure == Failure::kCapacity     ? GrammarFailure::kResource
        : event.failure == Failure::kDependency ? GrammarFailure::kDependency
                                                : GrammarFailure::kWorker);
  }
  if (event.failure != Failure::kNone) return s.Fail(GrammarFailure::kWorker);
  if (event.kind == EventKind::kNeedInput) {
    if (event.bytes || s.final) return s.Fail(GrammarFailure::kWorker);
    s.need_input = true;
    *step = SpeechCodecStep::kNeedInput;
    return {};
  }
  if (event.kind == EventKind::kOutput) {
    if (!event.bytes || event.bytes > kOutputBytes ||
        event.bytes > s.limits.max_output_bytes - s.output_hash.bytes())
      return s.Fail(GrammarFailure::kWorker);
    s.output.resize(event.bytes);
    if (!transfer(s.output.data(), s.output.size()))
      return s.Fail(s.process->Failure());
    if (!s.output_hash.Add(Bytes(s.output)))
      return s.Fail(GrammarFailure::kResource);
    *output = s.output;
    *step = SpeechCodecStep::kOutput;
    return {};
  }
  if (event.kind != EventKind::kDone || !s.final || !s.input_hash.bytes() ||
      !s.output_hash.bytes() || !event.bytes || event.bytes > 16384)
    return s.Fail(GrammarFailure::kWorker);
  std::string text(event.bytes, '\0');
  if (!transfer(text.data(), text.size())) return s.Fail(s.process->Failure());
  io::Json meta;
  if (!io::ParseJson(text, &meta, true,
                     {.max_depth = 8,
                      .max_values = 512,
                      .max_string_bytes = 16384,
                      .reject_duplicate_keys = true}))
    return s.Fail(GrammarFailure::kWorker);
  const auto* modules = meta.GetArray("modules");
  const auto samples = s.input_hash.bytes() / 2;
  if (meta.GetString("format") != "q4t.speech_stream.v1" ||
      meta.GetString("codec") != SpeechFormatName(s.format) ||
      meta.GetInt("samples", -1) != static_cast<int64_t>(samples) ||
      meta.GetInt("sample_rate") != 24000 || meta.GetInt("channels") != 1 ||
      meta.GetString("source_pcm16_sha256") !=
          trace::HexDigest(s.input_hash.Snapshot()) ||
      meta.GetString("output_sha256") !=
          trace::HexDigest(s.output_hash.Snapshot()) ||
      meta.GetInt("output_bytes", -1) !=
          static_cast<int64_t>(s.output_hash.bytes()) ||
      !modules || modules->array.size() < 3 || modules->array.size() > 32)
    return s.Fail(GrammarFailure::kWorker);
  if (s.format == SpeechFormat::kAac &&
      (meta.GetInt("encoder_tail_samples", -1) !=
           static_cast<int64_t>(
               speech_encode_detail::EncoderTailSamples(s.format, samples)) ||
       meta.GetInt("encoder_input_samples", -1) !=
           static_cast<int64_t>(
               samples +
               speech_encode_detail::EncoderTailSamples(s.format, samples))))
    return s.Fail(GrammarFailure::kWorker);
  std::string trailing;
  if (!s.process->CollectOutput(&trailing, 0, deadline, s.access.cancelled)) {
    const auto failure = s.process->Failure();
    // The complete payload was already accounted for. Bytes after Done are
    // a corrupt worker protocol, not an oversized client audio request.
    return s.Fail(failure == GrammarFailure::kResource ? GrammarFailure::kWorker
                                                      : failure);
  }
  s.process.reset();
  s.metadata = std::move(meta);
  s.done = true;
  *step = SpeechCodecStep::kDone;
  return {};
}
const io::Json& SpeechCodecStream::Metadata() const {
  static const io::Json empty;
  return impl_ && impl_->done ? impl_->metadata : empty;
}
void SpeechCodecStream::Stop() {
  if (impl_) (void)impl_->Fail(GrammarFailure::kCancelled);
}
namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetSpeechCodecStreamEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif
}  // namespace detail
}  // namespace q4t::server
