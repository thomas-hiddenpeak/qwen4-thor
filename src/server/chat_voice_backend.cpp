#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/tts_assets.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
constexpr uint64_t kHostMargin = 512ULL * 1024 * 1024;
}  // namespace

std::string ChatServer::NativeVoiceHealthJson() const {
  auto result =
      SpeechHealthJson(voice_health_.Snapshot(), gpu_healthy_.load(),
                       "qwen3-tts-12hz-1.7b-base-speaker", TtsFailureCode);
  result.pop_back();
  result += R"(,"asset_cleanup_pending":)";
  result += detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets)
                ? "true}"
                : "false}";
  return result;
}

ObjectReply ChatServer::EnsureNativeVoice(const BinaryAccess& access,
                                          RequestDiagnostics& diagnostic) {
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "voice_cancelled", "Voice asset load cancelled");
  };
  if (tts_base_model_dir_.empty())
    return Error(503, "voice_not_configured", "Voice model is not configured");
  // No ordinary Chat/ASR/health path takes this mutex. Asset reads never take
  // model_mu_; only the later bounded device allocation/upload does.
  auto lock = LockUnlessCancelled(voice_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  switch (voice_health_.BeginLoad(true, Clock::now())) {
    case AsrLoadAdmission::kReady:
      return {};
    case AsrLoadAdmission::kDisabled:
      return Error(503, "voice_not_configured",
                   "Voice model is not configured");
    case AsrLoadAdmission::kCooldown:
      return Error(503, "voice_retry_pending", "Voice recovery cooldown");
    case AsrLoadAdmission::kLoading:
      return Error(503, "voice_loading", "Voice load already active");
    case AsrLoadAdmission::kDevice:
      return Error(503, "gpu_unhealthy", "Shared GPU is unavailable");
    case AsrLoadAdmission::kStart:
      break;
  }
  ScopeExit loading([&] { voice_health_.CancelLoad(); });
  const auto fail = [&](AsrFailure kind, const char* message) {
    voice_health_.LoadFailed(kind, Clock::now());
    return Error(503, TtsFailureCode(kind), message);
  };
  try {
    const uint64_t estimate = kHostMargin + kNativeTtsBaseFileBytes +
                              audio::TtsSpeakerConfig::kDeviceBytes +
                              (6ULL << 20);
    if (!runtime::ReadSystemMemory().CanAllocate(estimate))
      return fail(AsrFailure::kResource,
                  "Voice host memory admission unavailable");
    TtsHostAssets assets;
    GrammarFailure cause;
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = std::min(
        access.deadline,
        Clock::now() + std::chrono::milliseconds(kModelAssetTimeoutMs));
    const auto loaded = LoadTtsAssets(tts_base_model_dir_, &assets, control,
                                      &cause, true, kTtsAssetStagingBytes,
                                      {audio::TtsVariant::kBase17B, false});
    if (!loaded) {
      if (cancelled() || cause == GrammarFailure::kCancelled)
        return interrupted();
      if (cause == GrammarFailure::kResource)
        return fail(AsrFailure::kResource, "Voice asset resources unavailable");
      if (cause == GrammarFailure::kTimeout)
        return fail(AsrFailure::kTimeout, "Voice asset load timed out");
      if (cause == GrammarFailure::kDependency)
        return fail(AsrFailure::kAssets,
                    "Voice assets are absent or fail identity checks");
      return fail(AsrFailure::kWorker, "Voice asset worker unavailable");
    }
    if (cancelled()) return interrupted();
    {
      auto gpu = LockUnlessCancelled(model_mu_, cancelled);
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto shared = ObserveHealth();
      if (!shared.gpu_healthy)
        return fail(AsrFailure::kDevice, "Shared GPU is unavailable");
      if (!shared.Healthy())
        return Error(503, shared.ErrorCode(), "Primary backend is unavailable");
      if (!runtime::ReadSystemMemory().CanAllocate(
              audio::TtsSpeakerConfig::kDeviceBytes + kHostMargin))
        return fail(AsrFailure::kResource,
                    "Voice device memory admission unavailable");
      RequestDiagnostics::ExecutionScope execution(diagnostic,
                                                   ProgressStage::kCache);
      uint8_t* pending = nullptr;
      ScopeExit cleanup([&] {
        const auto freed =
            detail::ReleaseCudaMemory(pending, "native_voice.weights_cleanup");
        RecordDeviceMemoryFailure(freed, "native_voice.weights_cleanup");
        if (!freed.ok()) {
          voice_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto memory = detail::AllocateCudaMemory(
          pending, audio::TtsSpeakerConfig::kDeviceBytes,
          "native_voice.weights");
      RecordDeviceMemoryFailure(memory, "native_voice.weights");
      if (!memory.ok()) {
        if (memory.state == DeviceMemoryState::kFailure)
          AbortBackendExecution();
        return fail(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Voice weight allocation unavailable");
      }
      audio::TtsSpeakerDeviceWeights bound;
      const auto upload = UploadTtsSpeakerWeights(
          assets, {pending, audio::TtsSpeakerConfig::kDeviceBytes}, &bound,
          cancelled);
      if (!upload.ok()) {
        if (upload.device_error != cudaSuccess) {
          AbortBackendExecution();
          return fail(AsrFailure::kDevice, "Voice weight upload failed");
        }
        if (cancelled()) return interrupted();
        return fail(AsrFailure::kAssets, "Voice weight binding failed");
      }
      execution.Completed();
      if (cancelled()) return interrupted();
      d_voice_weights_ = pending;
      pending = nullptr;
      voice_weights_ = bound;
    }
    voice_health_.Loaded(true, audio::TtsSpeakerConfig::kDeviceBytes);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(AsrFailure::kResource, "Voice host allocation exhausted");
  }
}
}  // namespace q4t::server
