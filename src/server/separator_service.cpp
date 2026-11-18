#include "q4t/server/separator_service.h"

#include "q4t/audio/separator_layout.h"

namespace q4t::server {
Status PlanSeparatorServiceResources(const SeparatorServiceLimits& limits,
                                     SeparatorResourcePlan* out) {
  if (!out || !limits.Valid())
    return Status::Fail("invalid separator duration/workspace limits");
  const size_t samples = size_t(limits.max_audio_seconds) * 8000;
  audio::SeparatorWorkspaceLayout workspace;
  auto status = audio::PlanSeparatorWorkspace(
      samples, samples, uint64_t(limits.workspace_mib) << 20, &workspace);
  if (!status) return status;
  SeparatorResourcePlan next;
  next.request_device_bytes = workspace.bytes;
  next.resident_weight_bytes = audio::SeparatorConfig::kWeightBytes;
  // Downsampled input, two separated 8k tracks, two restored 16k tracks and
  // temporary atomic output copies fit in eight 16k buffers. Metadata/output
  // staging is bounded separately; original input remains owned by ASR.
  next.request_host_bytes = (8ULL << 20) + samples * 2 * 8 * sizeof(float);
  // At most four 10-second reference clips need the same 8k bandwidth as the
  // separated signals. Their original validated PCM stays with the speaker
  // owner; account for four restored copies plus one sequential 8k temporary.
  next.request_host_bytes += (4 * 160000 + 80000) * sizeof(float);
  // Direct payload I/O: host staging and device upload share physical DRAM.
  next.load_peak_bytes = 2 * audio::SeparatorConfig::kFileBytes + (64ULL << 20);
  *out = next;
  return {};
}
std::string SeparatorServiceLimitsJson(const SeparatorServiceLimits& limits) {
  return R"({"max_audio_seconds":)" + std::to_string(limits.max_audio_seconds) +
         R"(,"workspace_mib":)" + std::to_string(limits.workspace_mib) + "}";
}
const char* SeparatorFailureCode(AsrFailure failure) {
  switch (failure) {
    case AsrFailure::kNone:
      return "none";
    case AsrFailure::kAssets:
      return "separator_assets_invalid";
    case AsrFailure::kTimeout:
      return "separator_assets_timeout";
    case AsrFailure::kWorker:
      return "separator_worker_unavailable";
    case AsrFailure::kResource:
      return "separator_resource_exhausted";
    case AsrFailure::kDevice:
      return "gpu_unhealthy";
    case AsrFailure::kCompute:
      return "separator_compute_failed";
  }
  return "separator_state_invalid";
}
std::string SeparatorHealthJson(const AsrHealthSnapshot& state,
                                bool gpu_healthy) {
  return SpeechHealthJson(state, gpu_healthy, audio::SeparatorConfig::kModelId,
                          SeparatorFailureCode);
}
}  // namespace q4t::server
