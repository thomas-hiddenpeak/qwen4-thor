#include "q4t/server/speaker_service.h"

#include <algorithm>

#include "q4t/audio/speaker_layout.h"

namespace q4t::server {
Status PlanSpeakerServiceResources(const SpeakerServiceLimits& limits,
                                   SpeakerResourcePlan* out) {
  if (!out || !limits.Valid())
    return Status::Fail("invalid speaker window/step/count/workspace limits");
  // Known references may be 10 seconds even when diarization windows are
  // smaller. They execute sequentially in the same reusable workspace.
  const size_t samples = size_t(std::max(10000, limits.window_ms)) * 16;
  const size_t frames = 1 + samples / audio::SpeakerConfig::kHop;
  audio::SpeakerWorkspaceLayout workspace;
  auto status = audio::PlanSpeakerWorkspace(
      frames, frames, uint64_t(limits.workspace_mib) << 20, &workspace);
  if (!status) return status;
  SpeakerResourcePlan next;
  next.request_device_bytes = workspace.bytes;
  next.resident_weight_bytes = audio::SpeakerConfig::kWeightBytes;
  // Four bounded data-URL source strings + decoded copies/PCM, one feature
  // matrix, window embeddings, dense clustering distances and result metadata.
  // The main input body/PCM/ASR/aligner totals are charged by their own owners.
  next.request_host_bytes =
      (16ULL << 20) + 4 * 160000 * sizeof(float) + samples * sizeof(float) +
      frames * 80 * sizeof(float) +
      size_t(limits.max_windows) * (192 * sizeof(float) + 128) +
      size_t(limits.max_windows) * limits.max_windows * sizeof(float) +
      size_t(limits.max_speakers) * 192 * sizeof(double);
  // Direct payload I/O stages bytes and uploaded data in unified RAM. The
  // margin also covers worker buffers and bounded parsing metadata.
  next.load_peak_bytes = 2 * audio::SpeakerConfig::kFileBytes + (64ULL << 20);
  *out = next;
  return {};
}
std::string SpeakerServiceLimitsJson(const SpeakerServiceLimits& limits) {
  return R"({"window_ms":)" + std::to_string(limits.window_ms) +
         R"(,"step_ms":)" + std::to_string(limits.step_ms) +
         R"(,"max_windows":)" + std::to_string(limits.max_windows) +
         R"(,"max_speakers":)" + std::to_string(limits.max_speakers) +
         R"(,"workspace_mib":)" + std::to_string(limits.workspace_mib) + "}";
}
const char* SpeakerFailureCode(AsrFailure failure) {
  switch (failure) {
    case AsrFailure::kNone:
      return "none";
    case AsrFailure::kAssets:
      return "speaker_assets_invalid";
    case AsrFailure::kTimeout:
      return "speaker_assets_timeout";
    case AsrFailure::kWorker:
      return "speaker_worker_unavailable";
    case AsrFailure::kResource:
      return "speaker_resource_exhausted";
    case AsrFailure::kDevice:
      return "gpu_unhealthy";
    case AsrFailure::kCompute:
      return "speaker_compute_failed";
  }
  return "speaker_state_invalid";
}
std::string SpeakerHealthJson(const AsrHealthSnapshot& state,
                              bool gpu_healthy) {
  return SpeechHealthJson(state, gpu_healthy, audio::SpeakerConfig::kModelId,
                          SpeakerFailureCode);
}
}  // namespace q4t::server
