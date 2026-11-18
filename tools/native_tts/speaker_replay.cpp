// Native Qwen Base 2048 speaker qualification; no synthesis acceptance.
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

#include "q4t/audio/tts_speaker_model.h"
#include "q4t/audio/tts_speaker_features.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using namespace q4t;
using C = audio::TtsSpeakerConfig;

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
void SaveDevice(const fs::path& path, const float* values, size_t count) {
  std::vector<float> host(count);
  Cuda(cudaMemcpy(host.data(), values, count * sizeof(float),
                  cudaMemcpyDeviceToHost));
  Save<float>(path, host);
}
int Replay(const fs::path& assets, const fs::path& plan_path,
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
  const auto deadline = started + std::chrono::seconds(180);
  const auto checkpoint = [&] {
    Require(Clock::now() < deadline, "speaker replay time budget exceeded");
  };
  const auto plan = Json(TextFile(plan_path, 1 << 20));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 4, "four frozen cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh output required");
  Require(runtime::ReadSystemMemory().CanAllocate(2ULL << 30),
          "insufficient observed unified RAM for speaker qualification");
  const auto receipt = Json(TextFile(assets / "result.json", 16384));
  Require(receipt.GetString("status") ==
                  "FULL_SOURCE_SHA_VERIFIED_SPEAKER_PREFIX_RETAINED" &&
              receipt.GetString("full_source_sha256") ==
                  "38fc7fc51c5e776e840414b6fd443962e941"
                  "1b9654888fd7913e4da643cb857c",
          "requires independently complete source verification");
  auto file = ReadFile(assets / "source-prefix.bin", 24061040);
  Require(file.size() == 24061040 && trace::HexDigest(trace::Sha256(file)) ==
                                         "6f0c979914635259a77c56b31ab9d3cda4f"
                                         "aacc1f466ba5e26f06ce6fc8b1661",
          "verified speaker prefix differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header_bytes = 0;
  std::memcpy(&header_bytes, file.data(), sizeof(header_bytes));
  Require(header_bytes == 58856, "Base header differs");
  audio::TtsWeightLayout tensor_layout;
  Check(audio::ParseTtsSpeakerWeightLayout(
      Json(std::string(reinterpret_cast<const char*>(file.data() + 8),
                       header_bytes)),
      C::kBaseDataBytes, &tensor_layout));
  Device device_weights(C::kDeviceBytes);
  for (const auto& tensor : tensor_layout.tensors) {
    checkpoint();
    const size_t elements = tensor.bytes / 2;
    constexpr size_t chunk_limit = 1U << 20;
    std::vector<uint16_t> compact(std::min(elements, chunk_limit));
    std::vector<float> expanded(compact.size());
    for (size_t at = 0; at < elements; at += chunk_limit) {
      checkpoint();
      const size_t count = std::min(chunk_limit, elements - at);
      const auto source = std::span(compact).first(count);
      const auto target = std::span(expanded).first(count);
      std::memcpy(source.data(),
                  file.data() + 8 + header_bytes + tensor.file_offset + at * 2,
                  source.size_bytes());
      Check(audio::ExpandTtsWeightChunk(source, target));
      Cuda(cudaMemcpy(static_cast<uint8_t*>(device_weights.value) +
                          tensor.offset * 2 + at * sizeof(float),
                      target.data(), target.size_bytes(),
                      cudaMemcpyHostToDevice));
    }
  }
  std::vector<uint8_t>().swap(file);
  audio::TtsSpeakerDeviceWeights weights;
  Check(audio::BindTtsSpeakerDeviceWeights(
      device_weights.value, C::kDeviceBytes, tensor_layout, &weights));
  size_t maximum_workspace = 0;
  int case_index = 0, boundary_checks = 0, lifecycle_checks = 0;
  for (const auto& input : cases->array) {
    checkpoint();
    const auto pcm_bytes = ReadFile(input.GetString("pcm"), 480000 * 4);
    Require(pcm_bytes.size() % 4 == 0 &&
                trace::HexDigest(trace::Sha256(pcm_bytes)) ==
                    input.GetString("pcm_sha256"),
            "PCM identity differs");
    std::vector<float> pcm(pcm_bytes.size() / 4);
    std::memcpy(pcm.data(), pcm_bytes.data(), pcm_bytes.size());
    audio::TtsSpeakerFeatures features;
    Check(audio::ComputeTtsSpeakerFeatures(pcm, 24000, {}, &features));
    Require(static_cast<int64_t>(features.frames) == input.GetInt("frames"),
            "feature frames differ");
    const auto directory = output / ("case-" + std::to_string(case_index++));
    Require(fs::create_directory(directory), "case output failed");
    Save<float>(directory / "mel.f32", features.mel);
    audio::TtsSpeakerWorkspace layout;
    Check(audio::PlanTtsSpeakerWorkspace(features.frames, 5625, 300ULL << 20,
                                         &layout));
    maximum_workspace = std::max(maximum_workspace, layout.bytes);
    Device workspace(layout.bytes);
    std::array<float, 2048> result;
    result.fill(123);
    if (case_index == 1) {
      audio::TtsSpeakerRun invalid;
      const auto rejected = [&](const audio::TtsComputeResult& value) {
        Require(!value.ok() && value.device_error == cudaSuccess &&
                    invalid.completed_stages() == 0 && result[0] == 123,
                "invalid input mutated stage/output or caused device failure");
        ++boundary_checks;
      };
      rejected(invalid.NextStage());
      rejected(invalid.Finish(result));
      rejected(invalid.Begin({}, features.mel, features.frames, workspace.value,
                             layout.bytes));
      rejected(invalid.Begin(weights, features.mel, 4, workspace.value,
                             layout.bytes));
      rejected(invalid.Begin(weights, {}, features.frames, workspace.value,
                             layout.bytes));
      rejected(invalid.Begin(weights, features.mel, features.frames,
                             workspace.value, layout.bytes - 1));
      auto bad = features.mel;
      bad.front() = std::numeric_limits<float>::quiet_NaN();
      rejected(invalid.Begin(weights, bad, features.frames, workspace.value,
                             layout.bytes));
      rejected(invalid.Begin(weights, features.mel, features.frames,
                             static_cast<uint8_t*>(workspace.value) + 1,
                             layout.bytes - 1));
    }
    audio::TtsSpeakerRun run;
    Check(run.Begin(weights, features.mel, features.frames, workspace.value,
                    layout.bytes));
    Require(!run.Begin(weights, features.mel, features.frames, workspace.value,
                       layout.bytes)
                    .ok() &&
                run.completed_stages() == 0,
            "duplicate Begin accepted");
    Require(!run.Finish(result).ok() && result[0] == 123,
            "unfinished embedding published");
    lifecycle_checks += 2;
    constexpr std::array<int, 7> checkpoints{1, 14, 27, 40, 41, 46, 47};
    size_t macro = 0;
    for (int stage = 0; stage < audio::TtsSpeakerRun::kStages; ++stage) {
      checkpoint();
      Check(run.NextStage());
      Require(run.completed_stages() == stage + 1, "stage progress differs");
      if (stage + 1 != checkpoints[macro]) continue;
      const size_t offset = macro < 4    ? layout.hidden
                            : macro == 4 ? layout.aggregate
                            : macro == 5 ? layout.pool
                                         : layout.output;
      const size_t count = macro < 4    ? 512 * features.frames
                           : macro == 4 ? 1536 * features.frames
                           : macro == 5 ? 3072
                                        : 2048;
      SaveDevice(directory / ("stage-" + std::to_string(macro) + ".f32"),
                 reinterpret_cast<const float*>(
                     static_cast<uint8_t*>(workspace.value) + offset),
                 count);
      ++macro;
    }
    Require(macro == 7 && !run.NextStage().ok() &&
                run.completed_stages() == audio::TtsSpeakerRun::kStages,
            "stage count or completion bound differs");
    Check(run.Finish(result));
    Save<float>(directory / "embedding.f32", result);
    const auto prior = result;
    Require(!run.Finish(result).ok() && result == prior,
            "repeated Finish changed output");
    lifecycle_checks += 2;
  }
  checkpoint();
  Require(boundary_checks == 8 && lifecycle_checks == 16, "case count differs");
  const double seconds =
      std::chrono::duration<double>(Clock::now() - started).count();
  std::ofstream result(output / "result.json");
  result << "{\"status\":\"PASS_NATIVE_COMPONENT\",\"cases\":4,"
            "\"boundary_checks\":8,\"lifecycle_checks\":16,"
            "\"gpu_model_loads\":1,\"max_explicit_workspace_bytes\":"
         << maximum_workspace << ",\"weight_bytes\":" << C::kDeviceBytes
         << ",\"seconds\":" << seconds << "}\n";
  result.close();
  Require(result.good(), "result write failed");
  return 0;
}
}  // namespace
int main(int argc, char** argv) {
  try {
    Require(argc == 4, "usage: replay ASSETS PLAN OUTPUT");
    return Replay(argv[1], argv[2], argv[3]);
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
