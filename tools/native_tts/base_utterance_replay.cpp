// Native Base asset/voice/synthesis pipeline; full audio checked independently.
#include <cuda_runtime_api.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <bit>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>

#include "q4t/audio/tts_model.h"
#include "q4t/audio/tts_codec.h"
#include "q4t/audio/tts_speaker_features.h"
#include "q4t/audio/tts_speaker_model.h"
#include "q4t/server/tts_upload.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/text/tokenizer.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using namespace q4t;

void Require(bool ok, const std::string& message) {
  if (!ok) throw std::runtime_error(message);
}
void Check(const Status& status) { Require(status.ok(), status.message()); }
void Check(const audio::TtsComputeResult& result) {
  Require(result.ok(), result.status.message());
}
void Cuda(cudaError_t result) {
  Require(result == cudaSuccess, cudaGetErrorString(result));
}

struct Fd {
  int value;
  ~Fd() {
    if (value >= 0) close(value);
  }
};
struct Device {
  void* value = nullptr;
  explicit Device(size_t bytes) { Cuda(cudaMalloc(&value, bytes)); }
  ~Device() {
    if (value) (void)cudaFree(value);
  }
  Device(const Device&) = delete;
  Device& operator=(const Device&) = delete;
};

std::vector<uint8_t> ReadFile(const fs::path& path, size_t limit) {
  Fd fd{open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK)};
  struct stat before {
  }, after{};
  Require(fd.value >= 0 && !fstat(fd.value, &before) &&
              S_ISREG(before.st_mode) && before.st_size >= 0 &&
              static_cast<uint64_t>(before.st_size) <= limit,
          "invalid file " + path.filename().string());
  std::vector<uint8_t> bytes(static_cast<size_t>(before.st_size));
  size_t offset = 0;
  while (offset < bytes.size()) {
    const auto count = read(fd.value, bytes.data() + offset,
                            std::min(size_t{1 << 20}, bytes.size() - offset));
    if (count < 0 && errno == EINTR) continue;
    Require(count > 0, "short file read");
    offset += static_cast<size_t>(count);
  }
  Require(!fstat(fd.value, &after) && before.st_size == after.st_size &&
              before.st_mtim.tv_sec == after.st_mtim.tv_sec &&
              before.st_mtim.tv_nsec == after.st_mtim.tv_nsec &&
              before.st_ctim.tv_sec == after.st_ctim.tv_sec &&
              before.st_ctim.tv_nsec == after.st_ctim.tv_nsec,
          "file changed during read");
  return bytes;
}
std::string TextFile(const fs::path& path, size_t limit) {
  const auto bytes = ReadFile(path, limit);
  return {reinterpret_cast<const char*>(bytes.data()), bytes.size()};
}
io::Json Json(const std::string& text) {
  io::Json result;
  Check(io::ParseJson(text, &result, false, {.reject_duplicate_keys = true}));
  return result;
}
template <typename T>
void Save(const fs::path& path, std::span<const T> values) {
  std::ofstream file(path, std::ios::binary | std::ios::out);
  file.write(reinterpret_cast<const char*>(values.data()), values.size_bytes());
  file.close();
  Require(file.good(), "output write failed");
}

