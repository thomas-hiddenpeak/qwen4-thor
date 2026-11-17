// Bounded CPU-only replay of the fixed real Silero model and PCM fixtures.
#include <algorithm>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "q4t/audio/vad.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using namespace q4t;
void Require(bool condition, const std::string& why) {
  if (!condition) throw std::runtime_error(why);
}
std::vector<uint8_t> Read(const fs::path& path, size_t limit) {
  const auto size = fs::file_size(path);
  Require(size <= limit, "replay file budget");
  std::vector<uint8_t> value(size);
  std::ifstream file(path, std::ios::binary);
  file.read(reinterpret_cast<char*>(value.data()), value.size());
  Require(file.good(), "replay file read failed");
  return value;
}
void Save(const fs::path& path, std::span<const float> values) {
  std::ofstream file(path, std::ios::binary);
  file.write(reinterpret_cast<const char*>(values.data()), values.size_bytes());
  file.close();
  Require(file.good(), "replay output write failed");
}
int Run(const fs::path& model_path, const fs::path& plan_path,
        const fs::path& out) {
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(120);
  const auto cancelled = [&] {
    return std::chrono::steady_clock::now() >= deadline;
  };
  const auto checkpoint = Read(model_path, audio::kVadFileBytes);
  Require(
      trace::HexDigest(trace::Sha256(checkpoint)) == audio::kVadWeightSha256,
      "actual VAD checkpoint identity differs");
  audio::SileroVad model;
  const auto loaded = model.Load(checkpoint, cancelled);
  Require(loaded.ok(), loaded.message());
  const auto bytes = Read(plan_path, 65536);
  io::Json plan;
  Require(io::ParseJson(std::string(bytes.begin(), bytes.end()), &plan, false,
                        {.reject_duplicate_keys = true})
              .ok(),
          "replay plan");
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 4, "requires four frozen CPU cases");
  Require(!fs::exists(out) && fs::create_directory(out),
          "fresh output required");
  for (size_t index = 0; index < cases->array.size(); ++index) {
    const auto& item = cases->array[index];
    const auto raw = Read(item.GetString("pcm"), 30 * 16000 * sizeof(float));
    Require(!raw.empty() && raw.size() % sizeof(float) == 0 &&
                trace::HexDigest(trace::Sha256(raw)) ==
                    item.GetString("pcm_sha256"),
            "replay PCM identity differs");
    std::vector<float> pcm(raw.size() / sizeof(float));
    std::memcpy(pcm.data(), raw.data(), raw.size());
    audio::VadState state;
    std::vector<float> probabilities;
    for (size_t at = 0; at < pcm.size(); at += 512) {
      std::array<float, 512> frame{};
      std::copy_n(pcm.data() + at, std::min(size_t{512}, pcm.size() - at),
                  frame.begin());
      float p;
      const auto status = model.Step(frame, &state, &p, cancelled);
      Require(status.ok(), status.message());
      probabilities.push_back(p);
    }
    const auto prefix = out / ("case-" + std::to_string(index));
    Save(prefix.string() + "-probabilities.f32", probabilities);
    Save(prefix.string() + "-hidden.f32", state.hidden);
    Save(prefix.string() + "-cell.f32", state.cell);
    std::cout << index << ' ' << pcm.size() << ' ' << probabilities.size()
              << '\n';
  }
  return 0;
}
}  // namespace
int main(int argc, char** argv) {
  try {
    Require(argc == 4, "usage: vad_replay model plan output");
    return Run(argv[1], argv[2], argv[3]);
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
