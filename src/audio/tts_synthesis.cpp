#include "q4t/audio/tts_synthesis.h"

#include <algorithm>
#include <limits>

namespace q4t::audio {
namespace {
TtsComputeResult Invalid(const char* message) {
  return {Status::Fail(message), cudaSuccess};
}
bool Disjoint(std::span<uint8_t> a, std::span<uint8_t> b) {
  const auto first = reinterpret_cast<uintptr_t>(a.data());
  const auto second = reinterpret_cast<uintptr_t>(b.data());
  return first && second && first % 256 == 0 && second % 256 == 0 &&
         a.size() <= std::numeric_limits<uintptr_t>::max() - first &&
         b.size() <= std::numeric_limits<uintptr_t>::max() - second &&
         (first + a.size() <= second || second + b.size() <= first);
}
}  // namespace

TtsComputeResult TtsSynthesisRun::Record(TtsComputeResult result) {
  if (!result.ok()) stage_ = Stage::kFailed;
  return result;
}
TtsComputeResult TtsSynthesisRun::Begin(
    const TtsDeviceWeights& weights, const TtsCodecDeviceWeights& codec_weights,
    const TtsPrompt& prompt, size_t max_frames, size_t chunk_frames,
    const TtsSampling& sampling, const TtsSampling& predictor_sampling,
    uint64_t seed, std::span<uint8_t> generation_workspace,
    std::span<uint8_t> codec_workspace, cudaStream_t stream,
    bool defer_decode_until_eos) {
  if (stage_ != Stage::kInactive || !chunk_frames ||
      chunk_frames > TtsCodecConfig::kChunkFrames ||
      !Disjoint(generation_workspace, codec_workspace))
    return Invalid("TTS streaming synthesis buffers or chunk size invalid");
  TtsCodecLayout layout;
  const auto status = PlanTtsCodecWorkspace(
      std::min(max_frames, chunk_frames + TtsCodecConfig::kLeftContext),
      codec_workspace.size(), &layout);
  if (!status) return {status, cudaSuccess};
  const auto started = generation_.Begin(
      weights, prompt, max_frames, sampling, predictor_sampling, seed,
      generation_workspace.data(), generation_workspace.size(), stream);
  if (!started.ok()) return Record(started);
  codec_weights_ = &codec_weights;
  codec_workspace_ = codec_workspace;
  chunk_frames_ = chunk_frames;
  defer_decode_until_eos_ = defer_decode_until_eos;
  stage_ = Stage::kGenerate;
  return {};
}

TtsComputeResult TtsSynthesisRun::Advance(cudaStream_t stream) {
  if (stage_ != Stage::kGenerate && stage_ != Stage::kDecode)
    return Invalid(
        "TTS synthesis cannot advance before output acknowledgement");
  if (stage_ == Stage::kDecode) {
    const auto result = decoder_.Advance(stream);
    if (!result.ok()) return Record(result);
    if (decoder_.done()) {
      if (decoder_.pcm().size() !=
          discard_samples_ + pending_frames_ * TtsConfig::kSamplesPerFrame)
        return Record(Invalid("TTS synthesis codec sample count differs"));
      stage_ = Stage::kOutput;
    }
    return {};
  }
  // Natural EOS is required. Reaching the frame budget is an explicit failed
  // termination even if previous chunks have already been sent to the client.
  if (generation_.at_limit() && !generation_.ended())
    return Record(Invalid("TTS output frame budget exhausted before EOS"));
  const size_t pending = generation_.frames() - emitted_frames_;
  if ((!defer_decode_until_eos_ && pending >= chunk_frames_) ||
      (generation_.ended() && pending)) {
    pending_frames_ = std::min(pending, chunk_frames_);
    const size_t history =
        std::min(emitted_frames_, TtsCodecConfig::kLeftContext);
    discard_samples_ = history * TtsConfig::kSamplesPerFrame;
    decoder_ = TtsCodecRun{};
    const auto result = decoder_.Begin(
        *codec_weights_,
        generation_.codes().subspan(emitted_frames_ - history,
                                    history + pending_frames_),
        codec_workspace_.data(), codec_workspace_.size(), stream);
    if (!result.ok()) return Record(result);
    stage_ = Stage::kDecode;
    return {};
  }
  if (generation_.ended()) {
    stage_ = Stage::kDone;
    return {};
  }
  return Record(generation_.Advance(stream));
}

std::span<const float> TtsSynthesisRun::OutputDevicePcm() const {
  if (!output_ready()) return {};
  return decoder_.pcm().subspan(discard_samples_,
                                pending_frames_ * TtsConfig::kSamplesPerFrame);
}
Status TtsSynthesisRun::AcknowledgeOutput() {
  if (!output_ready()) return Status::Fail("TTS synthesis has no pending PCM");
  emitted_frames_ += pending_frames_;
  pending_frames_ = 0;
  discard_samples_ = 0;
  stage_ = Stage::kGenerate;
  return {};
}
void TtsSynthesisRun::Cancel() {
  stage_ = Stage::kFailed;
  pending_frames_ = 0;
}
}  // namespace q4t::audio
