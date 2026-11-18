#include "q4t/server/file_audio.h"

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <numbers>
#include <stdexcept>

#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:audio-rate", Clock::now() + 5s, {}}; }
std::string Bytes(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/file_audio" / name,
                     std::ios::binary);
  if (!file) throw std::runtime_error("rate fixture missing");
  return {std::istreambuf_iterator<char>(file), {}};
}
FileAudioLimits Limits() {
  FileAudioLimits limits;
  limits.output_sample_rate = 24000;
  limits.max_output_samples = 1440000;
  limits.max_duration_ns = 60000000000ULL;
  return limits;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool Decode(std::string_view source, FileAudioPcm* pcm,
            FileAudioLimits limits = Limits()) {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  Q4T_CHECK(Ok(DecodeAudioContainer(source, limits, Access(), pcm, control)));
  detail::PollRequestWorkers();
  Q4T_CHECK(control.counters->started == 1 && control.counters->reaped == 1 &&
            control.counters->active == 0);
  Q4T_CHECK(ValidateAudioPcm(source, limits, *pcm).ok());
  return true;
}
std::string Wave(uint32_t rate, uint32_t frames) {
  auto bytes = Bytes("mono16.wav");
  bytes.resize(44 + size_t(frames) * 2);
  const auto put = [&](size_t at, uint32_t value) {
    std::memcpy(bytes.data() + at, &value, 4);
  };
  put(4, bytes.size() - 8);
  put(24, rate);
  put(28, rate * 2);
  put(40, frames * 2);
  for (uint32_t i = 0; i < frames; ++i) {
    const int16_t sample =
        std::lround(16384 * std::sin(2 * std::numbers::pi * 440 * i / rate));
    std::memcpy(bytes.data() + 44 + size_t(i) * 2, &sample, 2);
  }
  return bytes;
}
double Amplitude(const FileAudioPcm& pcm, double frequency) {
  double real = 0, imaginary = 0;
  const size_t first = pcm.sample_rate / 8, last = 7 * pcm.sample_rate / 8;
  if (pcm.samples.size() < last) return 0;
  for (size_t i = first; i < last; ++i) {
    const double phase = 2 * std::numbers::pi * frequency * i / pcm.sample_rate;
    real += pcm.samples[i] * std::cos(phase);
    imaginary += pcm.samples[i] * std::sin(phase);
  }
  return 2 * std::hypot(real, imaginary) / (last - first);
}
}  // namespace

Q4T_TEST(file_audio_24k_native_pcm_keeps_every_sample_and_fractional_duration) {
  const auto source = Wave(24000, 16001);
  FileAudioPcm pcm;
  Q4T_CHECK(Decode(source, &pcm));
  Q4T_CHECK(pcm.sample_rate == 24000 && pcm.samples.size() == 16001 &&
            pcm.duration_ns == 666708333);
  for (size_t i = 0; i < pcm.samples.size(); ++i) {
    int16_t value;
    std::memcpy(&value, source.data() + 44 + i * 2, 2);
    Q4T_CHECK(pcm.samples[i] == value / 32768.f);
  }
  Q4T_CHECK(pcm.conversion.GetInt("sample_rate") == 24000 &&
            pcm.conversion.GetInt("source_sample_rate") == 24000);
  return true;
}

Q4T_TEST(file_audio_24k_preserves_voice_band_above_asr_nyquist) {
  FileAudioPcm pcm;
  Q4T_CHECK(Decode(Bytes("stereo24.wav"), &pcm));
  Q4T_CHECK(pcm.samples.size() == 24000 && pcm.duration_ns == 1000000000);
  const auto low = Amplitude(pcm, 440), high = Amplitude(pcm, 11000);
  std::fprintf(stderr, "24k amplitudes 440Hz=%.9g 11kHz=%.9g\n", low, high);
  Q4T_CHECK(std::abs(low - .375) < .005 && high > .12 && high < .18);
  return true;
}

