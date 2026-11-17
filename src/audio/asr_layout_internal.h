// Common checked storage layout; each caller supplies its pinned profile.
#pragma once

#include <algorithm>
#include <limits>
#include <span>
#include <utility>

#include "q4t/audio/asr_layout.h"

namespace q4t::audio::detail {
inline Status ParseSpeechWeightLayout(
    const io::Json& header, size_t data_bytes,
    std::span<const AsrTensorContract> contracts, AsrWeightLayout* out) {
  if (!out) return Status::Fail("ASR weight layout output is null");
  AsrWeightLayout result;
  result.bytes = data_bytes;
  for (const auto& contract : contracts) {
    const auto* offsets = header.Find(contract.name)->GetArray("data_offsets");
    result.tensors.push_back({contract.name,
                              static_cast<size_t>(offsets->array[0].AsInt(-1)),
                              contract.bytes});
  }
  *out = std::move(result);
  return {};
}

inline Status ValidateSpeechWeightLayout(
    const AsrWeightLayout& layout, std::span<const AsrTensorContract> contracts,
    size_t expected_bytes) {
  if (layout.bytes != expected_bytes ||
      layout.tensors.size() != contracts.size())
    return Status::Fail("ASR weight layout size differs");
  std::vector<std::pair<size_t, size_t>> extents;
  for (size_t i = 0; i < contracts.size(); ++i) {
    const auto& t = layout.tensors[i];
    if (t.name != contracts[i].name || t.bytes != contracts[i].bytes ||
        t.offset % 2 || t.offset > layout.bytes ||
        t.bytes > layout.bytes - t.offset)
      return Status::Fail("ASR weight layout tensor differs");
    extents.emplace_back(t.offset, t.offset + t.bytes);
  }
  std::sort(extents.begin(), extents.end());
  size_t end = 0;
  for (const auto& extent : extents) {
    if (extent.first != end)
      return Status::Fail("ASR weight layout overlap or hole");
    end = extent.second;
  }
  return end == layout.bytes ? Status{}
                             : Status::Fail("ASR weight layout trailing bytes");
}

template <typename C>
Status PlanSpeechEncoderWorkspace(size_t frames, size_t max_frames,
                                  size_t max_bytes, AsrEncoderLayout* out) {
  // Keep every CUDA index within signed int, even for callers with an
  // unbounded size_t limit. A request budget never silently trims audio.
  constexpr size_t kIndexLimit =
      std::numeric_limits<int>::max() / C::kEncoderIntermediate;
  if (!out || !frames || frames > max_frames || frames > kIndexLimit)
    return Status::Fail("ASR mel frame or index budget exceeded");
  AsrEncoderLayout w;
  w.frames = static_cast<int>(frames);
  w.chunks = (w.frames + C::kMelChunk - 1) / C::kMelChunk;
  w.padded_frames = std::min(w.frames, C::kMelChunk);
  w.padded_tokens = (w.padded_frames + 7) / 8;
  w.tokens = static_cast<int>(AsrEncodedFrames(frames));
  w.attention_window =
      w.padded_tokens * (C::kAttentionWindowMel / C::kMelChunk);
  bool overflow = false;
  const auto carve = [&](size_t count, size_t width) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    if (start < w.bytes ||
        count > (std::numeric_limits<size_t>::max() - start) / width) {
      overflow = true;
      return size_t{0};
    }
    w.bytes = start + count * width;
    return start;
  };
  const auto bf16 = [&](size_t count) { return carve(count, 2); };
  const size_t conv1_rows = 64 * ((w.padded_frames + 1) / 2);
  const size_t conv2_rows = 32 * ((w.padded_frames + 3) / 4);
  const size_t t = w.tokens, hidden = t * C::kEncoderHidden;
  w.features = carve(frames * C::kMelBins, sizeof(float));
  w.columns = bf16(std::max(conv1_rows * 9, conv2_rows * C::kConvChannels * 9));
  w.convolution_a = bf16(conv1_rows * C::kConvChannels);
  w.convolution_b = bf16(conv2_rows * C::kConvChannels);
  w.conv_flat = bf16(w.padded_tokens * C::kConvChannels * C::kConvFrequency);
  w.chunk_hidden = bf16(w.padded_tokens * C::kEncoderHidden);
  w.hidden = bf16(hidden);
  w.normalized = bf16(hidden);
  w.q = bf16(hidden);
  w.k = bf16(hidden);
  w.v = bf16(hidden);
  w.attention = bf16(hidden);
  w.projected = bf16(hidden);
  w.intermediate = bf16(t * C::kEncoderIntermediate);
  w.output = bf16(t * C::kHidden);
  w.linear_fp32 = carve(std::max({conv1_rows * C::kConvChannels,
                                  t * C::kEncoderIntermediate, t * C::kHidden}),
                        sizeof(float));
  w.gemm = carve(AsrEncoderLayout::kGemmBytes, 1);
  if (overflow || w.bytes > max_bytes)
    return Status::Fail("ASR encoder workspace budget exceeded");
  *out = w;
  return {};
}

}  // namespace q4t::audio::detail
