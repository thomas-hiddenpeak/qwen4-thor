#include "q4t/server/speech_encode.h"

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
#include "../src/server/speech_encode_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
BinaryAccess Access() { return {"client:speech", Clock::now() + 5s, {}}; }
GrammarControl Control() {
  GrammarControl result;
  result.counters = std::make_shared<GrammarWorkerCounters>();
  return result;
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
std::vector<float> Signal() {
  std::vector<float> pcm(24000);
  for (size_t i = 0; i < pcm.size(); ++i) {
    const double t = 2 * std::numbers::pi * i / 24000;
    pcm[i] = .5 * std::sin(440 * t) + .1 * std::sin(997 * t);
  }
  return pcm;
}
void Capture(const EncodedSpeech& encoded, SpeechFormat format) {
  const char* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!root || !*root) return;
  auto path = std::filesystem::path(root) /
              ("encoded." + std::string(SpeechFormatName(format)));
  std::ofstream(path, std::ios::binary)
      .write(encoded.bytes.data(), encoded.bytes.size());
  std::ofstream(path.string() + ".json") << OutputJson(encoded.encoding);
}
bool Roundtrip(SpeechFormat format, bool lossless = false) {
  auto control = Control();
  const auto source = Signal();
  EncodedSpeech encoded;
  Q4T_CHECK(Ok(EncodeSpeech(source, format, {}, Access(), &encoded, control)));
  Q4T_CHECK(Clean(control));
  Q4T_CHECK(encoded.encoding.GetString("codec") == SpeechFormatName(format));
  Q4T_CHECK(encoded.encoding.GetInt("samples", 0) == 24000);
  Q4T_CHECK(encoded.encoding.GetString("output_sha256") ==
            Digest(encoded.bytes));
  Q4T_CHECK(IsAudioContainer(encoded.bytes));
  Capture(encoded, format);
  FileAudioPcm decoded;
  control = Control();
  Q4T_CHECK(
      Ok(DecodeAudioContainer(encoded.bytes, {}, Access(), &decoded, control)));
  Q4T_CHECK(Clean(control));
  // Lossy frames may contain codec priming/tail. Do not fabricate source
  // timestamps or strip data to make a lossy codec appear sample-exact.
  Q4T_CHECK(decoded.samples.size() >= 16000 && decoded.samples.size() <= 18400);
  for (double frequency : {440., 997.}) {
    double real = 0, imaginary = 0;
    for (size_t i = 3000; i < 14000; ++i) {
      const double phase = 2 * std::numbers::pi * frequency * i / 16000;
      real += decoded.samples[i] * std::cos(phase);
      imaginary += decoded.samples[i] * std::sin(phase);
    }
    const double amplitude = 2 * std::hypot(real, imaginary) / 11000;
    Q4T_CHECK(std::abs(amplitude - (frequency == 440 ? .5 : .1)) < .05);
  }
  if (lossless) {
    std::string wave;
    Q4T_CHECK(q4t::audio::EncodeSpeechPcm(source, true, 48044, &wave));
    FileAudioPcm baseline;
    control = Control();
    Q4T_CHECK(Ok(DecodeAudioContainer(wave, {}, Access(), &baseline, control)));
    Q4T_CHECK(Clean(control) && decoded.samples == baseline.samples);
  }
  return true;
}
}  // namespace

int SpeechEncodeFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const bool hang = !std::strcmp(argv[1], "--speech-encode-hang-fixture");
  const bool crash = !std::strcmp(argv[1], "--speech-encode-crash-fixture");
  if (!hang && !crash) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  speech_encode_detail::Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.samples > 24000)
    return 3;
  std::string source(request.samples * 2, '\0');
  if (!detail::WorkerTransfer(source.data(), source.size(), false)) return 3;
  auto ready = speech_encode_detail::Ready::kReady;
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  const int listener = video_detail::EnterVideoSandbox();
  if (listener < 0 || !detail::SendWorkerIoListener(listener)) return 3;
  close(listener);
  if (crash) _exit(17);
  for (;;) std::this_thread::sleep_for(1ms);
}

