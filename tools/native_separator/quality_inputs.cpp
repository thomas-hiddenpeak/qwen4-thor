// Prepare a retained failure's acoustic ablations with production CPU adapters.
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <vector>

#include "q4t/audio/separator_resample.h"

namespace {
std::vector<float> Read(const char* name) {
  std::ifstream file(name, std::ios::binary | std::ios::ate);
  if (!file) throw std::runtime_error("input missing");
  const auto bytes = file.tellg();
  if (bytes <= 0 || bytes % 4 || bytes > 480000 * 4)
    throw std::runtime_error("input size invalid");
  std::vector<float> result(size_t(bytes) / 4);
  file.seekg(0);
  file.read(reinterpret_cast<char*>(result.data()), bytes);
  if (!file) throw std::runtime_error("input read failed");
  for (float value : result)
    if (!std::isfinite(value)) throw std::runtime_error("nonfinite input");
  return result;
}
void Write(const std::filesystem::path& name, const std::vector<float>& value) {
  if (std::filesystem::exists(name)) throw std::runtime_error("output exists");
  std::ofstream file(name, std::ios::binary);
  file.write(reinterpret_cast<const char*>(value.data()), value.size() * 4);
  if (!file) throw std::runtime_error("output write failed");
}
double Correlation(std::span<const float> a, std::span<const float> b) {
  double aa = 0, bb = 0, ab = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    aa += double(a[i]) * a[i];
    bb += double(b[i]) * b[i];
    ab += double(a[i]) * b[i];
  }
  return aa > 0 && bb > 0 ? std::abs(ab) / std::sqrt(aa * bb) : 0;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 4) return 2;
  try {
    const auto original = Read(argv[1]), sources = Read(argv[2]);
    const std::filesystem::path out = argv[3];
    if (original.size() != 64000 || sources.size() != 64000 ||
        !std::filesystem::is_directory(out))
      throw std::runtime_error("fixed 4-second acoustic diagnostic required");
    std::vector<float> down, limited, separated;
    using namespace q4t::audio;
    if (!DownsampleSeparation(original, 480000, &down) ||
        !RestoreSeparation(down, original.size(), 480000, &limited) ||
        !NormalizeSeparation(limited, 480000, &limited))
      throw std::runtime_error("production bandwidth adapter failed");
    const auto a = std::span<const float>(sources).first(down.size());
    const auto b = std::span<const float>(sources).subspan(down.size());
    const double ca = Correlation(down, a), cb = Correlation(down, b);
    const size_t selected = ca > cb ? 0 : 1;
    // Match by actual waveform, without consulting transcript words or labels.
    if (std::max(ca, cb) < .95 || std::min(ca, cb) > .2)
      throw std::runtime_error("source match ambiguous");
    if (!RestoreSeparation(selected ? b : a, original.size(), 480000,
                           &separated) ||
        !NormalizeSeparation(separated, 480000, &separated))
      throw std::runtime_error("production separated audio adapter failed");
    Write(out / "original.f32", original);
    Write(out / "limited.f32", limited);
    Write(out / "separated.f32", separated);
    std::printf(
        "{\"selected_track\":%zu,\"absolute_waveform_cosines\":[%.12g,"
        "%.12g],\"samples\":%zu}\n",
        selected, ca, cb, original.size());
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "%s\n", error.what());
    return 1;
  }
}
