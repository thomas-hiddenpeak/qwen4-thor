#pragma once

#include "q4t/audio/tts_codec.h"

namespace q4t::audio::codec_detail {

TtsComputeResult Affine(const float* input, TtsAffineWeights weights,
                        float* output, int rows, int columns, int inner,
                        bool gelu, void* gemm, cudaStream_t stream);
TtsComputeResult LayerNorm(float* values, TtsAffineWeights weights, int rows,
                           int width, cudaStream_t stream);
TtsComputeResult TransformerBegin(const TtsCodecDeviceWeights& weights,
                                  const TtsCodecLayout& layout,
                                  uint8_t* workspace, const float* input,
                                  cudaStream_t stream);
TtsComputeResult TransformerLayer(const TtsCodecDeviceWeights& weights,
                                  const TtsCodecLayout& layout,
                                  uint8_t* workspace, int layer,
                                  cudaStream_t stream);
TtsComputeResult TransformerFinish(const TtsCodecDeviceWeights& weights,
                                   const TtsCodecLayout& layout,
                                   uint8_t* workspace, float* output,
                                   cudaStream_t stream);

}  // namespace q4t::audio::codec_detail
