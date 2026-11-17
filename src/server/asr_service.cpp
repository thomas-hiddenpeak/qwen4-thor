#include "q4t/server/asr_service.h"

#include "q4t/audio/asr_layout.h"
#include "q4t/server/asr_assets.h"

namespace q4t::server {
namespace {
constexpr uint64_t kMiB = 1024 * 1024;
constexpr uint64_t kHostMargin = 512 * kMiB;
}  // namespace
Status PlanAsrServiceResources(const AsrServiceLimits& limits,
                               AsrResourcePlan* out) {
  if (!out || !limits.Valid())
    return Status::Fail("invalid ASR duration/context/output/workspace limits");
  const size_t frames = size_t(limits.max_audio_seconds) * 100;
  // At least the trained empty-context template and full output budget fit.
  // Per-request context/prompt accounting must still reject larger prompts.
  if (audio::AsrEncodedFrames(frames) + 15 + limits.max_output_tokens >
      size_t(limits.max_context_tokens))
    return Status::Fail("ASR audio and output budgets exceed context capacity");
  const size_t budget = uint64_t(limits.workspace_mib) * kMiB;
  audio::AsrEncoderLayout encoder;
  auto status =
      audio::PlanAsrEncoderWorkspace(frames, frames, budget, &encoder);
  if (!status) return status;
  audio::AsrDecoderLayout decoder;
  status = audio::PlanAsrDecoderWorkspace(limits.max_context_tokens,
                                          limits.max_context_tokens,
                                          budget - encoder.bytes, &decoder);
  if (!status) return status;
  AsrResourcePlan plan;
  plan.encoder_bytes = encoder.bytes;
  plan.decoder_bytes = decoder.bytes;
  // Encoder output stays in its owned workspace through decoder completion.
  plan.request_device_bytes = encoder.bytes + decoder.bytes;
  // Encoded source, mono PCM, mel, host logits/output and bounded metadata.
  // Both the bounded multipart body and its owned binary file may coexist.
  plan.request_host_bytes =
      (32 + 25) * kMiB +
      uint64_t(limits.max_audio_seconds) * 16000 * sizeof(float) +
      frames * audio::AsrConfig::kMelBins * sizeof(float) + 16 * kMiB;
  plan.load_peak_bytes = 3 * kNativeAsrFileBytes + kHostMargin;
  *out = plan;
  return {};
}

std::string AsrServiceLimitsJson(const AsrServiceLimits& limits) {
  return R"({"max_audio_seconds":)" + std::to_string(limits.max_audio_seconds) +
         R"(,"max_context_tokens":)" +
         std::to_string(limits.max_context_tokens) +
         R"(,"max_output_tokens":)" + std::to_string(limits.max_output_tokens) +
         R"(,"workspace_mib":)" + std::to_string(limits.workspace_mib) + "}";
}

void AsrHealth::Configure(bool configured) {
  const std::lock_guard lock(mutex_);
  // Only called during server initialization before any request can run.
  state_ = {};
  state_.configured = configured;
}
AsrLoadAdmission AsrHealth::BeginLoad(bool weights, Clock::time_point now) {
  const std::lock_guard lock(mutex_);
  if (!state_.configured) return AsrLoadAdmission::kDisabled;
  if (state_.failure == AsrFailure::kDevice) return AsrLoadAdmission::kDevice;
  if (state_.loading) return AsrLoadAdmission::kLoading;
  // Valid metadata remains usable while a weights-only load is in cooldown.
  if (!weights && state_.metadata_ready) return AsrLoadAdmission::kReady;
  if (now < state_.retry_after) return AsrLoadAdmission::kCooldown;
  if (weights && state_.ready) return AsrLoadAdmission::kReady;
  state_.loading = true;
  ++state_.load_attempts;
  return AsrLoadAdmission::kStart;
}
void AsrHealth::Loaded(bool weights, uint64_t device_bytes) {
  const std::lock_guard lock(mutex_);
  if (!state_.loading || state_.failure == AsrFailure::kDevice) return;
  state_.loading = false;
  state_.metadata_ready = true;
  if (weights) {
    state_.ready = true;
    state_.device_bytes = device_bytes;
  }
  state_.failure = AsrFailure::kNone;
  state_.retry_after = {};
}
void AsrHealth::CancelLoad() {
  const std::lock_guard lock(mutex_);
  state_.loading = false;
}
void AsrHealth::LoadFailed(AsrFailure failure, Clock::time_point now) {
  const std::lock_guard lock(mutex_);
  if (!state_.loading) return;
  state_.loading = false;
  ++state_.load_failures;
  if (state_.failure != AsrFailure::kDevice) state_.failure = failure;
  state_.retry_after = now + kRetryDelay;
}
void AsrHealth::ComputeFailed(AsrFailure failure, Clock::time_point now) {
  const std::lock_guard lock(mutex_);
  ++state_.compute_failures;
  if (state_.failure != AsrFailure::kDevice) state_.failure = failure;
  state_.retry_after = now + kRetryDelay;
}
void AsrHealth::ComputeSucceeded() {
  const std::lock_guard lock(mutex_);
  if (state_.failure == AsrFailure::kDevice) return;
  state_.failure = AsrFailure::kNone;
  state_.retry_after = {};
}
void AsrHealth::Released() {
  const std::lock_guard lock(mutex_);
  state_.ready = false;
  state_.loading = false;
  state_.device_bytes = 0;
}
AsrHealthSnapshot AsrHealth::Snapshot() const {
  const std::lock_guard lock(mutex_);
  return state_;
}
const char* AsrFailureCode(AsrFailure failure) {
  switch (failure) {
    case AsrFailure::kNone:
      return "none";
    case AsrFailure::kAssets:
      return "asr_assets_invalid";
    case AsrFailure::kTimeout:
      return "asr_assets_timeout";
    case AsrFailure::kWorker:
      return "asr_worker_unavailable";
    case AsrFailure::kResource:
      return "asr_resource_exhausted";
    case AsrFailure::kDevice:
      return "gpu_unhealthy";
    case AsrFailure::kCompute:
      return "asr_compute_failed";
  }
  return "asr_state_invalid";
}
std::string AsrHealthJson(const AsrHealthSnapshot& s, bool gpu_healthy) {
  const char* reason = AsrFailureCode(
      s.configured && !gpu_healthy ? AsrFailure::kDevice : s.failure);
  const char* state = !s.configured                        ? "disabled"
                      : s.loading                          ? "loading"
                      : std::string_view(reason) != "none" ? "degraded"
                      : s.ready                            ? "ready"
                      : s.metadata_ready                   ? "metadata_ready"
                                                           : "configured";
  return std::string(R"({"state":")") + state + R"(","reason":")" + reason +
         R"(","core_readiness_dependency":false,"model":")" +
         audio::AsrConfig::kModelId + R"(","device_bytes":)" +
         std::to_string(s.device_bytes) + R"(,"load_attempts":)" +
         std::to_string(s.load_attempts) + R"(,"load_failures":)" +
         std::to_string(s.load_failures) + R"(,"compute_failures":)" +
         std::to_string(s.compute_failures) +
         R"(,"recovery":"request_driven","retry_delay_ms":5000})";
}
}  // namespace q4t::server
