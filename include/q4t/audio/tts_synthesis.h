// Incremental generation/codec composition with explicit output backpressure.
#pragma once

#include "q4t/audio/tts_codec.h"

namespace q4t::audio {
class TtsSynthesisRun {
 public:
  // All buffers and bound weights stay owned by the caller through completion.
  // Hold the shared GPU lock for Begin/Advance and copying OutputDevicePcm.
  // Each GPU operation drains its stream; socket writes need no GPU lock.
  TtsComputeResult Begin(const TtsDeviceWeights& weights,
                         const TtsCodecDeviceWeights& codec_weights,
                         const TtsPrompt& prompt, size_t max_frames,
                         size_t chunk_frames, const TtsSampling& sampling,
                         const TtsSampling& predictor_sampling, uint64_t seed,
                         std::span<uint8_t> generation_workspace,
                         std::span<uint8_t> codec_workspace,
                         cudaStream_t stream = nullptr);
  TtsComputeResult Advance(cudaStream_t stream = nullptr);
  // The returned span is device memory. It stays valid until acknowledgement
  // followed by Advance; generation cannot overwrite an unconsumed chunk.
  std::span<const float> OutputDevicePcm() const;
  Status AcknowledgeOutput();
  void Cancel();  // No GPU call; prior Begin/Advance must have returned.
  bool output_ready() const { return stage_ == Stage::kOutput; }
  bool done() const { return stage_ == Stage::kDone; }
  bool failed() const { return stage_ == Stage::kFailed; }
  bool ended() const { return generation_.ended(); }
  bool at_limit() const { return generation_.at_limit(); }
  size_t frames() const { return generation_.frames(); }
  size_t emitted_frames() const { return emitted_frames_; }
  std::span<const TtsCodes> codes() const { return generation_.codes(); }

 private:
  enum class Stage { kInactive, kGenerate, kDecode, kOutput, kDone, kFailed };
  TtsComputeResult Record(TtsComputeResult result);
  TtsGenerationRun generation_;
  TtsCodecRun decoder_;
  const TtsCodecDeviceWeights* codec_weights_ = nullptr;
  std::span<uint8_t> codec_workspace_;
  size_t chunk_frames_ = 0, emitted_frames_ = 0, pending_frames_ = 0;
  size_t discard_samples_ = 0;
  Stage stage_ = Stage::kInactive;
};
}  // namespace q4t::audio
