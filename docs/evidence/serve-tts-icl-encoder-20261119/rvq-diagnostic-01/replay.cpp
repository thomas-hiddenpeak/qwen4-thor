// Targeted reference codec encoder qualification, not speech-quality evidence.
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

#include "q4t/audio/tts_encoder.h"
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

void RunEncoder(const fs::path& directory, const fs::path& plan_file,
                const fs::path& output) {
  const char* inherited = std::getenv("Q4T_MANAGED_LEASE_FD");
  Require(inherited && *inherited, "strict inherited lease required");
  char* end = nullptr;
  const long fd = std::strtol(inherited, &end, 10);
  struct stat info {};
  Require(end && !*end && fd >= 0 && fd < 1048576 && !fstat(fd, &info) &&
              S_ISREG(info.st_mode) && !flock(fd, LOCK_EX | LOCK_NB),
          "inherited lease invalid");
  const auto started = Clock::now();
  const auto checkpoint = [&] {
    Require(Clock::now() - started < std::chrono::seconds(600),
            "encoder component deadline exceeded");
  };
  const auto plan = Json(TextFile(plan_file, 1 << 20));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 3,
          "three fixed encoder cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh encoder output required");
  Check(audio::ValidateTtsCodecConfig(
      Json(TextFile(directory / "config.json", 65536))));
  constexpr size_t kFileBytes = 682293092;
  Require(runtime::ReadSystemMemory().CanAllocate(3 * kFileBytes),
          "encoder qualification unified RAM unavailable");
  auto file = ReadFile(directory / "model.safetensors", kFileBytes);
  Require(file.size() == kFileBytes && trace::HexDigest(trace::Sha256(file)) ==
                                           "836b7b357f5ea43e889936a3709af68dfe3"
                                           "751881acefe4ecf0dbd30ba571258",
          "codec checkpoint identity differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header;
  std::memcpy(&header, file.data(), sizeof(header));
  Require(header < (1 << 20) && header < file.size() - 8,
          "encoder header invalid");
  audio::TtsWeightLayout weight_layout;
  Check(audio::ParseTtsEncoderWeightLayout(
      Json(std::string(reinterpret_cast<const char*>(file.data() + 8), header)),
      file.size() - header - 8, &weight_layout));
  Device device_weights(weight_layout.bytes);
  for (const auto& tensor : weight_layout.tensors) {
    checkpoint();
    Cuda(cudaMemcpy(static_cast<uint8_t*>(device_weights.value) + tensor.offset,
                    file.data() + 8 + header + tensor.file_offset, tensor.bytes,
                    cudaMemcpyHostToDevice));
  }
  std::vector<uint8_t>().swap(file);
  audio::TtsEncoderDeviceWeights weights;
  Check(audio::BindTtsEncoderDeviceWeights(
      device_weights.value, weight_layout.bytes, weight_layout, &weights));
  size_t advances = 0, boundaries = 0, output_frames = 0;
  constexpr size_t kSamples[] = {17, 55693, 278419};
  for (size_t index = 2; index < 3; ++index) {
    const auto& item = cases->array[index];
    Require(item.GetInt("samples") == static_cast<int64_t>(kSamples[index]),
            "encoder case samples differ");
    const auto input = ReadFile(item.GetString("pcm"), 1440000 * sizeof(float));
    Require(
        input.size() == kSamples[index] * sizeof(float) &&
            trace::HexDigest(trace::Sha256(input)) == item.GetString("sha256"),
        "encoder PCM identity differs");
    std::vector<float> pcm(kSamples[index]);
    std::memcpy(pcm.data(), input.data(), input.size());
    trace_directory = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(trace_directory), "encoder case exists");
    audio::TtsEncoderWorkspace layout;
    Check(audio::PlanTtsEncoderWorkspace(pcm.size(), 1ULL << 30, &layout));
    Device workspace(layout.bytes);
    audio::TtsEncoderRun run;
    auto invalid = run.Advance();
    Require(!invalid.ok() && invalid.device_error == cudaSuccess &&
                run.codes().empty(),
            "encoder pre-Begin access succeeded");
    ++boundaries;
    invalid = run.Begin(weights, pcm, 24000, nullptr, 0);
    Require(!invalid.ok() && invalid.device_error == cudaSuccess,
            "encoder invalid workspace reached CUDA");
    ++boundaries;
    invalid = run.Begin(weights, pcm, 16000, workspace.value, layout.bytes);
    Require(!invalid.ok() && invalid.device_error == cudaSuccess,
            "encoder incorrect sample rate reached CUDA");
    ++boundaries;
    Check(run.Begin(weights, pcm, 24000, workspace.value, layout.bytes));
    Require(run.codes().empty(), "encoder exposed incomplete codes");
    invalid = run.Begin(weights, pcm, 24000, workspace.value, layout.bytes);
    Require(!invalid.ok() && invalid.device_error == cudaSuccess,
            "encoder allowed reinitialization");
    ++boundaries;
    while (!run.done()) {
      checkpoint();
      Require(++advances <= 8192, "encoder advance budget exceeded");
      Require(run.codes().empty(), "encoder exposed partial condition");
      Check(run.Advance());
    }
    const auto codes = run.codes();
    Require(codes.size() == (kSamples[index] + 1919) / 1920,
            "encoder output length differs");
    for (const auto& frame : codes)
      for (const auto code : frame)
        Require(code >= 0 && code < 2048, "encoder invalid audio code");
    Save<audio::TtsCodes>(trace_directory / "codes.i32", codes);
    output_frames += codes.size();
    invalid = run.Advance();
    Require(!invalid.ok() && invalid.device_error == cudaSuccess &&
                run.codes().size() == codes.size(),
            "completed encoder accepted additional work");
    ++boundaries;
    std::ofstream report(trace_directory / "result.json");
    report << "{\"samples\":" << pcm.size() << ",\"frames\":" << codes.size()
           << ",\"workspace_bytes\":" << layout.bytes
           << ",\"transformer_rows\":" << layout.transformer_rows << "}\n";
    report.close();
    Require(report.good(), "encoder case report write failed");
    Cuda(cudaFree(workspace.value));
    workspace.value = nullptr;
  }
  Cuda(cudaFree(device_weights.value));
  device_weights.value = nullptr;
  Cuda(cudaDeviceSynchronize());
  Require(boundaries == 5, "encoder boundary coverage differs");
  std::ofstream report(output / "result.json");
  report << "{\"status\":\"PASS_ENCODER_DIAGNOSTIC_EXECUTION_ONLY\","
         << "\"gpu_model_loads\":1,\"cases\":1,\"encoder_forwards\":1,"
         << "\"output_frames\":" << output_frames
         << ",\"advances\":" << advances
         << ",\"boundary_checks\":" << boundaries
         << ",\"owned_allocations_released\":true,\"seconds\":"
         << std::chrono::duration<double>(Clock::now() - started).count()
         << "}\n";
  report.close();
  Require(report.good(), "encoder final report write failed");
}
}  // namespace

#ifdef Q4T_TTS_TRACE
namespace q4t::audio {
void TraceTtsOperand(const char* name, size_t frame, size_t group,
                     std::span<const float> values) {
  Require(!trace_directory.empty() && values.size() <= 8 * 1024,
          "encoder trace exceeds prefix/suffix budget");
  std::vector<float> host(values.size());
  Cuda(cudaMemcpy(host.data(), values.data(), values.size_bytes(),
                  cudaMemcpyDeviceToHost));
  Save<float>(
      trace_directory / (std::string(name) + "-" + std::to_string(frame) + "-" +
                         std::to_string(group) + ".f32"),
      host);
}
}  // namespace q4t::audio
#endif

int main(int argc, char** argv) {
  try {
    Require(argc == 4, "usage: q4t_tts_encoder_replay ASSETS PLAN OUTPUT");
    RunEncoder(argv[1], argv[2], argv[3]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