void RunUtterances(const fs::path& directory, const fs::path& plan_file,
                   const fs::path& output) {
  const char* lease = std::getenv("Q4T_MANAGED_LEASE_FD");
  Require(lease && *lease, "requires gpu_exclusive.py --inherit-lease");
  char* end = nullptr;
  const long fd = std::strtol(lease, &end, 10);
  struct stat lock_stat {};
  Require(end && !*end && fd >= 0 && fd < 1048576 && !fstat(fd, &lock_stat) &&
              S_ISREG(lock_stat.st_mode) && !flock(fd, LOCK_EX | LOCK_NB),
          "inherited lease unavailable");
  const auto started = Clock::now();
  const auto checkpoint = [&] {
    Require(Clock::now() - started < std::chrono::seconds(600),
            "TTS utterance deadline exceeded");
  };
  const auto plan = Json(TextFile(plan_file, 65536));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 2, "exactly two TTS cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh TTS output required");
  constexpr auto variant = audio::TtsVariant::kBase17B;
  Require(runtime::ReadSystemMemory().CanAllocate(
              3 * server::kNativeTtsBaseFileBytes + (2ULL << 30)),
          "insufficient unified RAM for Base qualification");
  server::TtsHostAssets assets;
  server::GrammarControl control;
  control.compile_deadline = Clock::now() + std::chrono::seconds(60);
  control.counters = std::make_shared<server::GrammarWorkerCounters>();
  control.cancelled = [&] {
    return Clock::now() - started >= std::chrono::seconds(600);
  };
  server::GrammarFailure failure;
  Check(server::LoadTtsAssets(directory, &assets, control, &failure, true,
                              server::kTtsAssetStagingBytes, {variant, true}));
  Require(assets.variant == variant &&
              assets.speaker_layout.tensors.size() == 76 &&
              control.counters->started == 2 && control.counters->reaped == 2 &&
              control.counters->active == 0,
          "Base verified asset ownership differs");
  Device device_weights(server::kNativeTtsWeightBytes);
  Device codec_weights(audio::TtsConfig::kCodecDecoderBytes);
  Device speaker_weights(audio::TtsSpeakerConfig::kDeviceBytes);
  server::TtsBoundWeights bound;
  Check(server::UploadTtsWeights(assets,
                                 {static_cast<uint8_t*>(device_weights.value),
                                  server::kNativeTtsWeightBytes},
                                 {static_cast<uint8_t*>(codec_weights.value),
                                  audio::TtsConfig::kCodecDecoderBytes},
                                 &bound, control.cancelled));
  audio::TtsSpeakerDeviceWeights speaker;
  Check(server::UploadTtsSpeakerWeights(
      assets,
      {static_cast<uint8_t*>(speaker_weights.value),
       audio::TtsSpeakerConfig::kDeviceBytes},
      &speaker, control.cancelled));
  auto tokenizer = std::move(assets.tokenizer);
  assets = {};  // Release all host source bytes before request workspaces.
  const auto& weights = bound.talker;
  const auto& decoder_weights = bound.codec;
  size_t speaker_stages = 0;
  size_t total_advances = 0, total_frames = 0, codec_advances = 0;
  for (size_t index = 0; index < 2; ++index) {
    checkpoint();
    const auto case_start = Clock::now();
    const auto& item = cases->array[index];
    const auto case_output = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(case_output), "case directory exists");
    std::vector<uint32_t> text, instruction;
    Check(tokenizer->Encode(item.GetString("text"), &text,
                            {.recognize_special_tokens = false}));
    Check(tokenizer->Encode(item.GetString("instruction"), &instruction,
                            {.recognize_special_tokens = false}));
    const auto source = ReadFile(item.GetString("reference_pcm"), 1440000 * 4);
    Require(!source.empty() && source.size() % 4 == 0 &&
                trace::HexDigest(trace::Sha256(source)) ==
                    item.GetString("reference_pcm_sha256"),
            "voice sample identity differs");
    std::vector<float> samples(source.size() / 4);
    std::memcpy(samples.data(), source.data(), source.size());
    audio::TtsSpeakerFeatures features;
    Check(audio::ComputeTtsSpeakerFeatures(samples, 24000, {}, &features,
                                           control.cancelled));
    std::vector<float> condition(2048);
    {
      audio::TtsSpeakerWorkspace layout;
      Check(audio::PlanTtsSpeakerWorkspace(features.frames, 5625, 300ULL << 20,
                                           &layout));
      Device workspace(layout.bytes);
      audio::TtsSpeakerRun run;
      Check(run.Begin(speaker, features.mel, features.frames, workspace.value,
                      layout.bytes));
      for (int stage = 0; stage < audio::TtsSpeakerRun::kStages; ++stage) {
        checkpoint();
        Require(++speaker_stages <= 94, "speaker stage budget exceeded");
        Check(run.NextStage());
      }
      Check(run.Finish(condition));
    }
    Save<float>(case_output / "speaker.f32", condition);
    audio::TtsPrompt prompt;
    Check(audio::PlanTtsBasePrompt(
        std::vector<int32_t>(text.begin(), text.end()),
        std::vector<int32_t>(instruction.begin(), instruction.end()), condition,
        item.GetString("language"), item.GetBool("non_streaming"), 512,
        &prompt));
    Check(audio::ValidateTtsPrompt(prompt));
    audio::TtsGenerationLayout generation_layout;
    Check(audio::PlanTtsGenerationWorkspace(variant, prompt.text_ids.size(),
                                            prompt.prefill.size(), 160,
                                            512ULL << 20, &generation_layout));
    Device generation_workspace(generation_layout.bytes);
    audio::TtsGenerationRun generation;
    Check(generation.Begin(weights, prompt, 160, {.sample = false},
                           {.sample = false, .repetition_penalty = 1.0f}, 42,
                           generation_workspace.value,
                           generation_layout.bytes));
    while (!generation.done()) {
      checkpoint();
      Require(++total_advances <= 48000, "TTS generation budget exceeded");
      Check(generation.Advance());
    }
    const double generation_seconds =
        std::chrono::duration<double>(Clock::now() - case_start).count();
    const auto codes = generation.codes();
    Save<audio::TtsCodes>(case_output / "codes.i32", codes);
    std::ofstream receipt(case_output / "generation.json");
    receipt << "{\"frames\":" << codes.size()
            << ",\"eos\":" << (generation.ended() ? "true" : "false")
            << ",\"at_limit\":" << (generation.at_limit() ? "true" : "false")
            << ",\"workspace_bytes\":" << generation_layout.bytes
            << ",\"seconds\":" << generation_seconds << "}\n";
    receipt.close();
    Require(receipt.good(), "generation receipt failed");
    Require(generation.ended() && !generation.at_limit() && codes.size() >= 2 &&
                codes.size() < 160,
            "utterance failed to reach natural EOS within frozen budget");
    audio::TtsCodecLayout wave_layout;
    Check(audio::PlanTtsCodecWorkspace(codes.size(), 1ULL << 30, &wave_layout));
    Device wave_workspace(wave_layout.bytes);
    audio::TtsCodecRun decoder;
    Check(decoder.Begin(decoder_weights, codes, wave_workspace.value,
                        wave_layout.bytes));
    while (!decoder.done()) {
      checkpoint();
      Require(++codec_advances <= 4096, "TTS waveform budget exceeded");
      Check(decoder.Advance());
    }
    const auto wave = decoder.pcm();
    Require(wave.size() == codes.size() * 1920,
            "utterance waveform sample count differs");
    std::vector<float> pcm(wave.size());
    Cuda(cudaMemcpy(pcm.data(), wave.data(), wave.size_bytes(),
                    cudaMemcpyDeviceToHost));
    double sum = 0;
    float peak = 0;
    for (const float value : pcm) {
      Require(std::isfinite(value) && std::abs(value) <= 1,
              "utterance waveform is invalid");
      sum += static_cast<double>(value) * value;
      peak = std::max(peak, std::abs(value));
    }
    const double rms = std::sqrt(sum / pcm.size());
    Require(rms > 1e-4 && peak > 0.01f,
            "utterance is empty or below frozen signal floor");
    Save<float>(case_output / "pcm.f32", pcm);
    total_frames += codes.size();
    std::ofstream report(case_output / "result.json");
    report << "{\"frames\":" << codes.size() << ",\"eos\":true,"
           << "\"at_limit\":false,\"samples\":" << pcm.size()
           << ",\"sample_rate\":24000,\"rms\":" << rms << ",\"peak\":" << peak
           << ",\"generation_seconds\":" << generation_seconds
           << ",\"generation_workspace_bytes\":" << generation_layout.bytes
           << ",\"codec_workspace_bytes\":" << wave_layout.bytes
           << ",\"seconds\":"
           << std::chrono::duration<double>(Clock::now() - case_start).count()
           << "}\n";
    report.close();
    Require(report.good(), "utterance receipt failed");
  }
  Cuda(cudaDeviceSynchronize());
  std::ofstream report(output / "result.json");
  report << "{\"status\":\"PASS_UTTERANCE_PENDING_CONTENT_CHECK\","
         << "\"gpu_model_loads\":3,\"speaker_stages\":" << speaker_stages
         << ",\"cases\":2,\"frames\":" << total_frames
         << ",\"generation_advances\":" << total_advances
         << ",\"codec_advances\":" << codec_advances << ",\"seconds\":"
         << std::chrono::duration<double>(Clock::now() - started).count()
         << "}\n";
  report.close();
  Require(report.good(), "utterance final receipt failed");
}
}  // namespace

#ifdef Q4T_TTS_TRACE
namespace q4t::audio {
// Link the already qualified math library without collecting per-token traces.
// This stage checks complete EOS and actual audio, not another numeric matrix.
void TraceTtsOperand(const char*, size_t, size_t, std::span<const float>) {}
}  // namespace q4t::audio
#endif

int main(int argc, char** argv) {
  const int worker = server::MaybeRunModelAssetWorker(argc, argv);
  if (worker >= 0) return worker;
  try {
    Require(argc == 4, "usage: q4t_tts_base_utterance ASSETS PLAN OUTPUT");
    RunUtterances(argv[1], argv[2], argv[3]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
