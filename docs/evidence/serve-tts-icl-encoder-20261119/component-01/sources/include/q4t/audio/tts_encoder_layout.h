// Native reference-audio codec encoder contracts. No CUDA or file I/O.
#pragma once

#include "q4t/audio/tts_codec_layout.h"

namespace q4t::audio {

struct TtsEncoderConfig {
  static constexpr size_t kWeightBytes = 224937216;
  static constexpr size_t kMaxSamples = 60 * 24000;
  static constexpr size_t kMaxFrames = 750;
  static constexpr int kHidden = 512, kHeads = 8, kHeadDim = 64;
  static constexpr int kLayers = 8, kWindow = 250;
};

// Certify all 225 encoder tensors, including the unused final 16 acoustic
// quantizers. Runtime uses the first semantic and first 15 acoustic codes.
// The existing full-file SHA and decoder contract remain mandatory.
std::vector<TtsTensorContract> TtsEncoderTensorContracts();
Status ValidateTtsEncoderTensorHeader(const io::Json& header,
                                      uint64_t data_bytes);
Status ParseTtsEncoderWeightLayout(const io::Json& header, size_t data_bytes,
                                   TtsWeightLayout* out);
Status ValidateTtsEncoderWeightLayout(const TtsWeightLayout& layout);

struct TtsEncoderBlockWeights {
  TtsCodecConvWeights first, second, downsample;
};

struct TtsEncoderLayerWeights {
  TtsAffineWeights input_norm, post_norm;
  const float* q = nullptr;
  const float* k = nullptr;
  const float* v = nullptr;
  const float* o = nullptr;
  const float* fc1 = nullptr;
  const float* fc2 = nullptr;
  const float* attention_scale = nullptr;
  const float* mlp_scale = nullptr;
};

struct TtsEncoderDeviceWeights {
  TtsCodecConvWeights input, output, downsample;
  std::array<TtsEncoderBlockWeights, 4> blocks;
  std::array<TtsEncoderLayerWeights, 8> layers;
  std::array<const float*, 2> quantizer_projection{};
  std::array<const float*, 16> usage{}, embedding_sum{}, initialized{};
};

Status BindTtsEncoderDeviceWeights(const void* data, size_t bytes,
                                   const TtsWeightLayout& layout,
                                   TtsEncoderDeviceWeights* out);

// Mimi's causal stride-s convolution pads (kernel-s) on the left, then pads
// only enough on the right to produce ceil(rows/s) outputs. It does not use
// the decoder's stride-one left padding for strided layers.
Status PlanTtsEncoderConvolution(const TtsCodecConvWeights& weights,
                                 size_t rows, size_t* output_rows,
                                 size_t* left_padding);

struct TtsEncoderWorkspace {
  size_t samples = 0, transformer_rows = 0, padded_rows = 0, frames = 0;
  size_t bytes = 0, wave_elements = 0;
  std::array<size_t, 3> wave{};
  size_t hidden = 0, normalized = 0, projected = 0;
  size_t q = 0, k = 0, v = 0, head_q = 0, head_k = 0, head_v = 0;
  size_t rope = 0, scores = 0, head_output = 0, attention = 0, feedforward = 0;
  size_t residual = 0, codebook = 0, distances = 0, codes = 0;
  size_t columns = 0, gemm = 0, finite_flag = 0;
};

// All waveform, residual, projection, attention and quantizer storage counts
// against the caller's unified RAM budget. No unbounded streaming cache.
Status PlanTtsEncoderWorkspace(size_t samples, size_t max_bytes,
                               TtsEncoderWorkspace* out);
Status ValidateTtsEncoderPcm(std::span<const float> samples, int sample_rate);

}  // namespace q4t::audio
