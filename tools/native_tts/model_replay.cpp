// Targeted TTS code-generation numerical driver, not a speech-quality test.
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
#include "q4t/runtime/system_memory.h"
#include "q4t/text/tokenizer.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using namespace q4t;
fs::path trace_directory;

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

void Run(const fs::path& directory, const fs::path& plan_file,
           const fs::path& output) {
  const char* lease = std::getenv("Q4T_MANAGED_LEASE_FD");
  Require(lease && *lease, "requires gpu_exclusive.py --inherit-lease");
  char* end = nullptr;
  const long fd = std::strtol(lease, &end, 10);
  struct stat lock_stat {};
  Require(end && !*end && fd >= 0 && fd < 1048576 &&
              !fstat(fd, &lock_stat) && S_ISREG(lock_stat.st_mode) &&
              !flock(fd, LOCK_EX | LOCK_NB), "inherited lease unavailable");
  const auto started = Clock::now();
  const auto checkpoint = [&] {
    Require(Clock::now() - started < std::chrono::seconds(600),
            "TTS component deadline exceeded");
  };
  const auto plan = Json(TextFile(plan_file, 65536));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 2, "exactly two TTS cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh TTS output required");
  constexpr auto variant = audio::TtsVariant::kCustomVoice17B;
  constexpr size_t kFileBytes = 3833402552;
  Require(runtime::ReadSystemMemory().CanAllocate(3 * kFileBytes + (2ULL << 30)),
          "insufficient unified RAM for TTS qualification");
  Check(audio::ValidateTtsConfig(Json(TextFile(directory / "config.json",
                                               65536)), variant));
  Check(audio::ValidateTtsGenerationConfig(Json(TextFile(
      directory / "generation_config.json", 65536))));
  std::unique_ptr<text::Tokenizer> tokenizer;
  text::TokenizerLimits limits;
  limits.max_input_bytes = 4096;
  limits.max_tokens = 512;
  Check(text::Tokenizer::LoadQwen3Tts(TextFile(directory / "vocab.json", 4 << 20),
      TextFile(directory / "merges.txt", 2 << 20),
      TextFile(directory / "tokenizer_config.json", 65536), limits, &tokenizer));
  auto file = ReadFile(directory / "model.safetensors", kFileBytes);
  Require(file.size() == kFileBytes && trace::HexDigest(trace::Sha256(file)) ==
      "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137",
      "TTS checkpoint identity differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header_bytes;
  std::memcpy(&header_bytes, file.data(), sizeof(header_bytes));
  Require(header_bytes <= 1048576 && header_bytes < file.size() - 8,
          "invalid TTS tensor header size");
  audio::TtsWeightLayout weight_layout;
  Check(audio::ParseTtsWeightLayout(Json(std::string(
      reinterpret_cast<const char*>(file.data() + 8), header_bytes)),
      file.size() - 8 - header_bytes, variant, &weight_layout));
  Device device_weights(weight_layout.bytes);
  for (const auto& tensor : weight_layout.tensors) {
    checkpoint();
    Cuda(cudaMemcpy(static_cast<uint8_t*>(device_weights.value) + tensor.offset,
        file.data() + 8 + header_bytes + tensor.file_offset,
        tensor.bytes, cudaMemcpyHostToDevice));
  }
  std::vector<uint8_t>().swap(file);
  audio::TtsDeviceWeights weights;
  Check(audio::BindTtsDeviceWeights(device_weights.value, weight_layout.bytes,
                                     weight_layout, variant, &weights));
  size_t total_advances = 0;
  for (size_t index = 0; index < cases->array.size(); ++index) {
    checkpoint();
    const auto& item = cases->array[index];
    const auto case_output = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(case_output), "case directory exists");
    trace_directory = case_output;
    std::vector<uint32_t> text, instruction;
    Check(tokenizer->Encode(item.GetString("text"), &text,
                               {.recognize_special_tokens = false}));
    Check(tokenizer->Encode(item.GetString("instruction"), &instruction,
                               {.recognize_special_tokens = false}));
    const std::vector<int32_t> ids(text.begin(), text.end());
    const std::vector<int32_t> instruction_ids(instruction.begin(), instruction.end());
    audio::TtsPrompt prompt;
    Check(audio::PlanTtsPrompt(variant, ids, instruction_ids,
        item.GetString("voice"), item.GetString("language"),
        item.GetBool("non_streaming"), 512, &prompt));
    Check(audio::ValidateTtsPrompt(prompt));
    Save<int32_t>(case_output / "text-ids.i32", prompt.text_ids);
    audio::TtsGenerationLayout layout;
    Check(audio::PlanTtsGenerationWorkspace(variant, prompt.text_ids.size(),
        prompt.prefill.size(), 2, 512ULL << 20, &layout));
    Device workspace(layout.bytes);
    audio::TtsGenerationRun run;
    auto invalid = run.Begin(weights, prompt, 2, {}, {}, 0, nullptr, 0);
    Require(!invalid.ok() && invalid.device_error == cudaSuccess,
            "invalid TTS input poisoned the device contract");
    Check(run.Begin(weights, prompt, 2, {.sample = false},
        {.sample = false, .repetition_penalty = 1.0f}, 42,
        workspace.value, layout.bytes));
    while (!run.done()) {
      checkpoint();
      Require(++total_advances <= 600, "TTS advance budget exceeded");
      Check(run.Advance());
    }
    Require(run.at_limit() && !run.ended() && run.frames() == 2,
            "TTS two-frame component output differs");
    invalid = run.Advance();
    Require(!invalid.ok() && invalid.device_error == cudaSuccess,
            "finished TTS run accepted more work");
    Save<audio::TtsCodes>(case_output / "codes.i32", run.codes());
    std::ofstream report(case_output / "result.json");
    report << "{\"frames\":2,\"groups\":16,\"at_limit\":true,"
           << "\"eos\":false,\"prefill_tokens\":" << prompt.prefill.size()
           << ",\"text_tokens\":" << prompt.text_ids.size()
           << ",\"workspace_bytes\":" << layout.bytes << "}\n";
    report.close();
    Require(report.good(), "TTS case report write failed");
  }
  Cuda(cudaDeviceSynchronize());
  std::ofstream result(output / "result.json");
  result << "{\"status\":\"PASS_NATIVE_EXECUTION_ONLY\",\"cases\":2,"
         << "\"gpu_model_loads\":1,\"frames\":4,\"advances\":" << total_advances
         << ",\"seconds\":" << std::chrono::duration<double>(Clock::now() - started).count()
         << "}\n";
  result.close();
  Require(result.good(), "TTS final report write failed");
}
}  // namespace

#ifdef Q4T_TTS_TRACE
namespace q4t::audio {
void TraceTtsOperand(const char* name, size_t frame, size_t group,
                     std::span<const uint16_t> device_values) {
  Require(!trace_directory.empty() && device_values.size() <= 512 * 2048,
          "TTS trace exceeds frozen element budget");
  std::vector<uint16_t> host(device_values.size());
  Cuda(cudaMemcpy(host.data(), device_values.data(), device_values.size_bytes(),
                    cudaMemcpyDeviceToHost));
  Save<uint16_t>(trace_directory / (std::string(name) + "-" +
      std::to_string(frame) + "-" + std::to_string(group) + ".bf16"), host);
}
}  // namespace q4t::audio
#endif

int main(int argc, char** argv) {
  try {
    Require(argc == 4, "usage: q4t_tts_replay ASSETS PLAN OUTPUT");
    Run(argv[1], argv[2], argv[3]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
