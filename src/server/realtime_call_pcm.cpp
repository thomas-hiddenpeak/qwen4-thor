#include "realtime_call_pcm.h"

#include <algorithm>
#include <array>
#include <cmath>

#include "q4t/server/realtime_audio_export.h"
#include "q4t/server/realtime_pcm.h"

namespace q4t::server {
namespace {
using Encoding = RealtimeAudioEncoding;
bool Supported(RealtimeAudioFormat format) {
  return (format.encoding == Encoding::kPcm16 && format.rate == 24000) ||
         ((format.encoding == Encoding::kMuLaw ||
           format.encoding == Encoding::kALaw) &&
          format.rate == 8000);
}
}  // namespace

RealtimeCallPcm::RealtimeCallPcm(RealtimeAudioFormat from,
                                 RealtimeAudioFormat to)
    : from_(from),
      to_(to),
      resampler_(from.rate, 3600ULL * from.rate, to.rate, false) {}

bool RealtimeCallPcm::Convert(std::string_view input, std::string* output,
                              bool finish) {
  const size_t width = from_.encoding == Encoding::kPcm16 ? 2 : 1;
  if (!output || !Supported(from_) || !Supported(to_) || finished_ ||
      !resampler_.Valid() || input.size() > 65536 || input.size() % width)
    return false;
  output->clear();
  finished_ = finish;
  if (from_ == to_) {
    output->assign(input);
    return true;
  }
  std::array<float, 512> decoded, converted;
  const auto write = [&](size_t count) {
    for (float value : std::span(converted).first(count)) {
      const int16_t sample = static_cast<int16_t>(std::clamp<long>(
          std::lround(std::clamp(value, -1.f, 1.f) * 32768.), -32768, 32767));
      if (to_.encoding == Encoding::kPcm16) {
        output->push_back(static_cast<char>(sample & 0xff));
        output->push_back(static_cast<char>(uint16_t(sample) >> 8));
      } else {
        output->push_back(
            EncodeRealtimeG711(sample, to_.encoding == Encoding::kMuLaw));
      }
    }
  };
  while (!input.empty()) {
    const size_t count = std::min(decoded.size(), input.size() / width);
    for (size_t i = 0; i < count; ++i) {
      decoded[i] = DecodeRealtimeSample(from_.encoding, input[i * width],
                                        width == 2 ? input[i * width + 1] : 0);
    }
    size_t pos = 0;
    while (pos < count) {
      audio::ResampleProgress progress;
      if (!resampler_.Process(std::span(decoded).first(count).subspan(pos),
                              converted, &progress) ||
          (!progress.consumed && !progress.produced))
        return false;
      pos += progress.consumed;
      write(progress.produced);
    }
    input.remove_prefix(count * width);
  }
  while (finish && !resampler_.Drained()) {
    size_t produced = 0;
    if (!resampler_.Finish(converted, &produced)) return false;
    write(produced);
  }
  return true;
}
}  // namespace q4t::server
