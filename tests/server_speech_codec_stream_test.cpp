#include "q4t/server/speech_codec_stream.h"

#include <unistd.h>

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numbers>
#include <thread>

#include "../src/server/file_video_protocol.h"
#include "../src/server/object_protocol.h"
#include "../src/server/speech_codec_stream_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
BinaryAccess Access() {
  return {"client:speech-stream", Clock::now() + 10s, {}};
}
GrammarControl Control() {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  return control;
}
bool Clean(const GrammarControl& control, uint64_t expected = 1) {
  detail::PollIsolatedWorkers(IsolatedWorkerDomain::kFileParse);
  return control.counters->started == expected &&
         control.counters->reaped == expected && control.counters->active == 0;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
std::vector<float> Signal(size_t samples = 72000) {
  std::vector<float> pcm(samples);
  for (size_t i = 0; i < pcm.size(); ++i) {
    const double t = 2 * std::numbers::pi * i / 24000;
    pcm[i] = .5 * std::sin(440 * t) + .1 * std::sin(997 * t);
  }
  return pcm;
}
struct Result {
  std::string bytes, before_final;
  q4t::io::Json metadata;
  size_t operations = 0, first_output_samples = 0;
};
ObjectReply Run(SpeechFormat format, std::span<const float> source,
                const SpeechEncodeLimits& limits, const GrammarControl& control,
                Result* result, size_t packet = 997, bool empty_final = true) {
  SpeechCodecStream stream;
  auto reply = stream.Start(format, limits, Access(), control);
  if (!reply.ok()) return reply;
  size_t sent = 0;
  bool final = false;
  for (size_t i = 0; i < 10000; ++i) {
    SpeechCodecStep step;
    std::string_view bytes;
    reply = stream.Advance(&step, &bytes);
    ++result->operations;
    if (!reply.ok()) return reply;
    if (step == SpeechCodecStep::kNeedInput) {
      const auto count = std::min(packet, source.size() - sent);
      final = empty_final ? !count : sent + count == source.size();
      reply = stream.Append(source.subspan(sent, count), final);
      if (!reply.ok()) return reply;
      sent += count;
    } else if (step == SpeechCodecStep::kOutput) {
      if (result->bytes.empty()) result->first_output_samples = sent;
      result->bytes += bytes;
      if (!final) result->before_final += bytes;
    } else {
      result->metadata = stream.Metadata();
      return {};
    }
  }
  return Error(500, "test_step_budget", "stream step budget exhausted");
}
void Capture(const Result& result, SpeechFormat format) {
  const char* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!root || !*root) return;
  auto path = std::filesystem::path(root) /
              ("stream." + std::string(SpeechFormatName(format)));
  std::ofstream(path, std::ios::binary)
      .write(result.bytes.data(), result.bytes.size());
  std::ofstream(path.string() + ".prefix", std::ios::binary)
      .write(result.before_final.data(), result.before_final.size());
  std::ofstream(path.string() + ".json") << OutputJson(result.metadata);
}
bool Tone(const std::vector<float>& pcm) {
  Q4T_CHECK(pcm.size() > 8000);
  const size_t end = std::min<size_t>(pcm.size() - 1000, 40000);
  for (double frequency : {440., 997.}) {
    double real = 0, imaginary = 0;
    for (size_t i = 3000; i < end; ++i) {
      const double phase = 2 * std::numbers::pi * frequency * i / 16000;
      real += pcm[i] * std::cos(phase);
      imaginary += pcm[i] * std::sin(phase);
    }
    const double amplitude = 2 * std::hypot(real, imaginary) / (end - 3000);
    Q4T_CHECK(std::abs(amplitude - (frequency == 440 ? .5 : .1)) < .05);
  }
  return true;
}
bool Roundtrip(SpeechFormat format, bool lossless = false) {
  const auto source = Signal();
  auto control = Control();
  Result result;
  SpeechEncodeLimits limits;
  limits.max_samples = source.size();
  Q4T_CHECK(Ok(Run(format, source, limits, control, &result)));
  Q4T_CHECK(Clean(control));
  Q4T_CHECK(result.metadata.GetString("format") == "q4t.speech_stream.v1" &&
            result.metadata.GetString("codec") == SpeechFormatName(format) &&
            result.metadata.GetInt("samples", -1) == 72000 &&
            result.metadata.GetString("output_sha256") == Digest(result.bytes));
  Q4T_CHECK(result.first_output_samples < source.size() &&
            result.before_final.size() > 4096 &&
            result.before_final.size() < result.bytes.size());
  Capture(result, format);
  FileAudioPcm decoded, partial;
  control = Control();
  Q4T_CHECK(
      Ok(DecodeAudioContainer(result.bytes, {}, Access(), &decoded, control)));
  Q4T_CHECK(Clean(control) && decoded.samples.size() >= 48000 &&
            decoded.samples.size() <= 50400 && Tone(decoded.samples));
  control = Control();
  // A real pre-EOF payload must already decode audible samples, not only a
  // format header. The explicit truncated container is test-only.
  Q4T_CHECK(Ok(DecodeAudioContainer(result.before_final, {}, Access(), &partial,
                                    control)));
  Q4T_CHECK(Clean(control) && Tone(partial.samples));
  if (lossless) {
    std::string wave;
    Q4T_CHECK(q4t::audio::EncodeSpeechPcm(source, true, 144044, &wave));
    FileAudioPcm baseline;
    control = Control();
    Q4T_CHECK(Ok(DecodeAudioContainer(wave, {}, Access(), &baseline, control)));
    Q4T_CHECK(Clean(control) && decoded.samples == baseline.samples);
    Result other;
    control = Control();
    Q4T_CHECK(Ok(Run(format, source, limits, control, &other, 2048, false)));
    Q4T_CHECK(Clean(control) && result.bytes == other.bytes);
  }
  std::fprintf(stderr,
               "%s: first samples=%zu, prefix=%zu final=%zu steps=%zu\n",
               std::string(SpeechFormatName(format)).c_str(),
               result.first_output_samples, result.before_final.size(),
               result.bytes.size(), result.operations);
  return true;
}
bool NeedInput(SpeechCodecStream& stream) {
  SpeechCodecStep step;
  std::string_view output;
  Q4T_CHECK(Ok(stream.Advance(&step, &output)) &&
            step == SpeechCodecStep::kNeedInput && output.empty());
  return true;
}
}  // namespace

int SpeechCodecStreamFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const std::string_view entry = argv[1];
  constexpr std::string_view prefix = "--speech-stream-fixture-";
  if (!entry.starts_with(prefix)) return -1;
  const auto kind = entry.substr(prefix.size());
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  using namespace speech_stream_detail;
  Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false)) return 3;
  Ready ready = Ready::kReady;
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  int listener = video_detail::EnterVideoSandbox();
  if (listener < 0 || !detail::SendWorkerIoListener(listener)) return 3;
  close(listener);
  if (kind == "crash") _exit(17);
  if (kind == "hang")
    for (;;) std::this_thread::sleep_for(1ms);
  const auto send = [](EventKind type, uint64_t bytes = 0) {
    Event event{kMagic, type, bytes, Failure::kNone};
    return detail::WorkerTransfer(&event, sizeof(event), true);
  };
  if (kind == "early-done") return send(EventKind::kDone, 1) ? 0 : 3;
  if (kind == "oversize")
    return send(EventKind::kOutput, SpeechCodecStream::kOutputBytes + 1) ? 0
                                                                         : 3;
  if (!send(EventKind::kNeedInput)) return 3;
  Input input;
  if (!detail::WorkerTransfer(&input, sizeof(input), false) ||
      input.samples > 2048 || !input.final)
    return 3;
  std::string pcm(input.samples * 2, '\0');
  if (!detail::WorkerTransfer(pcm.data(), pcm.size(), false)) return 3;
  if (kind == "late-input") return send(EventKind::kNeedInput) ? 0 : 3;
  std::string payload = "encoded";
  if (!send(EventKind::kOutput, payload.size()) ||
      !detail::WorkerTransfer(payload.data(), payload.size(), true))
    return 3;
  auto meta = Object(
      {{"format", String("q4t.speech_stream.v1")},
       {"codec", String("mp3")},
       {"samples", Number(input.samples)},
       {"sample_rate", Number(24000)},
       {"channels", Number(1)},
       {"source_pcm16_sha256", String(Digest(pcm))},
       {"output_sha256", String(kind == "bad-hash" ? "bad" : Digest(payload))},
       {"output_bytes", Number(payload.size())},
       {"modules", Array({Object(), Object(), Object()})}});
  auto text = kind == "bad-json" ? "{" : OutputJson(meta);
  if (!send(EventKind::kDone, text.size()) ||
      !detail::WorkerTransfer(text.data(), text.size(), true))
    return 3;
  if (kind == "trailing" && !detail::WorkerTransfer(payload.data(), 1, true))
    return 3;
  return kind == "nonzero" ? 17 : 0;
}

