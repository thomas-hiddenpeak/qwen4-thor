// Bounded full-utterance generation; content checked by independent ASR.
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
  constexpr auto variant = audio::TtsVariant::kCustomVoice17B;
  constexpr size_t kFileBytes = 3833402552;
  Require(
      runtime::ReadSystemMemory().CanAllocate(3 * kFileBytes + (2ULL << 30)),
      "insufficient unified RAM for TTS qualification");
  Check(audio::ValidateTtsConfig(
      Json(TextFile(directory / "config.json", 65536)), variant));
  Check(audio::ValidateTtsGenerationConfig(
      Json(TextFile(directory / "generation_config.json", 65536))));
  std::unique_ptr<text::Tokenizer> tokenizer;
  text::TokenizerLimits limits;
  limits.max_input_bytes = 4096;
  limits.max_tokens = 512;
  Check(text::Tokenizer::LoadQwen3Tts(
      TextFile(directory / "vocab.json", 4 << 20),
      TextFile(directory / "merges.txt", 2 << 20),
      TextFile(directory / "tokenizer_config.json", 65536), limits,
      &tokenizer));
  auto file = ReadFile(directory / "model.safetensors", kFileBytes);
  Require(file.size() == kFileBytes && trace::HexDigest(trace::Sha256(file)) ==
                                           "38b1d5971bdbd982b561cccec982669a53b"
                                           "0537c3cf5e9bd4778ed07bb2f5137",
          "TTS checkpoint identity differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header_bytes;
  std::memcpy(&header_bytes, file.data(), sizeof(header_bytes));
  Require(header_bytes <= 1048576 && header_bytes < file.size() - 8,
          "invalid TTS tensor header size");
  audio::TtsWeightLayout weight_layout;
  Check(audio::ParseTtsWeightLayout(
      Json(std::string(reinterpret_cast<const char*>(file.data() + 8),
                       header_bytes)),
      file.size() - 8 - header_bytes, variant, &weight_layout));
  Device device_weights(weight_layout.bytes * 2);
  std::vector<float> expanded(1 << 20);
  for (const auto& tensor : weight_layout.tensors) {
    checkpoint();
    for (size_t offset = 0; offset < tensor.bytes;) {
      const size_t count =
          std::min(expanded.size(), (tensor.bytes - offset) / 2);
      const auto* source = reinterpret_cast<const uint16_t*>(
          file.data() + 8 + header_bytes + tensor.file_offset + offset);
      Check(audio::ExpandTtsWeightChunk({source, count},
                                        {expanded.data(), count}));
      Cuda(cudaMemcpy(static_cast<uint8_t*>(device_weights.value) +
                          (tensor.offset + offset) * 2,
                      expanded.data(), count * sizeof(float),
                      cudaMemcpyHostToDevice));
      offset += count * 2;
    }
  }
  std::vector<uint8_t>().swap(file);
  std::vector<float>().swap(expanded);
  audio::TtsDeviceWeights weights;
  Check(audio::BindTtsDeviceWeights(device_weights.value,
                                    weight_layout.bytes * 2, weight_layout,
                                    variant, &weights));
  const auto codec_dir = directory / "speech_tokenizer";
  Check(audio::ValidateTtsCodecConfig(
      Json(TextFile(codec_dir / "config.json", 65536))));
  constexpr size_t kCodecFileBytes = 682293092;
  auto codec_file = ReadFile(codec_dir / "model.safetensors", kCodecFileBytes);
  Require(codec_file.size() == kCodecFileBytes &&
              trace::HexDigest(trace::Sha256(codec_file)) ==
                  "836b7b357f5ea43e889936a3709af68dfe3"
                  "751881acefe4ecf0dbd30ba571258",
          "codec checkpoint identity differs");
  uint64_t codec_header;
  std::memcpy(&codec_header, codec_file.data(), sizeof(codec_header));
  Require(codec_header < (1 << 20) && codec_header < codec_file.size() - 8,
          "codec tensor header invalid");
  audio::TtsWeightLayout codec_layout;
  Check(audio::ParseTtsCodecWeightLayout(
      Json(std::string(reinterpret_cast<const char*>(codec_file.data() + 8),
                       codec_header)),
      codec_file.size() - 8 - codec_header, &codec_layout));
  Device codec_weights(codec_layout.bytes);
  for (const auto& tensor : codec_layout.tensors) {
    checkpoint();
    Cuda(cudaMemcpy(static_cast<uint8_t*>(codec_weights.value) + tensor.offset,
                    codec_file.data() + 8 + codec_header + tensor.file_offset,
                    tensor.bytes, cudaMemcpyHostToDevice));
  }
  std::vector<uint8_t>().swap(codec_file);
  audio::TtsCodecDeviceWeights decoder_weights;
  Check(audio::BindTtsCodecDeviceWeights(
      codec_weights.value, codec_layout.bytes, codec_layout, &decoder_weights));
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
    audio::TtsPrompt prompt;
    Check(audio::PlanTtsPrompt(
        variant, std::vector<int32_t>(text.begin(), text.end()),
        std::vector<int32_t>(instruction.begin(), instruction.end()),
        item.GetString("voice"), item.GetString("language"),
        item.GetBool("non_streaming"), 512, &prompt));
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
         << "\"gpu_model_loads\":2,\"cases\":2,\"frames\":" << total_frames
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
  try {
    Require(argc == 4, "usage: q4t_tts_utterance ASSETS PLAN OUTPUT");
    RunUtterances(argv[1], argv[2], argv[3]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
