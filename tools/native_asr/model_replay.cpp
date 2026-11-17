// Targeted real ASR numerical/business driver, not a performance benchmark.
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

#include "q4t/audio/asr_model.h"
#include "q4t/audio/features.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/text/tokenizer.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using namespace q4t;
using C = audio::AsrConfig;
#ifdef Q4T_EMBEDDING_TRACE
fs::path decoder_trace;
#endif

void Require(bool ok, const std::string& message) {
  if (!ok) throw std::runtime_error(message);
}
void Check(const Status& status) { Require(status.ok(), status.message()); }
void Check(const audio::AsrComputeResult& result) {
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
void SaveDevice(const fs::path& path, std::span<const uint16_t> values) {
  std::vector<uint16_t> host(values.size());
  Cuda(cudaMemcpy(host.data(), values.data(), values.size_bytes(),
                  cudaMemcpyDeviceToHost));
  Save<uint16_t>(path, host);
}

int Replay(const fs::path& directory, const fs::path& plan_path,
           const fs::path& output) {
  const char* lease = std::getenv("Q4T_MANAGED_LEASE_FD");
  Require(lease && *lease, "requires gpu_exclusive.py --inherit-lease");
  char* end = nullptr;
  const long lease_fd = std::strtol(lease, &end, 10);
  struct stat lock_stat {};
  Require(end && !*end && lease_fd >= 0 && lease_fd < 1048576 &&
              !fstat(lease_fd, &lock_stat) && S_ISREG(lock_stat.st_mode) &&
              !flock(lease_fd, LOCK_EX | LOCK_NB),
          "inherited lease unavailable");
  const auto started = Clock::now();
  const auto deadline = started + std::chrono::seconds(600);
  const auto checkpoint = [&] {
    Require(Clock::now() < deadline, "native ASR replay time budget exceeded");
  };
  const auto plan = Json(TextFile(plan_path, 1024 * 1024));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() >= 1 && cases->array.size() <= 3,
          "replay case budget differs");
#ifdef Q4T_EMBEDDING_TRACE
  Require(cases->array.size() == 1 &&
              cases->array[0].GetInt("max_new_tokens", -1) == 1,
          "decoder diagnostic requires exactly one case and one step");
#endif
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh replay output required");
  // Thor page cache, host snapshot and device payload all occupy unified RAM.
  Require(runtime::ReadSystemMemory().CanAllocate(3 * audio::AsrWeightBytes() +
                                                  (2ULL << 30)),
          "insufficient observed unified RAM for ASR qualification");
  Check(audio::ValidateAsrConfig(
      Json(TextFile(directory / "config.json", 128 * 1024))));
  Check(audio::ValidateAsrPreprocessorConfig(
      Json(TextFile(directory / "preprocessor_config.json", 64 * 1024))));
  Check(audio::ValidateAsrGenerationConfig(
      Json(TextFile(directory / "generation_config.json", 64 * 1024))));
  std::unique_ptr<text::Tokenizer> tokenizer;
  Check(text::Tokenizer::LoadQwen3Asr(
      TextFile(directory / "vocab.json", 16 << 20),
      TextFile(directory / "merges.txt", 16 << 20),
      TextFile(directory / "tokenizer_config.json", 64 << 10), {}, &tokenizer));
  auto file = ReadFile(directory / "model.safetensors", 1876091704);
  Require(file.size() == 1876091704 && trace::HexDigest(trace::Sha256(file)) ==
                                           "79d6cbd4c98c7bbffe9db2edac07f56cd66"
                                           "37d0d5944b27f6c2b8353840323ea",
          "ASR checkpoint identity differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header_bytes = 0;
  std::memcpy(&header_bytes, file.data(), sizeof(header_bytes));
  Require(header_bytes <= 1024 * 1024 && header_bytes < file.size() - 8,
          "ASR safetensors header size");
  audio::AsrWeightLayout weight_layout;
  Check(audio::ParseAsrWeightLayout(
      Json(std::string(reinterpret_cast<const char*>(file.data() + 8),
                       header_bytes)),
      file.size() - 8 - header_bytes, &weight_layout));
  Device device_weights(weight_layout.bytes);
  Cuda(cudaMemcpy(device_weights.value, file.data() + 8 + header_bytes,
                  weight_layout.bytes, cudaMemcpyHostToDevice));
  std::vector<uint8_t>().swap(file);
  audio::AsrDeviceWeights weights;
  Check(audio::BindAsrDeviceWeights(device_weights.value, weight_layout.bytes,
                                    weight_layout, &weights));
  int total_steps = 0;
  for (size_t c = 0; c < cases->array.size(); ++c) {
    checkpoint();
    const auto& item = cases->array[c];
    const auto case_output = output / ("case-" + std::to_string(c));
    fs::create_directory(case_output);
#ifdef Q4T_EMBEDDING_TRACE
    decoder_trace = case_output / "decoder-operands";
    fs::create_directory(decoder_trace);
#endif
    const auto mel_file = ReadFile(item.GetString("mel"), 128 * 3000 * 4);
    Require(trace::HexDigest(trace::Sha256(mel_file)) ==
                item.GetString("mel_sha256"),
            "mel identity differs");
    const auto raw_frames = item.GetInt("frames", -1);
    Require(raw_frames > 0 && raw_frames <= 3000, "mel frame budget");
    const int frames = static_cast<int>(raw_frames);
    Require(frames > 0 && frames <= 3000 &&
                mel_file.size() == static_cast<size_t>(frames) * 128 * 4,
            "mel shape differs");
    std::vector<float> mel(mel_file.size() / sizeof(float));
    std::memcpy(mel.data(), mel_file.data(), mel_file.size());
    const auto pcm_file = ReadFile(item.GetString("pcm"), 480000 * 4);
    Require(!pcm_file.empty() && pcm_file.size() % sizeof(float) == 0 &&
                trace::HexDigest(trace::Sha256(pcm_file)) ==
                    item.GetString("pcm_sha256"),
            "PCM identity differs");
    std::vector<float> pcm(pcm_file.size() / sizeof(float));
    std::memcpy(pcm.data(), pcm_file.data(), pcm_file.size());
    audio::AsrFeatures features;
    Check(audio::ComputeAsrFeatures(pcm, C::kSampleRate, {}, &features,
                                    [&] { return Clock::now() >= deadline; }));
    Require(features.frames == static_cast<size_t>(frames) &&
                features.mel.size() == mel.size(),
            "native feature shape differs");
    float mel_error = 0;
    for (size_t i = 0; i < mel.size(); ++i)
      mel_error = std::max(mel_error, std::abs(mel[i] - features.mel[i]));
    Require(mel_error <= 1e-5f, "native real-audio features differ");
    mel.swap(features.mel);
    Save<float>(case_output / "native-mel.f32", mel);
    audio::AsrEncoderLayout encoder_layout;
    Check(audio::PlanAsrEncoderWorkspace(frames, 3000, 256ULL << 20,
                                         &encoder_layout));
    Device encoder_workspace(encoder_layout.bytes);
    audio::AsrEncoderRun encoder;
    Check(encoder.Begin(weights, mel, frames, encoder_workspace.value,
                        encoder_layout.bytes));
    for (int i = 0; i < encoder_layout.chunks; ++i) {
      checkpoint();
      Check(encoder.NextChunk());
    }
    SaveDevice(
        case_output / "encoder-input.bf16",
        {reinterpret_cast<uint16_t*>(
             static_cast<uint8_t*>(encoder_workspace.value) +
             encoder_layout.hidden),
         static_cast<size_t>(encoder_layout.tokens) * C::kEncoderHidden});
    for (int i = 0; i < C::kEncoderLayers; ++i) {
      checkpoint();
      Check(encoder.NextLayer());
    }
    Check(encoder.Finish());
    SaveDevice(case_output / "encoder.bf16", encoder.output());
    const auto* token_array = item.GetArray("tokens");
    Require(token_array && !token_array->array.empty() &&
                token_array->array.size() <= 2048,
            "input token budget");
    std::vector<int32_t> tokens;
    for (const auto& token : token_array->array) {
      const auto id = token.AsInt(-1);
      Require(id >= 0 && id < tokenizer->VocabSize(), "invalid token ID");
      tokens.push_back(static_cast<int32_t>(id));
    }
    const auto audio_start = item.GetInt("audio_start", -1);
    const auto raw_max_new = item.GetInt("max_new_tokens", -1);
    Require(raw_max_new > 0 && raw_max_new <= 128, "output token budget");
    const int max_new = static_cast<int>(raw_max_new);
    Require(audio_start >= 0 && max_new > 0 && max_new <= 128,
            "generation budget");
    audio::AsrDecoderLayout maximum;
    Check(audio::PlanAsrDecoderWorkspace(tokens.size() + max_new, 2176,
                                         512ULL << 20, &maximum));
    Device decoder_workspace(maximum.bytes);
    std::vector<uint16_t> logits(C::kVocab);
    std::vector<uint32_t> generated;
    bool eos = false;
    for (int step = 0; step < max_new; ++step) {
      checkpoint();
      Require(++total_steps <= 256, "cumulative generation budget");
      audio::AsrDecoderStep decoder;
      Check(decoder.Begin(weights, tokens, audio_start, encoder.output(),
                          decoder_workspace.value, maximum.bytes));
      for (int layer = 0; layer < C::kDecoderLayers; ++layer) {
        checkpoint();
        Check(decoder.NextLayer());
      }
      Check(decoder.Finish(logits));
      if (step < 4)
        Save<uint16_t>(
            case_output / ("logits-" + std::to_string(step) + ".bf16"), logits);
      int best = -1;
      float maximum_logit = -std::numeric_limits<float>::infinity();
      for (int id = 0; id < C::kVocab; ++id) {
        const auto value =
            std::bit_cast<float>(static_cast<uint32_t>(logits[id]) << 16);
        Require(std::isfinite(value), "nonfinite ASR logit");
        if (value > maximum_logit) {
          maximum_logit = value;
          best = id;
        }
      }
      Require(best >= 0 && best < static_cast<int>(tokenizer->VocabSize()),
              "model selected undefined tokenizer row");
      generated.push_back(best);
      tokens.push_back(best);
      if (best == C::kEos || best == C::kImEnd) {
        eos = true;
        break;
      }
    }
    Save<uint32_t>(case_output / "generated.u32", generated);
    std::string decoded;
    Check(tokenizer->Decode(generated, true, &decoded));
    Save<char>(case_output / "text.txt", decoded);
    std::ofstream result(case_output / "result.json");
    result << "{\"frames\":" << frames
           << ",\"audio_tokens\":" << encoder_layout.tokens
           << ",\"generated_tokens\":" << generated.size()
           << ",\"eos\":" << (eos ? "true" : "false")
           << ",\"encoder_workspace_bytes\":" << encoder_layout.bytes
           << ",\"decoder_workspace_bytes\":" << maximum.bytes << "}\n";
    result.close();
    Require(result.good(), "case receipt write failed");
    std::cout << "case " << c << " generated " << generated.size() << " EOS "
              << eos << std::endl;
  }
  Cuda(cudaDeviceSynchronize());
  std::ofstream result(output / "result.json");
  result << "{\"status\":\"NATIVE_EXECUTION_COMPLETED_NOT_YET_COMPARED\","
         << "\"cases\":" << cases->array.size() << ",\"steps\":" << total_steps
         << ",\"seconds\":"
         << std::chrono::duration<double>(Clock::now() - started).count()
         << "}\n";
  result.close();
  Require(result.good(), "replay receipt write failed");
  return 0;
}
}  // namespace

#ifdef Q4T_EMBEDDING_TRACE
namespace q4t::model {
void TraceEmbeddingOperand(int layer, const char* name, const uint16_t* data,
                           size_t values) {
  if (decoder_trace.empty()) return;
  const auto directory = decoder_trace / ("layer-" + std::to_string(layer));
  std::filesystem::create_directory(directory);
  SaveDevice(directory / (std::string(name) + ".bf16"), {data, values});
}
}  // namespace q4t::model
#endif

int main(int argc, char** argv) {
  if (argc != 4) {
    std::cerr << "usage: q4t_asr_replay ASSETS PLAN_JSON FRESH_OUTPUT\n";
    return 2;
  }
  try {
    return Replay(argv[1], argv[2], argv[3]);
  } catch (const std::exception& error) {
    std::cerr << "ASR qualification failed: " << error.what() << '\n';
    return 1;
  }
}
