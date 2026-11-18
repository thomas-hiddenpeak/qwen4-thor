// CPU-only replay of retained real separator PCM through production inspection.
#include <cstdio>
#include <fstream>
#include <vector>

#include "q4t/audio/separator_tracks.h"

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
  if (!input) return 2;
  const auto bytes = input.tellg();
  if (bytes <= 0 || bytes % 8 != 0 || bytes > 240000 * 8) return 2;
  std::vector<float> pcm(size_t(bytes) / 4);
  input.seekg(0);
  input.read(reinterpret_cast<char*>(pcm.data()), bytes);
  if (!input) return 2;
  const auto samples = pcm.size() / 2;
  q4t::audio::SeparationTrackDecision result;
  const auto status = q4t::audio::InspectSeparationTracks(
      std::span<const float>(pcm).first(samples),
      std::span<const float>(pcm).subspan(samples), &result);
  if (!status) return 1;
  std::printf(
      "{\"tracks\":%d,\"coherence\":%.12g,"
      "\"coherent_energy_fraction\":%.12g,\"independent_region\":%s}\n",
      result.use_original ? 1 : 2, result.coherence,
      result.coherent_energy_fraction,
      result.independent_region ? "true" : "false");
  return 0;
}
