// Development-only waveform capture using the actual native CPU pipeline.
#include <bit>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "q4t/server/realtime_noise_input.h"
#include "q4t/server/realtime_pcm.h"

int main(int argc, char** argv) {
  using namespace q4t::server;
  if (argc != 5) return 2;
  const std::string mode = argv[1], format = argv[2];
  if ((mode != "off" && mode != "near_field" && mode != "far_field") ||
      (format != "pcm" && format != "mulaw"))
    return 2;
  std::ifstream file(argv[3], std::ios::binary | std::ios::ate);
  if (!file || file.tellg() <= 0 || file.tellg() > 2 * 1024 * 1024) return 2;
  file.seekg(0);
  const std::vector<uint8_t> raw{std::istreambuf_iterator<char>(file), {}};
  if (file.bad()) return 2;
  const RealtimeAudioFormat wire =
      format == "pcm"
          ? RealtimeAudioFormat{}
          : RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 8000};
  const size_t width = format == "pcm" ? 2 : 1;
  if (raw.size() % width) return 2;
  std::vector<float> output;
  if (mode == "off") {
    RealtimeAudioQueue queue(1, wire);
    RealtimeAudioItem item;
    if (!queue.Append(1, raw) || !queue.Commit(1, &item) ||
        !DecodeRealtimePcm(item, queue.FrontBytes(), 600 * 16000, &output))
      return 1;
  } else {
    const auto setting = mode == "near_field"
                             ? q4t::audio::NoiseReduction::kNearField
                             : q4t::audio::NoiseReduction::kFarField;
    RealtimeNoiseInput input(wire, setting, 0);
    const auto sink = [&](auto samples) {
      output.insert(output.end(), samples.begin(), samples.end());
      return q4t::Status{};
    };
    for (size_t at = 0; at < raw.size(); at += 4093) {
      if (!input.Append(std::span(raw).subspan(
                            at, std::min<size_t>(4093, raw.size() - at)),
                        sink))
        return 1;
    }
    if (!input.Finish(sink)) return 1;
  }
  if (output.size() != raw.size() / width * 16000 / wire.rate) return 1;
  std::ofstream destination(argv[4], std::ios::binary | std::ios::trunc);
  for (float value : output) {
    const auto bits = std::bit_cast<uint32_t>(value);
    for (int b = 0; b < 4; ++b) destination.put(char(bits >> (b * 8)));
  }
  destination.close();
  if (!destination) return 1;
  std::printf("{\"input_bytes\":%zu,\"output_samples\":%zu}\n", raw.size(),
              output.size());
  return 0;
}
