// Targeted neural codec qualification, not a speech-quality or speed test.
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

#include "q4t/audio/tts_codec.h"
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

void RunCodec(const fs::path& directory, const fs::path& plan_file,
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
            "codec component deadline exceeded");
  };
  const auto plan = Json(TextFile(plan_file, 65536));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 3,
          "three fixed codec cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh codec output required");
  Check(audio::ValidateTtsCodecConfig(
      Json(TextFile(directory / "config.json", 65536))));
  constexpr size_t kFileBytes = 682293092;
  Require(
      runtime::ReadSystemMemory().CanAllocate(3 * kFileBytes + (1ULL << 30)),
      "codec qualification unified RAM unavailable");
  auto file = ReadFile(directory / "model.safetensors", kFileBytes);
  Require(file.size() == kFileBytes && trace::HexDigest(trace::Sha256(file)) ==
                                           "836b7b357f5ea43e889936a3709af68dfe3"
                                           "751881acefe4ecf0dbd30ba571258",
          "codec checkpoint identity differs");
  static_assert(std::endian::native == std::endian::little);
  uint64_t header;
  std::memcpy(&header, file.data(), sizeof(header));
  Require(header < (1 << 20) && header < file.size() - 8,
          "codec header invalid");
  audio::TtsWeightLayout weight_layout;
  Check(audio::ParseTtsCodecWeightLayout(
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
  audio::TtsCodecDeviceWeights weights;
  Check(audio::BindTtsCodecDeviceWeights(
      device_weights.value, weight_layout.bytes, weight_layout, &weights));
  size_t advances = 0, forwards = 0, input_frames = 0, output_samples = 0;
  constexpr size_t kFrames[] = {2, 75, 37};
  constexpr size_t kChunkFrames[] = {300, 300, 16};
  for (size_t index = 0; index < 3; ++index) {
    const auto& item = cases->array[index];
    const auto* frames = item.GetArray("codes");
    Require(frames && frames->array.size() == kFrames[index] &&
                item.GetInt("chunk_frames") ==
                    static_cast<int64_t>(kChunkFrames[index]),
            "codec case budget differs");
    std::vector<audio::TtsCodes> codes;
    for (const auto& frame : frames->array) {
      Require(frame.IsArray() && frame.array.size() == 16,
              "codec frame shape differs");
      audio::TtsCodes value;
      for (size_t group = 0; group < 16; ++group) {
        const auto code = frame.array[group].AsInt(-1);
        Require(code >= 0 && code < 2048, "codec code out of range");
        value[group] = static_cast<int32_t>(code);
      }
      codes.push_back(value);
    }
    const auto case_output = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(case_output), "codec case exists");
    std::vector<audio::TtsCodecChunk> chunks;
    Check(audio::PlanTtsCodecChunks(codes.size(), kChunkFrames[index],
                                    codes.size() * 1920, &chunks));
    std::vector<float> pcm;
    pcm.reserve(codes.size() * 1920);
    std::ostringstream chunk_records;
    for (size_t i = 0; i < chunks.size(); ++i) {
      checkpoint();
      const auto& chunk = chunks[i];
      trace_directory = case_output / ("chunk-" + std::to_string(i));
      Require(fs::create_directory(trace_directory),
              "codec trace directory exists");
      audio::TtsCodecLayout layout;
      Check(audio::PlanTtsCodecWorkspace(chunk.input_frames, 1ULL << 30,
                                         &layout));
      Device workspace(layout.bytes);
      audio::TtsCodecRun run;
      const auto input = std::span<const audio::TtsCodes>(codes).subspan(
          chunk.input_begin, chunk.input_frames);
      auto invalid = run.Begin(weights, input, nullptr, 0);
      Require(!invalid.ok() && invalid.device_error == cudaSuccess &&
                  run.pcm().empty(),
              "codec bad workspace changed failure classification");
      auto invalid_codes =
          std::vector<audio::TtsCodes>(input.begin(), input.end());
      invalid_codes.back().back() = 2150;
      invalid =
          run.Begin(weights, invalid_codes, workspace.value, layout.bytes);
      Require(!invalid.ok() && invalid.device_error == cudaSuccess,
              "codec control token reached GPU computation");
      Check(run.Begin(weights, input, workspace.value, layout.bytes));
      Require(run.pcm().empty(), "codec exposed incomplete PCM");
      ++forwards;
      input_frames += input.size();
      while (!run.done()) {
        checkpoint();
        Require(++advances <= 4096, "codec advance budget exhausted");
        Check(run.Advance());
      }
      const auto device_pcm = run.pcm();
      Require(device_pcm.size() == chunk.input_frames * 1920,
              "codec generated incorrect sample count");
      std::vector<float> complete(device_pcm.size());
      Cuda(cudaMemcpy(complete.data(), device_pcm.data(),
                      device_pcm.size_bytes(), cudaMemcpyDeviceToHost));
      for (const auto value : complete)
        Require(std::isfinite(value) && std::abs(value) <= 1,
                "codec PCM invalid");
      Require(chunk.discard_samples + chunk.output_samples == complete.size(),
              "codec trim and output differ");
      pcm.insert(pcm.end(), complete.begin() + chunk.discard_samples,
                 complete.end());
      invalid = run.Advance();
      Require(!invalid.ok() && invalid.device_error == cudaSuccess,
              "completed codec accepted more work");
      if (i) chunk_records << ',';
      chunk_records << "{\"input_frames\":" << chunk.input_frames
                    << ",\"input_begin\":" << chunk.input_begin
                    << ",\"discard_samples\":" << chunk.discard_samples
                    << ",\"output_samples\":" << chunk.output_samples
                    << ",\"workspace_bytes\":" << layout.bytes << '}';
    }
    Require(pcm.size() == codes.size() * 1920,
            "codec joined sample count differs");
    Save<float>(case_output / "pcm.f32", pcm);
    output_samples += pcm.size();
    std::ofstream report(case_output / "result.json");
    report << "{\"samples\":" << pcm.size() << ",\"chunks\":["
           << chunk_records.str() << "]}\n";
    report.close();
    Require(report.good(), "codec report write failed");
  }
  Require(forwards == 5 && input_frames == 155 && output_samples == 218880,
          "codec aggregate execution budget differs");
  Cuda(cudaDeviceSynchronize());
  std::ofstream report(output / "result.json");
  report << "{\"status\":\"PASS_CODEC_NATIVE_EXECUTION_ONLY\","
         << "\"gpu_model_loads\":1,\"cases\":3,\"decoder_forwards\":5,"
         << "\"input_frames\":" << input_frames
         << ",\"output_samples\":" << output_samples
         << ",\"advances\":" << advances << ",\"seconds\":"
         << std::chrono::duration<double>(Clock::now() - started).count()
         << "}\n";
  report.close();
  Require(report.good(), "codec final report write failed");
}
}  // namespace

#ifdef Q4T_TTS_TRACE
namespace q4t::audio {
void TraceTtsOperand(const char* name, size_t frame, size_t group,
                     std::span<const float> values) {
  Require(!trace_directory.empty() && values.size() <= 8 * 1536,
          "codec trace exceeds prefix/suffix budget");
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
    Require(argc == 4, "usage: q4t_tts_codec_replay ASSETS PLAN OUTPUT");
    RunCodec(argv[1], argv[2], argv[3]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