Q4T_TEST(speech_encode_native_pcm_wave_uses_no_codec_worker) {
  const auto source = Signal();
  auto control = Control();
  detail::SetSpeechEncodeEntryForTesting("--speech-encode-missing-worker");
  ScopeExit reset([] { detail::SetSpeechEncodeEntryForTesting({}); });
  for (auto format : {SpeechFormat::kPcm, SpeechFormat::kWav}) {
    EncodedSpeech encoded;
    Q4T_CHECK(
        Ok(EncodeSpeech(source, format, {}, Access(), &encoded, control)));
    std::string expected;
    Q4T_CHECK(q4t::audio::EncodeSpeechPcm(source, format == SpeechFormat::kWav,
                                          48044, &expected));
    Q4T_CHECK(encoded.bytes == expected && Clean(control, 0));
    Capture(encoded, format);
  }
  return true;
}
Q4T_TEST(speech_encode_mp3_real_roundtrip) {
  return Roundtrip(SpeechFormat::kMp3);
}
Q4T_TEST(speech_encode_aac_real_roundtrip) {
  return Roundtrip(SpeechFormat::kAac);
}
Q4T_TEST(speech_encode_aac_partial_tail_contains_actual_signal) {
  std::vector<float> source(2507);
  for (size_t i = 0; i < source.size(); ++i) {
    const double frequency = i < 1700 ? 440 : 997;
    source[i] = .5 * std::sin(2 * std::numbers::pi * frequency * i / 24000);
  }
  SpeechEncodeLimits limits;
  limits.max_samples = source.size();
  EncodedSpeech encoded;
  auto control = Control();
  Q4T_CHECK(Ok(EncodeSpeech(source, SpeechFormat::kAac, limits, Access(),
                            &encoded, control)));
  Q4T_CHECK(Clean(control));
  Q4T_CHECK(encoded.encoding.GetInt("samples", 0) == 2507 &&
            encoded.encoding.GetInt("encoder_input_samples", 0) == 5120 &&
            encoded.encoding.GetInt("encoder_tail_samples", 0) == 2613);
  FileAudioPcm decoded, baseline;
  control = Control();
  Q4T_CHECK(Ok(DecodeAudioContainer(encoded.bytes, {}, Access(), &decoded,
                                    control)) &&
            Clean(control));
  std::string wave;
  Q4T_CHECK(q4t::audio::EncodeSpeechPcm(source, true, 5060, &wave));
  control = Control();
  Q4T_CHECK(Ok(DecodeAudioContainer(wave, {}, Access(), &baseline, control)) &&
            Clean(control));
  Q4T_CHECK(baseline.samples.size() == 1672 &&
            decoded.samples.size() >= baseline.samples.size());
  const auto cosine = [&](size_t lag, size_t begin, size_t end) {
    double ab = 0, aa = 0, bb = 0;
    for (size_t i = begin; i < end; ++i) {
      const double a = baseline.samples[i], b = decoded.samples[i + lag];
      ab += a * b;
      aa += a * a;
      bb += b * b;
    }
    return aa > 0 && bb > 0 ? ab / std::sqrt(aa * bb) : -1.;
  };
  size_t best_lag = 0;
  double best = -1;
  for (size_t lag = 0;
       lag <= 2048 && lag + baseline.samples.size() <= decoded.samples.size();
       ++lag) {
    const double score = cosine(lag, 0, baseline.samples.size());
    if (score > best) {
      best = score;
      best_lag = lag;
    }
  }
  const double tail = cosine(best_lag, 1250, baseline.samples.size());
  std::fprintf(stderr, "AAC whole cosine=%.6f tail=%.6f lag=%zu samples=%zu\n",
               best, tail, best_lag, decoded.samples.size());
  Q4T_CHECK(best >= .9 && tail >= .8);
  return true;
}
Q4T_TEST(speech_encode_opus_real_roundtrip) {
  return Roundtrip(SpeechFormat::kOpus);
}
Q4T_TEST(speech_encode_flac_lossless_roundtrip) {
  return Roundtrip(SpeechFormat::kFlac, true);
}
Q4T_TEST(speech_encode_invalid_input_is_atomic_before_worker) {
  auto source = Signal();
  auto control = Control();
  EncodedSpeech out;
  out.bytes = "sentinel";
  source[0] = std::numeric_limits<float>::quiet_NaN();
  Q4T_CHECK(
      !EncodeSpeech(source, SpeechFormat::kMp3, {}, Access(), &out, control)
           .ok());
  source[0] = 2;
  Q4T_CHECK(
      !EncodeSpeech(source, SpeechFormat::kPcm, {}, Access(), &out, control)
           .ok());
  source[0] = 0;
  Q4T_CHECK(!EncodeSpeech(source, static_cast<SpeechFormat>(100), {}, Access(),
                          &out, control)
                 .ok());
  Q4T_CHECK(
      !EncodeSpeech(source, SpeechFormat::kPcm, {}, {}, &out, control).ok());
  Q4T_CHECK(out.bytes == "sentinel" && Clean(control, 0));
  return true;
}
Q4T_TEST(speech_encode_budgets_and_child_thread_failure_are_bounded) {
  const auto source = Signal();
  auto control = Control();
  SpeechEncodeLimits limits;
  limits.max_samples = 23999;
  EncodedSpeech out;
  out.bytes = "sentinel";
  Q4T_CHECK(
      EncodeSpeech(source, SpeechFormat::kMp3, limits, Access(), &out, control)
          .http_status == 413);
  Q4T_CHECK(Clean(control, 0));
  limits = {};
  limits.max_output_bytes = 10;
  Q4T_CHECK(
      EncodeSpeech(source, SpeechFormat::kFlac, limits, Access(), &out, control)
          .http_status == 413);
  Q4T_CHECK(Clean(control));
  control = Control();
  limits = {};
  limits.max_thread_creations = 0;
  limits.timeout_ms = 250;
  Q4T_CHECK(
      !EncodeSpeech(source, SpeechFormat::kMp3, limits, Access(), &out, control)
           .ok());
  Q4T_CHECK(Clean(control) && out.bytes == "sentinel");
  return true;
}
Q4T_TEST(speech_encode_dependency_and_crash_preserve_parent_and_native_output) {
  const auto source = Signal();
  const auto parent = getpid();
  ScopeExit reset([] { detail::SetSpeechEncodeEntryForTesting({}); });
  for (const char* entry :
       {"--speech-encode-missing-worker", "--speech-encode-crash-fixture"}) {
    auto control = Control();
    detail::SetSpeechEncodeEntryForTesting(entry);
    EncodedSpeech out;
    out.bytes = "sentinel";
    const auto error =
        EncodeSpeech(source, SpeechFormat::kMp3, {}, Access(), &out, control);
    Q4T_CHECK(error.http_status == 503 && Clean(control) &&
              out.bytes == "sentinel");
    Q4T_CHECK(Ok(
        EncodeSpeech(source, SpeechFormat::kWav, {}, Access(), &out, control)));
    Q4T_CHECK(getpid() == parent && Clean(control));
  }
  return true;
}
Q4T_TEST(speech_encode_cancel_and_deadline_reap_owned_worker) {
  const auto source = Signal();
  detail::SetSpeechEncodeEntryForTesting("--speech-encode-hang-fixture");
  ScopeExit reset([] { detail::SetSpeechEncodeEntryForTesting({}); });
  auto control = Control();
  bool cancelled = false;
  control.cancelled = [&] { return cancelled; };
  control.worker_ready = [&] { cancelled = true; };
  EncodedSpeech out;
  out.bytes = "sentinel";
  Q4T_CHECK(
      EncodeSpeech(source, SpeechFormat::kMp3, {}, Access(), &out, control)
          .http_status == 499);
  Q4T_CHECK(Clean(control));
  control = Control();
  SpeechEncodeLimits limits;
  limits.timeout_ms = 100;
  Q4T_CHECK(
      EncodeSpeech(source, SpeechFormat::kMp3, limits, Access(), &out, control)
          .http_status == 408);
  Q4T_CHECK(Clean(control) && out.bytes == "sentinel");
  return true;
}
