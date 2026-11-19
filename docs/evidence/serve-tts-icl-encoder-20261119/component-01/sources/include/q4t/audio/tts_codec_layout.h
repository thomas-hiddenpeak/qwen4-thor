// Native 12 Hz codec storage and chunk contracts; no CUDA or file I/O.
#pragma once

#include "q4t/audio/tts_generation.h"
#include "q4t/audio/tts_layout.h"

namespace q4t::audio {

struct TtsCodecConfig {
  static constexpr int kLayers = 8;
  static constexpr int kHidden = 512;
  static constexpr int kHeads = 16;
  static constexpr int kHeadDim = 64;
  static constexpr int kWindow = 72;
  static constexpr size_t kChunkFrames = 300;
  static constexpr size_t kLeftContext = 25;
  static constexpr size_t kMaxInputFrames = kChunkFrames + kLeftContext;
  static constexpr size_t kColumnBytes = 32ULL << 20;
  static constexpr size_t kTransposeWeightBytes = 8ULL << 20;
};

struct TtsCodecConvWeights {
  const float* weight = nullptr;
  const float* bias = nullptr;
  int input = 0, output = 0, kernel = 0;
  int stride = 1, dilation = 1, groups = 1;
  bool transposed = false;
};

struct TtsCodecSnakeWeights {
  const float* alpha = nullptr;
  const float* beta = nullptr;
};

struct TtsCodecLayerWeights {
  const float* input_norm = nullptr;
  const float* post_norm = nullptr;
  const float* q = nullptr;
  const float* k = nullptr;
  const float* v = nullptr;
  const float* o = nullptr;
  const float* gate = nullptr;
  const float* up = nullptr;
  const float* down = nullptr;
  const float* attention_scale = nullptr;
  const float* mlp_scale = nullptr;
};

struct TtsCodecUpsampleWeights {
  TtsCodecConvWeights transpose, depthwise;
  TtsAffineWeights norm, fc1, fc2;
  const float* gamma = nullptr;
};

struct TtsCodecResidualWeights {
  TtsCodecSnakeWeights act1, act2;
  TtsCodecConvWeights conv1, conv2;
};

struct TtsCodecVocoderWeights {
  TtsCodecSnakeWeights act;
  TtsCodecConvWeights transpose;
  std::array<TtsCodecResidualWeights, 3> residuals;
};

struct TtsCodecDeviceWeights {
  std::array<const float*, 16> usage{}, embedding_sum{};
  std::array<const float*, 2> rvq_projection{};
  TtsCodecConvWeights pre_conv;
  TtsAffineWeights input_projection, output_projection;
  const float* final_norm = nullptr;
  std::array<TtsCodecLayerWeights, 8> layers;
  std::array<TtsCodecUpsampleWeights, 2> upsample;
  TtsCodecConvWeights vocoder_input, vocoder_output;
  std::array<TtsCodecVocoderWeights, 4> vocoder;
  TtsCodecSnakeWeights final_act;
};

// The codec checkpoint is already F32; no precision expansion is required.
// Binds the compact decoder payload, after the complete file SHA was verified.
Status BindTtsCodecDeviceWeights(const void* data, size_t bytes,
                                 const TtsWeightLayout& layout,
                                 TtsCodecDeviceWeights* out);

struct TtsCodecLayout {
  size_t frames = 0, padded_frames = 0, samples = 0, bytes = 0;
  size_t buffer_elements = 0;
  std::array<size_t, 3> wave_buffers{};
  size_t codes = 0, rvq_first = 0, rvq_rest = 0;
  size_t hidden = 0, normalized = 0, projected = 0;
  size_t q = 0, k = 0, v = 0, head_q = 0, head_k = 0, head_v = 0;
  size_t rope = 0, scores = 0, head_output = 0, attention = 0;
  size_t gate = 0, up = 0;
  size_t columns = 0, transpose_weights = 0, gemm = 0;
};

// Three reusable wave tensors cover every convolution/residual stage. im2col
// and transposed-convolution weight gathering use fixed-size tiles. They do
// not grow with waveform length or materialize a full convolution matrix.
Status PlanTtsCodecWorkspace(size_t frames, size_t max_bytes,
                             TtsCodecLayout* out);
Status ValidateTtsCodecCodes(std::span<const TtsCodes> codes);

struct TtsCodecChunk {
  size_t input_begin = 0, input_frames = 0;
  size_t discard_samples = 0, output_samples = 0;
};

// Matches the official chunked decoder: up to 25 preceding code frames are
// replayed, then discarded from PCM. Chunk size is explicit; the default is
// 300, while streaming may choose a smaller qualified value.
Status PlanTtsCodecChunks(size_t frames, size_t chunk_frames,
                          size_t max_output_samples,
                          std::vector<TtsCodecChunk>* out);

}  // namespace q4t::audio