Q4T_TEST(file_audio_24k_resample_keeps_source_ceil_and_phase) {
  struct Case {
    uint32_t rate, frames, samples;
  };
  for (const auto& item : {Case{44100, 44101, 24001}, Case{8000, 8001, 24003},
                           Case{48000, 48001, 24001}}) {
    FileAudioPcm pcm;
    Q4T_CHECK(Decode(Wave(item.rate, item.frames), &pcm));
    Q4T_CHECK(pcm.samples.size() == item.samples &&
              pcm.duration_ns == uint64_t(item.samples) * 1000000000 / 24000);
    double error = 0;
    for (size_t i = 300; i < 23700; ++i) {
      const double expected =
          .5 * std::sin(2 * std::numbers::pi * 440 * i / 24000);
      error += std::pow(pcm.samples[i] - expected, 2);
    }
    std::fprintf(stderr, "24k source=%u RMS=%.9g\n", item.rate,
                 std::sqrt(error / 23400));
    Q4T_CHECK(std::sqrt(error / 23400) < .0001);
  }
  return true;
}

Q4T_TEST(file_audio_output_rate_validation_precedes_worker_start) {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  auto limits = Limits();
  for (const uint32_t rate : {0U, 1U, 22050U, 48000U, UINT32_MAX}) {
    limits.output_sample_rate = rate;
    FileAudioPcm pcm;
    Q4T_CHECK(!ValidFileAudioLimits(limits));
    Q4T_CHECK(DecodeAudioContainer(Bytes("mono16.wav"), limits, Access(), &pcm,
                                   control)
                  .code == "file_audio_budget_invalid");
    Q4T_CHECK(pcm.samples.empty());
  }
  Q4T_CHECK(control.counters->started == 0);
  limits = Limits();
  limits.max_output_samples = 14400000;
  Q4T_CHECK(ValidFileAudioLimits(limits));
  ++limits.max_output_samples;
  Q4T_CHECK(!ValidFileAudioLimits(limits));
  return true;
}

Q4T_TEST(file_audio_24k_sample_and_exact_rational_duration_limits) {
  const auto source = Wave(48000, 48001);
  auto limits = Limits();
  limits.max_output_samples = 24001;
  limits.max_duration_ns = 1000041667;
  FileAudioPcm pcm;
  Q4T_CHECK(Decode(source, &pcm, limits));
  for (int mode = 0; mode < 2; ++mode) {
    auto short_limit = limits;
    if (mode == 0)
      --short_limit.max_output_samples;
    else
      --short_limit.max_duration_ns;
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    Q4T_CHECK(DecodeAudioContainer(source, short_limit, Access(), &pcm, control)
                  .http_status == 413);
    Q4T_CHECK(pcm.samples.empty() && control.counters->reaped == 1 &&
              control.counters->active == 0);
  }
  return true;
}

Q4T_TEST(file_audio_rate_metadata_cannot_relabel_waveform) {
  const auto source = Wave(24000, 24000);
  FileAudioPcm pcm;
  Q4T_CHECK(Decode(source, &pcm));
  Q4T_CHECK(!ValidateAudioPcm(source, {}, pcm).ok());
  auto modified = pcm;
  modified.sample_rate = 16000;
  Q4T_CHECK(!ValidateAudioPcm(source, Limits(), modified).ok());
  modified = pcm;
  for (auto& [key, value] : modified.conversion.object)
    if (key == "sample_rate") {
      value.number = 16000;
      value.str = "16000";
    }
  Q4T_CHECK(!ValidateAudioPcm(source, Limits(), modified).ok());
  return true;
}

Q4T_TEST(file_audio_compressed_input_decodes_to_requested_24k) {
  for (const auto* name : {"tone.flac", "tone.mp3"}) {
    FileAudioPcm pcm;
    Q4T_CHECK(Decode(Bytes(name), &pcm));
    Q4T_CHECK(pcm.sample_rate == 24000 && pcm.samples.size() >= 24000 &&
              pcm.samples.size() < 30000 &&
              std::abs(Amplitude(pcm, 440) - .5) < .08);
  }
  return true;
}

Q4T_TEST(file_audio_24k_dependency_failure_does_not_disable_16k) {
  ScopeExit reset([] { detail::SetAudioWorkerEntryForTesting({}); });
  detail::SetAudioWorkerEntryForTesting("--file-audio-missing-worker");
  FileAudioPcm pcm;
  Q4T_CHECK(DecodeAudioContainer(Bytes("mono16.wav"), Limits(), Access(), &pcm)
                .code == "file_audio_dependency_unavailable");
  Q4T_CHECK(pcm.samples.empty());
  detail::SetAudioWorkerEntryForTesting({});
  Q4T_CHECK(Decode(Bytes("mono16.wav"), &pcm, {}));
  Q4T_CHECK(pcm.sample_rate == 16000 && pcm.samples.size() == 16000);
  return true;
}
