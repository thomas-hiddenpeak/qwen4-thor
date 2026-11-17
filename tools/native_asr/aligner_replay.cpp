// Real forced-alignment qualification; no synthetic timestamp output.
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

#include "q4t/audio/aligner_model.h"
#include "q4t/audio/features.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/text/tokenizer.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using namespace q4t;
using C = audio::AlignerConfig;

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
  Require(lease && *lease, "strict GPU lease required");
  char* end = nullptr;
  const long lease_fd = std::strtol(lease, &end, 10);
  struct stat lock_stat {};
  Require(end && !*end && lease_fd >= 0 && lease_fd < 1048576 &&
              !fstat(lease_fd, &lock_stat) && S_ISREG(lock_stat.st_mode) &&
              !flock(lease_fd, LOCK_EX | LOCK_NB),
          "inherited lease unavailable");
  const auto started = Clock::now();
  const auto deadline = started + std::chrono::seconds(120);
  const auto checkpoint = [&] {
    Require(Clock::now() < deadline, "alignment replay time budget exceeded");
  };
  const auto plan = Json(TextFile(plan_path, 1024 * 1024));
  const auto* cases = plan.GetArray("cases");
  Require(cases && !cases->array.empty() && cases->array.size() <= 2,
          "requires one or two frozen cases");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh output required");
  Require(runtime::ReadSystemMemory().CanAllocate(
              3 * audio::AlignerWeightBytes() + (2ULL << 30)),
          "insufficient observed unified RAM for alignment qualification");
  Check(audio::ValidateAlignerConfig(
      Json(TextFile(directory / "config.json", 128 * 1024))));
  Check(audio::ValidateAlignerPreprocessorConfig(
      Json(TextFile(directory / "preprocessor_config.json", 128 * 1024))));
  Check(audio::ValidateAlignerGenerationConfig(
      Json(TextFile(directory / "generation_config.json", 128 * 1024))));
  std::unique_ptr<text::Tokenizer> tokenizer;
  Check(text::Tokenizer::LoadQwen3Aligner(
      TextFile(directory / "vocab.json", 16 << 20),
      TextFile(directory / "merges.txt", 16 << 20),
      TextFile(directory / "tokenizer_config.json", 64 << 10), {}, &tokenizer));
  auto file = ReadFile(directory / "model.safetensors", 1835544544);
  Require(file.size() == 1835544544 && trace::HexDigest(trace::Sha256(file)) ==
                                           "47831d0e82f96b20e9034dba01a075ee064"
                                           "36654719f6a68289e49f1b65ce0e7",
          "alignment checkpoint identity differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header_bytes = 0;
  std::memcpy(&header_bytes, file.data(), sizeof(header_bytes));
  Require(header_bytes == 86744, "alignment checkpoint header differs");
  audio::AsrWeightLayout layout;
  Check(audio::ParseAlignerWeightLayout(
      Json(std::string(reinterpret_cast<const char*>(file.data() + 8),
                       header_bytes)),
      file.size() - 8 - header_bytes, &layout));
  Device device_weights(layout.bytes);
  Cuda(cudaMemcpy(device_weights.value, file.data() + 8 + header_bytes,
                  layout.bytes, cudaMemcpyHostToDevice));
  std::vector<uint8_t>().swap(file);
  audio::AlignerDeviceWeights weights;
  Check(audio::BindAlignerDeviceWeights(device_weights.value, layout.bytes,
                                        layout, &weights));
  for (size_t index = 0; index < cases->array.size(); ++index) {
    checkpoint();
    const auto& item = cases->array[index];
    const auto destination = output / ("case-" + std::to_string(index));
    fs::create_directory(destination);
    const auto raw_frames = item.GetInt("frames", -1);
    Require(raw_frames > 0 && raw_frames <= 3000, "mel frame budget");
    const int frames = static_cast<int>(raw_frames);
    const auto mel_file = ReadFile(item.GetString("mel"), 128 * 3000 * 4);
    Require(mel_file.size() == static_cast<size_t>(frames) * 128 * 4 &&
                trace::HexDigest(trace::Sha256(mel_file)) ==
                    item.GetString("mel_sha256"),
            "mel identity or shape");
    std::vector<float> mel(mel_file.size() / sizeof(float));
    std::memcpy(mel.data(), mel_file.data(), mel_file.size());
    audio::AsrEncoderLayout encoder_layout;
    Check(audio::PlanAlignerEncoderWorkspace(frames, 3000, 256ULL << 20,
                                             &encoder_layout));
    Device encoder_workspace(encoder_layout.bytes);
    audio::AlignerEncoderRun encoder;
    Check(encoder.Begin(weights, mel, frames, encoder_workspace.value,
                        encoder_layout.bytes));
    for (int i = 0; i < encoder_layout.chunks; ++i) {
      checkpoint();
      Check(encoder.NextChunk());
    }
    SaveDevice(
        destination / "encoder-input.bf16",
        {reinterpret_cast<uint16_t*>(
             static_cast<uint8_t*>(encoder_workspace.value) +
             encoder_layout.hidden),
         static_cast<size_t>(encoder_layout.tokens) * C::kEncoderHidden});
    for (int i = 0; i < C::kEncoderLayers; ++i) {
      checkpoint();
      Check(encoder.NextLayer());
    }
    Check(encoder.Finish());
    SaveDevice(destination / "encoder.bf16", encoder.output());
    const auto* expected = item.GetArray("tokens");
    const auto* words = item.GetArray("words");
    Require(expected && words && !words->array.empty() &&
                expected->array.size() <= 2048,
            "alignment case token budget");
    // Build from literal word encodings; inputs cannot inject slot markers.
    std::vector<int32_t> tokens{C::kAudioStart};
    tokens.insert(tokens.end(), encoder_layout.tokens, C::kAudioPad);
    tokens.push_back(C::kAudioEnd);
    for (const auto& word : words->array) {
      Require(word.IsString() && !word.str.empty(), "alignment word shape");
      std::vector<uint32_t> ids;
      Check(tokenizer->Encode(word.str, &ids,
                              {.recognize_special_tokens = false}));
      tokens.insert(tokens.end(), ids.begin(), ids.end());
      tokens.push_back(C::kTimestampToken);
      tokens.push_back(C::kTimestampToken);
    }
    Require(tokens.size() == expected->array.size(), "reference token count");
    for (size_t i = 0; i < tokens.size(); ++i)
      Require(tokens[i] == expected->array[i].AsInt(-1), "reference token ID");
    std::vector<int32_t> positions;
    Check(
        audio::PlanAlignerSequence(tokens, encoder_layout.tokens, &positions));
    Require(positions.size() == words->array.size() * 2, "word slot count");
    audio::AlignerDecoderLayout decoder_layout;
    Check(audio::PlanAlignerDecoderWorkspace(
        tokens.size(), positions.size(), 2048, 512ULL << 20, &decoder_layout));
    Device decoder_workspace(decoder_layout.bytes);
    audio::AlignerDecoderRun decoder;
    Check(decoder.Begin(weights, tokens, 1, encoder.output(),
                        decoder_workspace.value, decoder_layout.bytes));
    for (int i = 0; i < C::kDecoderLayers; ++i) {
      checkpoint();
      Check(decoder.NextLayer());
    }
    std::vector<uint16_t> logits(positions.size() * C::kClasses);
    Check(decoder.Finish(logits));
    Save<uint16_t>(destination / "logits.bf16", logits);
    SaveDevice(destination / "selected.bf16",
               {reinterpret_cast<uint16_t*>(
                    static_cast<uint8_t*>(decoder_workspace.value) +
                    decoder_layout.selected),
                positions.size() * C::kHidden});
    std::vector<uint32_t> predicted;
    for (size_t slot = 0; slot < positions.size(); ++slot) {
      float maximum = -std::numeric_limits<float>::infinity();
      uint32_t best = 0;
      for (int id = 0; id < C::kClasses; ++id) {
        const auto value = std::bit_cast<float>(
            static_cast<uint32_t>(logits[slot * C::kClasses + id]) << 16);
        Require(std::isfinite(value), "nonfinite alignment logit");
        if (value > maximum) {
          maximum = value;
          best = id;
        }
      }
      predicted.push_back(best);
    }
    Save<uint32_t>(destination / "classes.u32", predicted);
    std::ofstream receipt(destination / "result.json");
    receipt << "{\"slots\":" << predicted.size() << ",\"frames\":" << frames
            << ",\"tokens\":" << tokens.size()
            << ",\"encoder_workspace\":" << encoder_layout.bytes
            << ",\"decoder_workspace\":" << decoder_layout.bytes << "}\n";
    receipt.close();
    Require(receipt.good(), "alignment case receipt write failed");
    std::cout << "aligned " << index << " slots " << predicted.size()
              << std::endl;
  }
  Cuda(cudaDeviceSynchronize());
  std::ofstream receipt(output / "result.json");
  receipt << "{\"status\":\"NATIVE_COMPLETED_PENDING_COMPARISON\","
          << "\"cases\":" << cases->array.size() << ",\"seconds\":"
          << std::chrono::duration<double>(Clock::now() - started).count()
          << "}\n";
  receipt.close();
  Require(receipt.good(), "alignment receipt write failed");
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 4) {
    std::cerr << "usage: q4t_aligner_replay ASSETS PLAN_JSON FRESH_OUTPUT\n";
    return 2;
  }
  try {
    return Replay(argv[1], argv[2], argv[3]);
  } catch (const std::exception& error) {
    std::cerr << "alignment qualification failed: " << error.what() << '\n';
    return 1;
  }
}
