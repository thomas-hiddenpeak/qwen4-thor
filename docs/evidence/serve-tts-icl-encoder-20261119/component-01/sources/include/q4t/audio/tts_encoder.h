// Request-owned native reference-audio encoder; caller holds the GPU lease.
#pragma once

#include "q4t/audio/tts_encoder_layout.h"
#include "q4t/audio/tts_model.h"

namespace q4t::audio {

class TtsEncoderConvRun {
 public:
  TtsComputeResult Begin(const TtsCodecConvWeights& weights, size_t rows,
                         std::span<const float> input, std::span<float> output,
                         std::span<float> columns, std::span<uint8_t> gemm);
  TtsComputeResult Advance(cudaStream_t stream = nullptr);
  bool done() const { return initialized_ && done_ && !failed_; }
  size_t output_rows() const { return output_rows_; }

 private:
  TtsCodecConvWeights weights_;
  const float* input_ = nullptr;
  float* output_ = nullptr;
  float* columns_ = nullptr;
  uint8_t* gemm_ = nullptr;
  size_t rows_ = 0, output_rows_ = 0, left_ = 0, row_ = 0;
  bool initialized_ = false, failed_ = false, done_ = false;
};

// Each Advance performs one bounded convolution tile, transformer substage
// or quantizer. No background thread, persistent cache or implicit retry.
// The fixed non-streaming Mimi eager reference uses full causal attention.
class TtsEncoderRun {
 public:
  TtsComputeResult Begin(const TtsEncoderDeviceWeights& weights,
                         std::span<const float> pcm, int sample_rate,
                         void* workspace, size_t bytes,
                         cudaStream_t stream = nullptr);
  TtsComputeResult Advance(cudaStream_t stream = nullptr);
  bool done() const {
    return initialized_ && !failed_ && stage_ == Stage::kDone;
  }
  std::span<const TtsCodes> codes() const {
    return done() ? std::span<const TtsCodes>(output_)
                  : std::span<const TtsCodes>{};
  }

 private:
  enum class Stage {
    kInput,
    kResidualStart,
    kResidualFirst,
    kResidualElu,
    kResidualSecond,
    kResidualAdd,
    kDownElu,
    kDownsample,
    kOutputElu,
    kOutput,
    kTransformerBegin,
    kAttentionProject,
    kAttentionHead,
    kAttentionFinish,
    kFeedForward,
    kTransformerFinish,
    kFinalDownsample,
    kQuantizerProjection,
    kQuantizer,
    kFinish,
    kDone
  };
  float* At(size_t offset) const;
  float* Current() const;
  float* Other() const;
  void Trace(const char* name, size_t index, const float* data, size_t rows,
             size_t width) const;
  TtsComputeResult Convolution(const TtsCodecConvWeights& weights, Stage next,
                               cudaStream_t stream);
  TtsComputeResult Activate(cudaStream_t stream);
  TtsComputeResult TransformerStart(cudaStream_t stream);
  TtsComputeResult AttentionProject(cudaStream_t stream);
  TtsComputeResult AttentionHead(cudaStream_t stream);
  TtsComputeResult AttentionFinish(cudaStream_t stream);
  TtsComputeResult FeedForward(cudaStream_t stream);
  TtsComputeResult Quantize(cudaStream_t stream);

  const TtsEncoderDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  TtsEncoderWorkspace layout_;
  TtsEncoderConvRun conv_;
  std::vector<TtsCodes> output_;
  size_t rows_ = 0;
  int width_ = 0, wave_ = 0, block_ = 0, layer_ = 0, head_ = 0, group_ = 0;
  Stage stage_ = Stage::kInput;
  bool initialized_ = false, failed_ = false, conv_active_ = false;
};

}  // namespace q4t::audio
