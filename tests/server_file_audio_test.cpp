#include "q4t/server/file_audio.h"

#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numbers>
#include <thread>

#include "../src/server/file_audio_protocol.h"
#include "../src/server/file_video_protocol.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:audio", Clock::now() + 5s, {}}; }
std::string Bytes(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/file_audio" / name,
                     std::ios::binary);
  if (!file) throw std::runtime_error("audio fixture missing");
  return {std::istreambuf_iterator<char>(file), {}};
}
GrammarControl Control() {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  return control;
}
bool Clean(const GrammarControl& control, uint64_t count = 1) {
  detail::PollRequestWorkers();
  return control.counters->started == count &&
         control.counters->reaped == count && control.counters->active == 0;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool Load(const char* name, FileAudioPcm* pcm) {
  auto control = Control();
  const auto bytes = Bytes(name);
  Q4T_CHECK(Ok(DecodeAudioContainer(bytes, {}, Access(), pcm, control)));
  Q4T_CHECK(Clean(control));
  Q4T_CHECK(ValidateAudioPcm(bytes, {}, *pcm).ok());
  return true;
}
void Capture(const char* name, const FileAudioPcm& pcm) {
  const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!directory || !*directory) return;
  const auto base = std::filesystem::path(directory) / name;
  std::ofstream bytes(base.string() + ".f32", std::ios::binary);
  bytes.write(reinterpret_cast<const char*>(pcm.samples.data()),
              pcm.samples.size() * sizeof(float));
  std::ofstream(base.string() + ".json") << OutputJson(pcm.conversion);
}
bool Tone(const char* name) {
  FileAudioPcm pcm;
  Q4T_CHECK(Load(name, &pcm));
  if (std::string_view(name) == "tone.m4a" ||
      std::string_view(name) == "tone.aac") {
    Capture(name, pcm);
    std::fprintf(stderr, "%s decoded samples=%zu\n", name, pcm.samples.size());
  }
  // Lossy codecs have explicit priming/padding. Check the real decoded
  // signal rather than inventing exact original sample timestamps.
  Q4T_CHECK(pcm.samples.size() >= 15000 && pcm.samples.size() <= 19000);
  const size_t begin = 2000, end = 14000;
  double real = 0, imaginary = 0, energy = 0;
  for (size_t i = begin; i < end; ++i) {
    const double angle = 2 * std::numbers::pi * 440 * i / 16000;
    real += pcm.samples[i] * std::cos(angle);
    imaginary += pcm.samples[i] * std::sin(angle);
    energy += double(pcm.samples[i]) * pcm.samples[i];
  }
  const double amplitude = 2 * std::hypot(real, imaginary) / (end - begin);
  Q4T_CHECK(amplitude > .35 && amplitude < .6);
  Q4T_CHECK(energy / (end - begin) > .06 && energy / (end - begin) < .20);
  return true;
}
}  // namespace
int AudioWorkerFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const bool hang = !std::strcmp(argv[1], "--audio-hang-fixture");
  const bool crash = !std::strcmp(argv[1], "--audio-crash-fixture");
  if (!hang && !crash) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  audio_detail::Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.bytes > (128ULL << 20))
    return 3;
  std::string source(request.bytes, '\0');
  if (!detail::WorkerTransfer(source.data(), source.size(), false)) return 3;
  auto ready = audio_detail::Ready::kReady;
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  const int listener = video_detail::EnterVideoSandbox();
  if (listener < 0 || !detail::SendWorkerIoListener(listener)) return 3;
  close(listener);
  if (crash) _exit(17);
  for (;;) std::this_thread::sleep_for(1ms);
}
Q4T_TEST(file_audio_pcm16_wave_matches_every_source_sample) {
  FileAudioPcm pcm;
  Q4T_CHECK(Load("mono16.wav", &pcm));
  const auto source = Bytes("mono16.wav");
  Q4T_CHECK(pcm.samples.size() == 16000 && pcm.duration_ns == 1000000000);
  for (size_t i = 0; i < pcm.samples.size(); ++i) {
    int16_t sample;
    std::memcpy(&sample, source.data() + 44 + 2 * i, 2);
    Q4T_CHECK(pcm.samples[i] == sample / 32768.f);
  }
  Q4T_CHECK(pcm.conversion.GetInt("source_frames", 0) == 16000);
  return true;
}
Q4T_TEST(file_audio_pcm24_resamples_and_averages_channels_without_aliasing) {
  FileAudioPcm pcm;
  Q4T_CHECK(Load("stereo24.wav", &pcm));
  Capture("stereo24.wav", pcm);
  Q4T_CHECK(pcm.samples.size() == 16000 &&
            pcm.conversion.GetInt("source_channels", 0) == 2 &&
            pcm.conversion.GetInt("source_sample_rate", 0) == 48000);
  double square = 0;
  for (size_t i = 200; i < 15800; ++i) {
    const double expected =
        .375 * std::sin(2 * std::numbers::pi * 440 * i / 16000);
    square += std::pow(pcm.samples[i] - expected, 2);
  }
  std::fprintf(stderr, "stereo24 interior RMS error=%.9g\n",
               std::sqrt(square / 15600));
  Q4T_CHECK(std::sqrt(square / 15600) < 0.0001);
  return true;
}
Q4T_TEST(file_audio_noninteger_and_upsample_keep_phase_and_length) {
  struct Case {
    uint32_t rate, frames, output;
  };
  for (const auto& item : {Case{44100, 44100, 16000}, Case{8000, 8000, 16000},
                           Case{44100, 44101, 16001}, Case{8000, 8001, 16002},
                           Case{48000, 48001, 16001}}) {
    const auto rate = item.rate;
    auto source = Bytes("mono16.wav");
    source.resize(44 + item.frames * 2);
    const auto put = [&](size_t offset, uint32_t value) {
      std::memcpy(source.data() + offset, &value, sizeof(value));
    };
    put(4, source.size() - 8);
    put(24, rate);
    put(28, rate * 2);
    put(40, item.frames * 2);
    for (uint32_t i = 0; i < item.frames; ++i) {
      const double phase = 2 * std::numbers::pi * i / rate;
      const double wave = .5 * std::sin(440 * phase) +
                          (rate == 44100 ? .15 * std::sin(11000 * phase) : 0);
      const int16_t value = std::lround(wave * 32767);
      std::memcpy(source.data() + 44 + i * 2, &value, 2);
    }
    auto control = Control();
    FileAudioPcm pcm;
    Q4T_CHECK(Ok(DecodeAudioContainer(source, {}, Access(), &pcm, control)));
    std::fprintf(stderr, "resample rate=%u samples=%zu\n", rate,
                 pcm.samples.size());
    Capture(("rate" + std::to_string(rate) + "-frames" +
             std::to_string(item.frames))
                .c_str(),
            pcm);
    Q4T_CHECK(Clean(control));
    Q4T_CHECK(pcm.samples.size() == item.output);
    if (rate == 8000 && item.frames == 8000)
      Q4T_CHECK(std::abs(pcm.samples.back()) > .01f);
    double square = 0;
    for (size_t i = 200; i < 15800; ++i) {
      const double expected =
          .5 * std::sin(2 * std::numbers::pi * 440 * i / 16000);
      square += std::pow(pcm.samples[i] - expected, 2);
    }
    std::fprintf(stderr, "resample rate=%u interior RMS error=%.9g\n", rate,
                 std::sqrt(square / 15600));
    Q4T_CHECK(std::sqrt(square / 15600) < .0001);
  }
  return true;
}
Q4T_TEST(file_audio_float32_and_float64_preserve_finite_values) {
  for (const auto* name : {"float32.wav", "float64.wav"}) {
    FileAudioPcm pcm;
    Q4T_CHECK(Load(name, &pcm));
    Q4T_CHECK(pcm.samples.size() == 1600);
    for (int i = 0; i < 1600; ++i)
      Q4T_CHECK(pcm.samples[i] == (i % 17 - 8) / 16.f);
  }
  return true;
}
Q4T_TEST(file_audio_flac_lossless_matches_pcm16) {
  FileAudioPcm pcm;
  Q4T_CHECK(Load("tone.flac", &pcm));
  const auto source = Bytes("mono16.wav");
  Q4T_CHECK(pcm.samples.size() == 16000);
  for (size_t i = 0; i < pcm.samples.size(); ++i) {
    int16_t sample;
    std::memcpy(&sample, source.data() + 44 + 2 * i, 2);
    Q4T_CHECK(pcm.samples[i] == sample / 32768.f);
  }
  return true;
}
Q4T_TEST(file_audio_mp3_decodes_real_signal) { return Tone("tone.mp3"); }
Q4T_TEST(file_audio_m4a_and_adts_aac_decode_real_signal) {
  const bool mp4 = Tone("tone.m4a");
  const bool adts = Tone("tone.aac");
  return mp4 && adts;
}
Q4T_TEST(file_audio_ogg_opus_and_vorbis_decode_real_signal) {
  return Tone("tone.opus") && Tone("tone.ogg");
}
Q4T_TEST(file_audio_webm_opus_decodes_real_signal) { return Tone("tone.webm"); }
Q4T_TEST(file_audio_preflight_scope_limits_cancel_and_deadline) {
  const auto source = Bytes("mono16.wav");
  auto control = Control();
  FileAudioPcm pcm;
  Q4T_CHECK(DecodeAudioContainer("not audio", {}, Access(), &pcm, control)
                .http_status == 400);
  auto broken = source;
  broken.resize(30);
  Q4T_CHECK(
      DecodeAudioContainer(broken, {}, Access(), &pcm, control).http_status ==
      400);
  auto limits = FileAudioLimits{};
  limits.max_source_bytes = source.size() - 1;
  Q4T_CHECK(DecodeAudioContainer(source, limits, Access(), &pcm, control)
                .http_status == 413);
  limits.max_channels = 0;
  Q4T_CHECK(DecodeAudioContainer(source, limits, Access(), &pcm, control)
                .http_status == 500);
  auto access = Access();
  access.scope.clear();
  Q4T_CHECK(DecodeAudioContainer(source, {}, access, &pcm, control).code ==
            "object_scope_unavailable");
  access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(
      DecodeAudioContainer(source, {}, access, &pcm, control).http_status ==
      499);
  access = Access();
  access.deadline = Clock::now() - 1ms;
  Q4T_CHECK(
      DecodeAudioContainer(source, {}, access, &pcm, control).http_status ==
      408);
  Q4T_CHECK(pcm.samples.empty() && Clean(control, 0));
  return true;
}
Q4T_TEST(file_audio_nonfinite_pcm_is_request_local) {
  for (float value : {std::numeric_limits<float>::quiet_NaN(),
                      std::numeric_limits<float>::infinity()}) {
    auto source = Bytes("float32.wav");
    std::memcpy(source.data() + 44, &value, sizeof(value));
    auto control = Control();
    FileAudioPcm pcm;
    const auto reply =
        DecodeAudioContainer(source, {}, Access(), &pcm, control);
    Q4T_CHECK(reply.http_status == 400 && pcm.samples.empty() &&
              Clean(control));
  }
  return true;
}
Q4T_TEST(file_audio_actual_source_decode_and_output_budgets) {
  const auto source = Bytes("stereo24.wav");
  std::vector<FileAudioLimits> limits(5);
  limits[0].max_channels = 1;
  limits[1].max_sample_rate = 32000;
  limits[2].max_decoded_values = 95999;
  limits[3].max_output_samples = 15999;
  limits[4].max_duration_ns = 999999999;
  for (const auto& limit : limits) {
    auto control = Control();
    FileAudioPcm pcm;
    const auto reply =
        DecodeAudioContainer(source, limit, Access(), &pcm, control);
    if (reply.http_status != 413) Ok(reply);
    Q4T_CHECK(reply.http_status == 413 && pcm.samples.empty() &&
              Clean(control));
  }
  return true;
}
Q4T_TEST(file_audio_dependency_failure_keeps_text_and_slots_healthy) {
  ScopeExit reset([] { detail::SetAudioWorkerEntryForTesting(""); });
  detail::SetAudioWorkerEntryForTesting("--file-audio-missing-worker");
  auto control = Control();
  FileAudioPcm pcm;
  Q4T_CHECK(
      DecodeAudioContainer(Bytes("mono16.wav"), {}, Access(), &pcm, control)
          .code == "file_audio_dependency_unavailable");
  Q4T_CHECK(pcm.samples.empty() && Clean(control));
  FileInputContent input;
  input.bytes = "healthy text after audio dependency failure";
  input.filename = "text.txt";
  FileDocument text;
  control = Control();
  Q4T_CHECK(ParseNativeFileDocument(input, {}, Access(), &text, control).ok());
  Q4T_CHECK(text.text == input.bytes && Clean(control));
  return true;
}
Q4T_TEST(file_audio_hang_crash_inflight_cancel_have_bounded_cleanup) {
  ScopeExit reset([] { detail::SetAudioWorkerEntryForTesting(""); });
  for (int mode = 0; mode < 3; ++mode) {
    detail::SetAudioWorkerEntryForTesting(mode == 1 ? "--audio-crash-fixture"
                                                    : "--audio-hang-fixture");
    auto control = Control();
    auto access = Access();
    bool ready = false;
    if (mode == 0) access.deadline = Clock::now() + 150ms;
    if (mode == 2) {
      control.worker_ready = [&] { ready = true; };
      access.cancelled = [&] { return ready; };
    }
    FileAudioPcm pcm;
    const auto start = Clock::now();
    const auto reply =
        DecodeAudioContainer(Bytes("mono16.wav"), {}, access, &pcm, control);
    Q4T_CHECK(reply.http_status == (mode == 0 ? 408 : mode == 1 ? 503 : 499));
    Q4T_CHECK(Clock::now() - start < 2s && pcm.samples.empty() &&
              Clean(control));
  }
  return true;
}
Q4T_TEST(file_audio_unreaped_worker_preserves_shared_parse_capacity) {
  auto control = Control();
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> worker;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", control,
                                          &worker));
  detail::DeferGrammarReapingForTesting(true);
  ScopeExit reset([&] {
    detail::DeferGrammarReapingForTesting(false);
    const auto end = Clock::now() + 300ms;
    while (!control.counters->reaped && Clock::now() < end) {
      detail::PollRequestWorkers();
      std::this_thread::sleep_for(1ms);
    }
  });
  worker.reset();
  FileAudioPcm pcm;
  Q4T_CHECK(
      DecodeAudioContainer(Bytes("mono16.wav"), {}, Access(), &pcm).code ==
      "file_audio_worker_capacity");
  Q4T_CHECK(control.counters->active == 1 && control.counters->started == 1);
  return true;
}
Q4T_TEST(file_audio_conversion_rejects_source_pcm_and_policy_tampering) {
  FileAudioPcm original;
  Q4T_CHECK(Load("mono16.wav", &original));
  const auto source = Bytes("mono16.wav");
  for (int mode = 0; mode < 4; ++mode) {
    auto modified = original;
    if (mode == 0) modified.samples[100] += 1;
    if (mode == 1) modified.duration_ns += 1;
    if (mode == 2)
      Set(&modified.conversion, "mono_policy", String("first-only"));
    if (mode == 3) Set(&modified.conversion, "source_frames", Number(-1));
    Q4T_CHECK(!ValidateAudioPcm(source, {}, modified).ok());
  }
  auto other = source;
  other.back() ^= 1;
  Q4T_CHECK(!ValidateAudioPcm(other, {}, original).ok());
  return true;
}
Q4T_TEST(file_audio_corrupt_container_is_bounded_and_reaped) {
  for (const auto* name : {"tone.flac", "tone.m4a", "tone.webm"}) {
    auto source = Bytes(name);
    source.resize(16);
    auto control = Control();
    auto access = Access();
    access.deadline = Clock::now() + 500ms;
    FileAudioPcm pcm;
    const auto reply = DecodeAudioContainer(source, {}, access, &pcm, control);
    Q4T_CHECK((reply.http_status == 400 || reply.http_status == 408) &&
              pcm.samples.empty() && Clean(control));
  }
  return true;
}
Q4T_TEST(file_audio_thread_budget_failure_does_not_disable_next_request) {
  FileAudioLimits limits;
  limits.max_thread_creations = 0;
  auto control = Control();
  FileAudioPcm pcm;
  const auto reply = DecodeAudioContainer(Bytes("mono16.wav"), limits, Access(),
                                          &pcm, control);
  Q4T_CHECK(!reply.ok() && pcm.samples.empty() && Clean(control));
  Q4T_CHECK(Load("mono16.wav", &pcm));
  return true;
}
