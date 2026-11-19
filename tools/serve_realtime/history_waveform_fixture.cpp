// Development-only capture of the exact qualified PCM -> wire audio pipeline.
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "q4t/server/realtime_audio_export.h"
#include "q4t/server/realtime_noise_input.h"
#include "q4t/server/realtime_pcm.h"

int main(int argc, char** argv) {
  using namespace q4t::server;
  if (argc != 4) return 2;
  const std::string mode = argv[1];
  if (mode != "manual" && mode != "near_stream") return 2;
  std::ifstream file(argv[2], std::ios::binary | std::ios::ate);
  if (!file || file.tellg() <= 0 || file.tellg() > 2 * 1024 * 1024) return 2;
  file.seekg(0);
  const std::vector<uint8_t> raw{std::istreambuf_iterator<char>(file), {}};
  if (file.bad()) return 2;
  std::vector<float> samples;
  const auto wire = mode == "manual" ? RealtimeAudioFormat{}
                                     : RealtimeAudioFormat{
                                           RealtimeAudioEncoding::kMuLaw, 8000};
  if (mode == "manual") {
    RealtimeAudioQueue queue(1);
    RealtimeAudioItem item;
    if (!queue.Append(1, raw) || !queue.Commit(1, &item) ||
        !DecodeRealtimePcm(item, queue.FrontBytes(), 600 * 16000, &samples))
      return 1;
  } else {
    RealtimeNoiseInput input(wire, q4t::audio::NoiseReduction::kNearField, 0);
    const auto sink = [&](auto part) {
      samples.insert(samples.end(), part.begin(), part.end());
      return q4t::Status{};
    };
    for (size_t at = 0; at < raw.size(); at += 4093)
      if (!input.Append(std::span(raw).subspan(
                            at, std::min<size_t>(4093, raw.size() - at)),
                        sink))
        return 1;
    if (!input.Finish(sink)) return 1;
  }
  std::string encoded;
  if (!EncodeRealtimeAudio(samples, wire, 2 * 1024 * 1024, &encoded)) return 1;
  std::ofstream out(argv[3], std::ios::binary | std::ios::trunc);
  out.write(encoded.data(), encoded.size());
  out.close();
  if (!out) return 1;
  std::printf(
      "{\"input_bytes\":%zu,\"processed_samples\":%zu,\"output_bytes\":%zu}\n",
      raw.size(), samples.size(), encoded.size());
  return 0;
}
