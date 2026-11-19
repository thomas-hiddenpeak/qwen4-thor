// Compile this same bounded input driver separately with the frozen old and
// current resampler sources. Compare every default ASR output float bit.
#include "q4t/audio/stream_resample.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <vector>

int main() {
  using namespace q4t::audio;
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    for (const size_t count : {1, 47, 48, 49, 97, 4097}) {
      std::vector<float> input(count);
      for (size_t i = 0; i < count; ++i)
        input[i] = float(int((i * 1597 + 719) % 65536) - 32768) / 32768;
      AsrStreamResampler resampler(rate, count);
      std::array<float, 31> output;
      const auto write = [&](size_t n) {
        return std::fwrite(output.data(), sizeof(float), n, stdout) == n;
      };
      size_t position = 0;
      while (position < count) {
        ResampleProgress progress;
        if (!resampler.Process(std::span(input).subspan(
                                    position, std::min<size_t>(7, count - position)),
                                output, &progress) ||
            (!progress.consumed && !progress.produced) ||
            !write(progress.produced))
          return 1;
        position += progress.consumed;
      }
      do {
        size_t produced;
        if (!resampler.Finish(output, &produced) || !write(produced)) return 2;
      } while (!resampler.Drained());
      if (resampler.OutputSamples() != count * 16000 / rate) return 3;
    }
  }
  return std::fflush(stdout) == 0 ? 0 : 4;
}
