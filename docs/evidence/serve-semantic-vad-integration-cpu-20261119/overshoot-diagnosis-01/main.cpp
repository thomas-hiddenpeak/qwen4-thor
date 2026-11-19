#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdio>
#include <vector>

#include "q4t/audio/stream_resample.h"
#include "q4t/server/realtime_vad.h"

int main() {
  using namespace q4t::server;
  std::vector<float> input(1025), pcm(1025);
  for (size_t i = 0; i < input.size(); ++i)
    input[i] = float((i * 31) % 30000) / 32768;
  q4t::audio::AsrStreamResampler resample(24000, input.size());
  q4t::audio::ResampleProgress progress;
  if (!resample.Process(input, pcm, &progress)) return 2;
  size_t final = 0;
  if (!resample.Finish(std::span(pcm).subspan(progress.produced), &final))
    return 2;
  pcm.resize(progress.produced + final);
  float peak = 0;
  std::vector<uint8_t> bytes(pcm.size() * 4);
  for (size_t i = 0; i < pcm.size(); ++i) {
    peak = std::max(peak, std::abs(pcm[i]));
    const auto bits = std::bit_cast<uint32_t>(pcm[i]);
    for (size_t b = 0; b < 4; ++b) bytes[i * 4 + b] = uint8_t(bits >> (8 * b));
  }
  RealtimeAudioQueue queue(1, {RealtimeAudioEncoding::kFloat32, 16000});
  if (!queue.Append(1, bytes)) return 2;
  size_t frames = 0;
  RealtimeVad vad(queue.Format(), 0, {}, [&](auto, auto*, float* p) {
    ++frames;
    *p = 1;
    return q4t::Status{};
  });
  for (int i = 0; i < 4; ++i) {
    RealtimeVadProgress step;
    const auto result = vad.Advance(queue, &step);
    if (!result) {
      std::printf("peak=%.9g frames=%zu samples=%zu error=%s\n", peak, frames,
                  pcm.size(), result.message().c_str());
      return peak > 1 && result.message() ==
                              "stream resampling input/state invalid"
                 ? 0
                 : 1;
    }
  }
  std::printf("No rejection: peak=%.9g frames=%zu samples=%zu\n", peak,
              frames, pcm.size());
  return 1;
}
