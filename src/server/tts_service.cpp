#include "q4t/server/tts_service.h"

#include <algorithm>

#include "q4t/audio/tts_codec_layout.h"
#include "q4t/server/tts_assets.h"

namespace q4t::server {
namespace {
constexpr uint64_t kMiB = 1ULL << 20;
constexpr uint64_t kHostMargin = 512 * kMiB;
}  // namespace

bool TtsServiceLimits::Valid() const {
  return max_text_tokens >= 4 && max_text_tokens <= 32768 && max_frames >= 2 &&
         max_frames <= 4096 && max_prefill_tokens >= 8 &&
         max_prefill_tokens <= 32768 - max_frames && workspace_mib >= 128 &&
         workspace_mib <= 16384 && max_output_mib >= 1 && max_output_mib <= 128;
}

Status PlanTtsServiceResources(const TtsServiceLimits& limits,
                               size_t text_tokens, size_t prefill_tokens,
                               size_t max_frames, TtsResourcePlan* out) {
  if (!out || !limits.Valid() || text_tokens > size_t(limits.max_text_tokens) ||
      prefill_tokens > size_t(limits.max_prefill_tokens) ||
      max_frames > size_t(limits.max_frames))
    return Status::Fail("TTS request exceeds service capacity");
  const size_t budget = uint64_t(limits.workspace_mib) * kMiB;
  audio::TtsGenerationLayout generation;
  auto status = audio::PlanTtsGenerationWorkspace(
      audio::TtsVariant::kCustomVoice17B, text_tokens, prefill_tokens,
      max_frames, budget, &generation);
  if (!status) return status;
  audio::TtsCodecLayout codec;
  // Bound any qualified chunk size up to the official 300 + 25 history.
  status = audio::PlanTtsCodecWorkspace(
      std::min(max_frames, audio::TtsCodecConfig::kMaxInputFrames),
      budget - generation.bytes, &codec);
  if (!status) return status;
  TtsResourcePlan next;
  next.generation_bytes = generation.bytes;
  next.codec_bytes = codec.bytes;
  next.request_device_bytes = generation.bytes + codec.bytes;
  next.resident_weight_bytes =
      kNativeTtsWeightBytes + audio::TtsConfig::kCodecDecoderBytes;
  next.resident_host_bytes = kHostMargin;
  next.source_staging_bytes = kNativeTtsFileBytes + kNativeTtsCodecFileBytes;
  next.possible_file_page_bytes = next.source_staging_bytes;
  // BF16 chunk and F32 expansion only, never another full F32 host payload.
  next.expansion_scratch_bytes = 6 * kMiB;
  next.load_peak_bytes = next.resident_weight_bytes + next.resident_host_bytes +
                         next.source_staging_bytes +
                         next.possible_file_page_bytes +
                         next.expansion_scratch_bytes;
  // The complete-PCM path retains native and up-to-4x slowed PCM, encoded
  // bytes, JSON/base64/transport copies and bounded token/code/tempo scratch.
  // PCM16 encoder input is independent of the compressed output byte cap.
  // Progressive synthesis can later release earlier samples; do not assume
  // that reduction before the actual ownership path is implemented.
  const uint64_t samples = max_frames * audio::TtsConfig::kSamplesPerFrame;
  const uint64_t output = uint64_t(limits.max_output_mib) * kMiB;
  next.encoder_address_space_limit = 2ULL << 30;
  // Conservatively charge the codec child's whole virtual limit as a RAM
  // ceiling. It is not measured RSS or an assertion that all pages are used.
  next.request_host_bytes =
      samples * (5 * sizeof(float) + 4 * sizeof(int16_t)) + 6 * output +
      16 * kMiB + next.encoder_address_space_limit;
  *out = next;
  return {};
}

std::string TtsServiceLimitsJson(const TtsServiceLimits& limits) {
  return R"({"max_text_tokens":)" + std::to_string(limits.max_text_tokens) +
         R"(,"max_prefill_tokens":)" +
         std::to_string(limits.max_prefill_tokens) + R"(,"max_frames":)" +
         std::to_string(limits.max_frames) + R"(,"workspace_mib":)" +
         std::to_string(limits.workspace_mib) + R"(,"max_output_mib":)" +
         std::to_string(limits.max_output_mib) + "}";
}
const char* TtsFailureCode(AsrFailure failure) {
  switch (failure) {
    case AsrFailure::kNone:
      return "none";
    case AsrFailure::kAssets:
      return "tts_assets_invalid";
    case AsrFailure::kTimeout:
      return "tts_assets_timeout";
    case AsrFailure::kWorker:
      return "tts_worker_unavailable";
    case AsrFailure::kResource:
      return "tts_resource_exhausted";
    case AsrFailure::kDevice:
      return "gpu_unhealthy";
    case AsrFailure::kCompute:
      return "tts_compute_failed";
  }
  return "tts_state_invalid";
}
std::string TtsHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy) {
  return SpeechHealthJson(state, gpu_healthy, kNativeTtsModel, TtsFailureCode);
}
}  // namespace q4t::server
