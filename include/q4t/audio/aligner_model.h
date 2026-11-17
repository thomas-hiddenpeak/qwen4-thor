// Native causal classification at every supplied timestamp token.
#pragma once

#include <vector>

#include "q4t/audio/asr_model.h"

namespace q4t::audio {

Status BindAlignerDeviceWeights(const void* data, size_t bytes,
                                const AsrWeightLayout& layout,
                                AlignerDeviceWeights* out);

class AlignerDecoderRun {
 public:
  // All time slots are collected from the verified token sequence. Audio and
  // text must already use the pinned template; no hidden EOS or generation.
  AsrComputeResult Begin(const AlignerDeviceWeights& weights,
                         std::span<const int32_t> tokens, size_t audio_start,
                         std::span<const uint16_t> device_audio,
                         void* workspace, size_t bytes,
                         cudaStream_t stream = nullptr);
  AsrComputeResult NextLayer(cudaStream_t stream = nullptr);
  // Row-major [slots, 5000] BF16 host logits. Classification/postprocessing
  // remains explicit; the original predictions can be retained for diagnosis.
  AsrComputeResult Finish(std::span<uint16_t> logits,
                          cudaStream_t stream = nullptr);
  int completed_layers() const { return dense_.completed_layers(); }
  int slots() const { return layout_.slots; }

 private:
  const AlignerDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  AlignerDecoderLayout layout_;
  model::EmbeddingRun dense_;
  bool started_ = false;
  bool failed_ = false;
  bool finished_ = false;
};

}  // namespace q4t::audio
