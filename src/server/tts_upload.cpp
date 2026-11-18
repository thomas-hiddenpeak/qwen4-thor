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
audio::TtsComputeResult CopyWeights(const TtsHostWeightFile& file,
                                    const audio::TtsWeightLayout& layout,
                                    std::span<uint8_t> device, bool expand,
                                    const std::function<bool()>& cancelled,
                                    cudaStream_t stream) {
  // Layout extents and the destination were validated by the graph binder.
  std::vector<uint16_t> source(1 << 20);
  std::vector<float> expanded(source.size());
  const auto payload = file.WeightBytes();
  for (const auto& tensor : layout.tensors) {
    for (size_t offset = 0; offset < tensor.bytes;) {
      if (cancelled && cancelled())
        return Invalid("TTS weight upload cancelled");
      const size_t bytes = std::min(tensor.bytes - offset, source.size() * 2);
      const auto* raw = payload.data() + tensor.file_offset + offset;
      const void* data = raw;
      size_t output_bytes = bytes;
      size_t destination = tensor.offset + offset;
      if (expand) {
        std::memcpy(source.data(), raw, bytes);
        const size_t count = bytes / 2;
        auto status = audio::ExpandTtsWeightChunk({source.data(), count},
                                                  {expanded.data(), count});
        if (!status) return {status, cudaSuccess};
        data = expanded.data();
        output_bytes *= 2;
        destination *= 2;
      }
      auto copied =
          cudaMemcpyAsync(device.data() + destination, data, output_bytes,
                          cudaMemcpyHostToDevice, stream);
      const auto drained = cudaStreamSynchronize(stream);
      if (copied == cudaSuccess) copied = drained;
      if (copied != cudaSuccess)
        return {Status::Fail("TTS weight copy failed"), copied};
      offset += bytes;
    }
  }
  return {};
}
}  // namespace

audio::TtsComputeResult UploadTtsWeights(const TtsHostAssets& assets,
                                         std::span<uint8_t> talker_device,
                                         std::span<uint8_t> codec_device,
                                         TtsBoundWeights* out,
                                         const std::function<bool()>& cancelled,
                                         cudaStream_t stream) {
  static_assert(std::endian::native == std::endian::little);
  const auto variant = assets.variant;
  const bool base = variant == audio::TtsVariant::kBase17B;
  if (!base && variant != audio::TtsVariant::kCustomVoice17B)
    return Invalid("TTS upload variant differs");
  if (!out || talker_device.size() != kNativeTtsWeightBytes ||
      codec_device.size() != audio::TtsConfig::kCodecDecoderBytes ||
      assets.talker.bytes.size() !=
          (base ? kNativeTtsBaseFileBytes : kNativeTtsFileBytes) ||
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
  auto copied = CopyWeights(assets.talker, assets.talker.layout, talker_device,
                            true, cancelled, stream);
  if (copied.ok())
    copied = CopyWeights(assets.codec, assets.codec.layout, codec_device, false,
                         cancelled, stream);
  if (!copied.ok()) return copied;
  if (cancelled && cancelled()) return Invalid("TTS weight upload cancelled");
  *out = bound;
  return {};
}
audio::TtsComputeResult UploadTtsSpeakerWeights(
    const TtsHostAssets& assets, std::span<uint8_t> speaker_device,
    audio::TtsSpeakerDeviceWeights* out, const std::function<bool()>& cancelled,
    cudaStream_t stream) {
  if (!out || assets.variant != audio::TtsVariant::kBase17B ||
      assets.talker.bytes.size() != kNativeTtsBaseFileBytes ||
      assets.talker.WeightBytes().size() != assets.speaker_layout.file_bytes)
    return Invalid("TTS speaker upload assets differ");
  audio::TtsSpeakerDeviceWeights next;
  auto status = audio::BindTtsSpeakerDeviceWeights(
      speaker_device.data(), speaker_device.size(), assets.speaker_layout,
      &next);
  if (!status) return {status, cudaSuccess};
  auto copied = CopyWeights(assets.talker, assets.speaker_layout,
                            speaker_device, true, cancelled, stream);
  if (!copied.ok()) return copied;
  if (cancelled && cancelled()) return Invalid("TTS speaker upload cancelled");
  *out = next;
  return {};
}
}  // namespace q4t::server
