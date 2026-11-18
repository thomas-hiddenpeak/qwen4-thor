#include "q4t/server/tts_upload.h"

#include <algorithm>
#include <bit>
#include <cstring>
#include <limits>

namespace q4t::server {
namespace {
audio::TtsComputeResult Invalid(const char* message) {
  return {Status::Fail(message), cudaSuccess};
}
}  // namespace

audio::TtsComputeResult UploadTtsWeights(const TtsHostAssets& assets,
                                         std::span<uint8_t> talker_device,
                                         std::span<uint8_t> codec_device,
                                         TtsBoundWeights* out,
                                         const std::function<bool()>& cancelled,
                                         cudaStream_t stream) {
  static_assert(std::endian::native == std::endian::little);
  constexpr auto variant = audio::TtsVariant::kCustomVoice17B;
  if (!out || talker_device.size() != kNativeTtsWeightBytes ||
      codec_device.size() != audio::TtsConfig::kCodecDecoderBytes ||
      assets.talker.bytes.size() != kNativeTtsFileBytes ||
      assets.codec.bytes.size() != kNativeTtsCodecFileBytes ||
      assets.talker.WeightBytes().size() != assets.talker.layout.file_bytes ||
      assets.codec.WeightBytes().size() != assets.codec.layout.file_bytes)
    return Invalid("TTS upload asset or buffer size differs");
  TtsBoundWeights bound;
  auto status =
      audio::BindTtsDeviceWeights(talker_device.data(), talker_device.size(),
                                  assets.talker.layout, variant, &bound.talker);
  if (status)
    status = audio::BindTtsCodecDeviceWeights(
        codec_device.data(), codec_device.size(), assets.codec.layout,
        &bound.codec);
  if (!status) return {status, cudaSuccess};
  const auto first = reinterpret_cast<uintptr_t>(talker_device.data());
  const auto second = reinterpret_cast<uintptr_t>(codec_device.data());
  // Bind checked overflow/alignment; additionally disallow shared storage.
  if (first < second + codec_device.size() &&
      second < first + talker_device.size())
    return Invalid("TTS upload buffers overlap");
  if (cancelled && cancelled()) return Invalid("TTS weight upload cancelled");
  std::vector<uint16_t> source(1 << 20);
  std::vector<float> expanded(source.size());
  const auto copy = [&](uint8_t* target, const void* data, size_t bytes) {
    auto copied =
        cudaMemcpyAsync(target, data, bytes, cudaMemcpyHostToDevice, stream);
    const auto drained = cudaStreamSynchronize(stream);
    if (copied == cudaSuccess) copied = drained;
    return audio::TtsComputeResult{copied == cudaSuccess
                                       ? Status{}
                                       : Status::Fail("TTS weight copy failed"),
                                   copied};
  };
  for (bool codec : {false, true}) {
    const auto& file = codec ? assets.codec : assets.talker;
    const auto payload = file.WeightBytes();
    auto device = codec ? codec_device : talker_device;
    for (const auto& tensor : file.layout.tensors) {
      for (size_t offset = 0; offset < tensor.bytes;) {
        if (cancelled && cancelled())
          return Invalid("TTS weight upload cancelled");
        const size_t bytes = std::min(tensor.bytes - offset, source.size() * 2);
        const auto* raw = payload.data() + tensor.file_offset + offset;
        audio::TtsComputeResult result;
        if (codec) {
          result = copy(device.data() + tensor.offset + offset, raw, bytes);
        } else {
          // Avoid aliasing uint8 storage as live uint16 objects.
          std::memcpy(source.data(), raw, bytes);
          const size_t count = bytes / 2;
          status = audio::ExpandTtsWeightChunk({source.data(), count},
                                               {expanded.data(), count});
          if (!status) return {status, cudaSuccess};
          result = copy(device.data() + (tensor.offset + offset) * 2,
                        expanded.data(), count * sizeof(float));
        }
        if (!result.ok()) return result;
        offset += bytes;
      }
    }
  }
  if (cancelled && cancelled()) return Invalid("TTS weight upload cancelled");
  *out = bound;
  return {};
}
}  // namespace q4t::server
