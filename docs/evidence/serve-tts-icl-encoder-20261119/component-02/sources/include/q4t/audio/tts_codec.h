// Request-owned native F32 neural codec; caller holds the shared GPU lock.
#pragma once

#include "q4t/audio/tts_codec_layout.h"
#include "q4t/audio/tts_model.h"

namespace q4t::audio {

// One Advance computes a bounded output tile (or one kernel position/tile
// for transposed convolution), then drains its stream. Callers can enforce
// cancellation and time limits without waiting for the entire waveform.
class TtsCodecConvRun {
 public:
  TtsComputeResult Begin(const TtsCodecConvWeights& weights, size_t rows,
                         std::span<const float> input, std::span<float> output,
                         std::span<float> columns,
                         std::span<float> transpose_weights,
                         std::span<uint8_t> gemm,
                         cudaStream_t stream = nullptr);
  TtsComputeResult Advance(cudaStream_t stream = nullptr);
  bool done() const { return done_; }
  size_t output_rows() const { return output_rows_; }

 private:
  TtsCodecConvWeights weights_;
  const float* input_ = nullptr;
  float* output_ = nullptr;
  float* columns_ = nullptr;
  float* transpose_weights_ = nullptr;
  uint8_t* gemm_ = nullptr;
  size_t rows_ = 0, output_rows_ = 0, row_ = 0;
  int kernel_ = 0;
  bool initialized_ = false, done_ = false, failed_ = false;
};

class TtsCodecRun {
 public:
  TtsComputeResult Begin(const TtsCodecDeviceWeights& weights,
                         std::span<const TtsCodes> codes, void* workspace,
                         size_t bytes, cudaStream_t stream = nullptr);
  TtsComputeResult Advance(cudaStream_t stream = nullptr);
  bool done() const { return initialized_ && stage_ == Stage::kDone; }
  std::span<const float> pcm() const;

 private:
  enum class Stage {
    kQuantize,
    kProjectFirst,
    kProjectRest,
    kPreConv,
    kTransformerBegin,
    kTransformerLayer,
    kTransformerFinish,
    kUpsampleTranspose,
    kConvNextDepthwise,
    kConvNextNorm,
    kConvNextFc1,
    kConvNextFc2,
    kConvNextResidual,
    kVocoderInput,
    kVocoderSnake,
    kVocoderTranspose,
    kResidualSnake1,
    kResidualConv1,
    kResidualSnake2,
    kResidualConv2,
    kResidualAdd,
    kFinalSnake,
    kFinalConv,
    kClamp,
    kDone
  };
  float* At(size_t offset) const;
  float* Current() const;
  float* Other() const;
  TtsComputeResult Convolution(const TtsCodecConvWeights& weights, Stage next,
                               cudaStream_t stream);
  TtsComputeResult SaveResidual(cudaStream_t stream);
  TtsComputeResult Activate(TtsCodecSnakeWeights weights, cudaStream_t stream);
  void Trace(const char* name, size_t index) const;

  const TtsCodecDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  TtsCodecLayout layout_;
  TtsCodecConvRun conv_;
  size_t rows_ = 0;
  int width_ = 0, wave_ = 0, layer_ = 0, upsample_ = 0;
  int vocoder_ = 0, residual_ = 0;
  Stage stage_ = Stage::kQuantize;
  bool initialized_ = false, failed_ = false, conv_active_ = false;
};

}  // namespace q4t::audio