Q4T_TEST(speech_stream_mp3_progressive_roundtrip) {
  return Roundtrip(SpeechFormat::kMp3);
}
Q4T_TEST(speech_stream_aac_progressive_roundtrip) {
  return Roundtrip(SpeechFormat::kAac);
}
Q4T_TEST(speech_stream_opus_progressive_roundtrip) {
  return Roundtrip(SpeechFormat::kOpus);
}
Q4T_TEST(speech_stream_flac_lossless_and_packet_independent) {
  return Roundtrip(SpeechFormat::kFlac, true);
}
Q4T_TEST(speech_stream_aac_partial_tail_keeps_actual_signal) {
  std::vector<float> source(2507);
  for (size_t i = 0; i < source.size(); ++i) {
    const double frequency = i < 1700 ? 440 : 997;
    source[i] = .5 * std::sin(2 * std::numbers::pi * frequency * i / 24000);
  }
  Result streamed;
  auto control = Control();
  Q4T_CHECK(Ok(Run(SpeechFormat::kAac, source, {}, control, &streamed, 17)));
  Q4T_CHECK(Clean(control));
  EncodedSpeech full;
  control = Control();
  Q4T_CHECK(Ok(
      EncodeSpeech(source, SpeechFormat::kAac, {}, Access(), &full, control)));
  Q4T_CHECK(Clean(control) && full.bytes == streamed.bytes &&
            streamed.metadata.GetInt("samples") == 2507 &&
            streamed.metadata.GetInt("encoder_tail_samples") == 2613 &&
            streamed.metadata.GetInt("encoder_input_samples") == 5120);
  return true;  // The exact complete AAC bytes already have tail-signal
                // evidence.
}
Q4T_TEST(speech_stream_invalid_input_and_capacity_preserve_state) {
  SpeechCodecStream stream;
  auto control = Control();
  auto access = Access();
  Q4T_CHECK(!stream.Start(SpeechFormat::kWav, {}, access, control).ok());
  Q4T_CHECK(!stream.Start(SpeechFormat::kMp3, {}, {}, control).ok());
  Q4T_CHECK(Clean(control, 0));
  SpeechEncodeLimits limits;
  limits.max_samples = 2;
  Q4T_CHECK(Ok(stream.Start(SpeechFormat::kFlac, limits, access, control)));
  Q4T_CHECK(NeedInput(stream));
  const std::array<float, 3> over{0, 0, 0};
  Q4T_CHECK(stream.Append(over, true).http_status == 413);
  const float nan = std::numeric_limits<float>::quiet_NaN();
  Q4T_CHECK(stream.Append({&nan, 1}, false).http_status == 400);
  const float invalid = 2;
  Q4T_CHECK(stream.Append({&invalid, 1}, false).http_status == 400);
  Q4T_CHECK(stream.Append({}, false).http_status == 400);
  SpeechCodecStep step;
  std::string_view bytes;
  Q4T_CHECK(stream.Advance(&step, &bytes).http_status == 400);
  Q4T_CHECK(Ok(stream.Append(std::span(over).first(2), true)));
  Q4T_CHECK(stream.Append({}, true).http_status == 400);
  bool done = false;
  for (size_t i = 0; i < 100 && !done; ++i) {
    Q4T_CHECK(Ok(stream.Advance(&step, &bytes)));
    done = step == SpeechCodecStep::kDone;
  }
  Q4T_CHECK(done && Clean(control) && stream.Metadata().GetInt("samples") == 2);
  Q4T_CHECK(stream.Advance(&step, &bytes).http_status == 400);
  return true;
}
Q4T_TEST(speech_stream_output_budget_thread_failure_and_empty_eof_are_bounded) {
  const auto source = Signal(4096);
  for (int test = 0; test < 3; ++test) {
    auto control = Control();
    SpeechEncodeLimits limits;
    limits.timeout_ms = 250;
    if (test == 0) limits.max_output_bytes = 10;
    if (test == 1) limits.max_thread_creations = 0;
    Result result;
    const auto reply =
        Run(SpeechFormat::kFlac, test == 2 ? std::span<const float>{} : source,
            limits, control, &result);
    Q4T_CHECK(!reply.ok() && Clean(control));
    if (test == 0) Q4T_CHECK(reply.http_status == 413);
    Q4T_CHECK(result.metadata.GetString("format").empty());
  }
  return true;
}
Q4T_TEST(speech_stream_faults_reap_and_next_request_succeeds) {
  ScopeExit reset([] { detail::SetSpeechCodecStreamEntryForTesting({}); });
  for (const char* entry : {"--speech-codec-stream-missing-worker",
                            "--speech-stream-fixture-crash"}) {
    detail::SetSpeechCodecStreamEntryForTesting(entry);
    auto control = Control();
    Result result;
    Q4T_CHECK(Run(SpeechFormat::kMp3, Signal(4096), {}, control, &result)
                  .http_status == 503);
    Q4T_CHECK(Clean(control));
  }
  detail::SetSpeechCodecStreamEntryForTesting({});
  auto control = Control();
  {
    SpeechCodecStream stream;
    Q4T_CHECK(Ok(stream.Start(SpeechFormat::kMp3, {}, Access(), control)));
    Q4T_CHECK(NeedInput(stream));
    stream.Stop();
    stream.Stop();
  }
  Q4T_CHECK(Clean(control));
  control = Control();
  Result result;
  Q4T_CHECK(Ok(Run(SpeechFormat::kMp3, Signal(4096), {}, control, &result)));
  Q4T_CHECK(Clean(control));
  return true;
}
Q4T_TEST(speech_stream_cancel_and_codec_deadline_reap_owned_process) {
  detail::SetSpeechCodecStreamEntryForTesting("--speech-stream-fixture-hang");
  ScopeExit reset([] { detail::SetSpeechCodecStreamEntryForTesting({}); });
  for (int test = 0; test < 3; ++test) {
    auto control = Control();
    auto access = Access();
    bool cancelled = false;
    if (test == 0) control.cancelled = [&] { return cancelled; };
    if (test == 1) access.cancelled = [&] { return cancelled; };
    SpeechEncodeLimits limits;
    limits.timeout_ms = 100;
    SpeechCodecStream stream;
    Q4T_CHECK(Ok(stream.Start(SpeechFormat::kMp3, limits, access, control)));
    cancelled = true;
    SpeechCodecStep step;
    std::string_view output;
    Q4T_CHECK(stream.Advance(&step, &output).http_status ==
              (test == 2 ? 408 : 499));
    Q4T_CHECK(Clean(control) && stream.Metadata().GetString("format").empty());
  }
  return true;
}
Q4T_TEST(speech_stream_overall_deadline_includes_producer_idle_time) {
  auto access = Access();
  access.deadline = Clock::now() + 200ms;
  auto control = Control();
  SpeechCodecStream stream;
  Q4T_CHECK(Ok(stream.Start(SpeechFormat::kMp3, {}, access, control)));
  Q4T_CHECK(NeedInput(stream));
  std::this_thread::sleep_until(access.deadline + 1ms);
  const float sample = 0;
  Q4T_CHECK(stream.Append({&sample, 1}, true).http_status == 408 &&
            Clean(control));
  return true;
}
Q4T_TEST(speech_stream_corrupt_protocol_never_publishes_completion) {
  ScopeExit reset([] { detail::SetSpeechCodecStreamEntryForTesting({}); });
  for (const char* kind : {"early-done", "oversize", "late-input", "bad-hash",
                           "bad-json", "trailing", "nonzero"}) {
    detail::SetSpeechCodecStreamEntryForTesting(
        std::string("--speech-stream-fixture-") + kind);
    auto control = Control();
    SpeechCodecStream stream;
    Q4T_CHECK(Ok(stream.Start(SpeechFormat::kMp3, {}, Access(), control)));
    ObjectReply reply;
    for (int i = 0; i < 5; ++i) {
      SpeechCodecStep step;
      std::string_view bytes;
      reply = stream.Advance(&step, &bytes);
      if (!reply.ok()) break;
      Q4T_CHECK(step != SpeechCodecStep::kDone);
      if (step == SpeechCodecStep::kNeedInput) {
        const float sample = .5;
        Q4T_CHECK(Ok(stream.Append({&sample, 1}, true)));
      }
    }
    std::fprintf(stderr, "protocol %s: HTTP %d, code=%s\n", kind,
                 reply.http_status, reply.code.c_str());
    Q4T_CHECK(reply.http_status == 503 && Clean(control) &&
              stream.Metadata().GetString("format").empty());
  }
  return true;
}
