#include "q4t/server/aligner_service.h"

#include <bit>
#include <cmath>
#include <limits>

#include "q4t/audio/aligner_layout.h"
#include "q4t/server/aligner_assets.h"

namespace q4t::server {
namespace {
constexpr uint64_t kMiB = 1024 * 1024;
using C = audio::AlignerConfig;
}  // namespace

Status PlanAlignerServiceResources(const AlignerServiceLimits& limits,
                                   AlignerResourcePlan* out) {
  if (!out || !limits.Valid())
    return Status::Fail(
        "invalid alignment duration/context/word/workspace limits");
  const size_t frames = size_t(limits.max_audio_seconds) * 100;
  const size_t budget = uint64_t(limits.workspace_mib) * kMiB;
  audio::AsrEncoderLayout encoder;
  auto status =
      audio::PlanAlignerEncoderWorkspace(frames, frames, budget, &encoder);
  if (!status) return status;
  audio::AlignerDecoderLayout decoder;
  status = audio::PlanAlignerDecoderWorkspace(
      limits.max_context_tokens, size_t(limits.max_words) * 2,
      limits.max_context_tokens, budget - encoder.bytes, &decoder);
  if (!status) return status;
  AlignerResourcePlan plan;
  plan.encoder_bytes = encoder.bytes;
  plan.decoder_bytes = decoder.bytes;
  plan.request_device_bytes = encoder.bytes + decoder.bytes;
  // HTTP binary body, PCM/mel, actual BF16 class logits, word/source metadata.
  // These coexist with resident ASR weights in the same physical DRAM.
  plan.request_host_bytes =
      (32 + 25 + 16) * kMiB +
      uint64_t(limits.max_audio_seconds) * 16000 * sizeof(float) +
      frames * C::kMelBins * sizeof(float) +
      uint64_t(limits.max_words) * 2 * C::kClasses * sizeof(uint16_t);
  plan.load_peak_bytes = 3 * kNativeAlignerFileBytes + 512 * kMiB;
  *out = plan;
  return {};
}

std::string AlignerServiceLimitsJson(const AlignerServiceLimits& limits) {
  return R"({"max_audio_seconds":)" + std::to_string(limits.max_audio_seconds) +
         R"(,"max_context_tokens":)" +
         std::to_string(limits.max_context_tokens) + R"(,"max_words":)" +
         std::to_string(limits.max_words) + R"(,"workspace_mib":)" +
         std::to_string(limits.workspace_mib) + "}";
}
const char* AlignerFailureCode(AsrFailure failure) {
  switch (failure) {
    case AsrFailure::kNone:
      return "none";
    case AsrFailure::kAssets:
      return "alignment_assets_invalid";
    case AsrFailure::kTimeout:
      return "alignment_assets_timeout";
    case AsrFailure::kWorker:
      return "alignment_worker_unavailable";
    case AsrFailure::kResource:
      return "alignment_resource_exhausted";
    case AsrFailure::kDevice:
      return "gpu_unhealthy";
    case AsrFailure::kCompute:
      return "alignment_compute_failed";
  }
  return "alignment_state_invalid";
}
std::string AlignerHealthJson(const AsrHealthSnapshot& state, bool gpu) {
  return SpeechHealthJson(state, gpu, C::kModelId, AlignerFailureCode);
}

Status BuildAlignerPrompt(std::span<const audio::AlignerWord> words,
                          size_t audio_tokens,
                          const AlignerServiceLimits& limits,
                          const AlignerEncode& encode, AlignerPrompt* out,
                          const std::function<bool()>& cancelled) {
  if (!out || !encode || !limits.Valid() || words.empty() ||
      words.size() > size_t(limits.max_words) || !audio_tokens ||
      audio_tokens > size_t(limits.max_context_tokens) - 5)
    return Status::Fail("alignment prompt budget or owner invalid");
  const auto interrupted = [&] { return cancelled && cancelled(); };
  if (interrupted()) return Status::Fail("alignment tokenization cancelled");
  AlignerPrompt prompt;
  prompt.audio_tokens = audio_tokens;
  prompt.tokens.push_back(C::kAudioStart);
  prompt.tokens.insert(prompt.tokens.end(), audio_tokens, C::kAudioPad);
  prompt.tokens.push_back(C::kAudioEnd);
  size_t bytes = 0;
  for (const auto& word : words) {
    if (word.text.empty() || word.text.size() > 64 * 1024 - bytes)
      return Status::Fail("alignment word text budget invalid");
    bytes += word.text.size();
    if (interrupted()) return Status::Fail("alignment tokenization cancelled");
    std::vector<uint32_t> ids;
    if (auto status = encode(word.text, &ids); !status) return status;
    if (ids.empty() || ids.size() + 2 > size_t(limits.max_context_tokens) -
                                            prompt.tokens.size())
      return Status::Fail("alignment context budget exceeded");
    for (const auto id : ids) {
      if (id >= C::kEos)
        return Status::Fail("alignment word includes nonliteral token");
      prompt.tokens.push_back(static_cast<int32_t>(id));
    }
    prompt.tokens.push_back(C::kTimestampToken);
    prompt.tokens.push_back(C::kTimestampToken);
  }
  if (interrupted()) return Status::Fail("alignment tokenization cancelled");
  if (auto s = audio::PlanAlignerSequence(prompt.tokens, audio_tokens,
                                          &prompt.positions);
      !s)
    return s;
  *out = std::move(prompt);
  return {};
}
Status SelectAlignerClasses(std::span<const uint16_t> logits, size_t words,
                            std::vector<uint32_t>* out,
                            const std::function<bool()>& cancelled) {
  if (!out || !words || words > 2048 ||
      logits.size() != words * 2 * C::kClasses)
    return Status::Fail("alignment classification shape differs");
  std::vector<uint32_t> classes;
  classes.reserve(words * 2);
  for (size_t slot = 0; slot < words * 2; ++slot) {
    if (cancelled && cancelled())
      return Status::Fail("alignment classification cancelled");
    float maximum = -std::numeric_limits<float>::infinity();
    uint32_t best = 0;
    for (int id = 0; id < C::kClasses; ++id) {
      const auto value =
          std::bit_cast<float>(uint32_t(logits[slot * C::kClasses + id]) << 16);
      if (!std::isfinite(value))
        return Status::Fail("nonfinite alignment classifier output");
      if (value > maximum) {
        maximum = value;
        best = id;
      }
    }
    classes.push_back(best);
  }
  *out = std::move(classes);
  return {};
}
}  // namespace q4t::server
