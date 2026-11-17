#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/speaker_assets.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
constexpr uint64_t kHostMargin = 512ULL * 1024 * 1024;
}  // namespace

std::string ChatServer::NativeSpeakerHealthJson() const {
  auto result =
      SpeakerHealthJson(speaker_health_.Snapshot(), gpu_healthy_.load());
  result.pop_back();
  result += R"(,"asset_cleanup_pending":)";
  result += detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets)
                ? "true}"
                : "false}";
  return result;
}

ObjectReply ChatServer::EnsureNativeSpeaker(bool weights,
                                            const BinaryAccess& access,
                                            RequestDiagnostics* diagnostic) {
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "speaker_cancelled", "Speaker asset load cancelled");
  };
  if (speaker_model_dir_.empty())
    return Error(503, "speaker_not_configured",
                 "Speaker model is not configured");
  // No ordinary Chat/ASR/health path takes this mutex. Asset reads never take
  // model_mu_; only the later bounded device allocation/upload does.
  auto lock = LockUnlessCancelled(speaker_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  switch (speaker_health_.BeginLoad(weights, Clock::now())) {
    case AsrLoadAdmission::kReady:
      return {};
    case AsrLoadAdmission::kDisabled:
      return Error(503, "speaker_not_configured",
                   "Speaker model is not configured");
    case AsrLoadAdmission::kCooldown:
      return Error(503, "speaker_retry_pending", "Speaker recovery cooldown");
    case AsrLoadAdmission::kLoading:
      return Error(503, "speaker_loading", "Speaker load already active");
    case AsrLoadAdmission::kDevice:
      return Error(503, "gpu_unhealthy", "Shared GPU is unavailable");
    case AsrLoadAdmission::kStart:
      break;
  }
  ScopeExit loading([&] { speaker_health_.CancelLoad(); });
  const auto fail = [&](AsrFailure kind, const char* message) {
    speaker_health_.LoadFailed(kind, Clock::now());
    return Error(503, SpeakerFailureCode(kind), message);
  };
  try {
    const uint64_t estimate =
        kHostMargin + (weights ? 3 * audio::SpeakerConfig::kFileBytes : 0);
    if (!runtime::ReadSystemMemory().CanAllocate(estimate))
      return fail(AsrFailure::kResource,
                  "Speaker host memory admission unavailable");
    SpeakerHostAssets assets;
    GrammarFailure cause;
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = std::min(
        access.deadline,
        Clock::now() + std::chrono::milliseconds(kModelAssetTimeoutMs));
    const auto loaded = LoadSpeakerAssets(speaker_model_dir_, &assets, control,
                                          &cause, weights);
    if (!loaded) {
      if (cancelled() || cause == GrammarFailure::kCancelled)
        return interrupted();
      if (cause == GrammarFailure::kResource)
        return fail(AsrFailure::kResource,
                    "Speaker asset resources unavailable");
      if (cause == GrammarFailure::kTimeout)
        return fail(AsrFailure::kTimeout, "Speaker asset load timed out");
      if (cause == GrammarFailure::kDependency)
        return fail(AsrFailure::kAssets,
                    "Speaker assets are absent or fail identity checks");
      return fail(AsrFailure::kWorker, "Speaker asset worker unavailable");
    }
    if (cancelled()) return interrupted();
    if (weights) {
      auto gpu = LockUnlessCancelled(model_mu_, cancelled);
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto shared = ObserveHealth();
      if (!shared.gpu_healthy)
        return fail(AsrFailure::kDevice, "Shared GPU is unavailable");
      if (!shared.Healthy())
        return Error(503, shared.ErrorCode(), "Primary backend is unavailable");
      if (!runtime::ReadSystemMemory().CanAllocate(assets.WeightBytes().size() +
                                                   kHostMargin))
        return fail(AsrFailure::kResource,
                    "Speaker device memory admission unavailable");
      std::unique_ptr<RequestDiagnostics::ExecutionScope> execution;
      if (diagnostic)
        execution = std::make_unique<RequestDiagnostics::ExecutionScope>(
            *diagnostic, ProgressStage::kCache);
      uint8_t* pending = nullptr;
      ScopeExit cleanup([&] {
        const auto freed = detail::ReleaseCudaMemory(
            pending, "native_speaker.weights_cleanup");
        RecordDeviceMemoryFailure(freed, "native_speaker.weights_cleanup");
        if (!freed.ok()) {
          speaker_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto memory = detail::AllocateCudaMemory(
          pending, assets.WeightBytes().size(), "native_speaker.weights");
      RecordDeviceMemoryFailure(memory, "native_speaker.weights");
      if (!memory.ok()) {
        if (memory.state == DeviceMemoryState::kFailure)
          AbortBackendExecution();
        return fail(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Speaker weight allocation unavailable");
      }
      auto copied = cudaMemcpyAsync(pending, assets.WeightBytes().data(),
                                    assets.WeightBytes().size(),
                                    cudaMemcpyHostToDevice, nullptr);
      const auto drained = cudaStreamSynchronize(nullptr);
      if (copied == cudaSuccess) copied = drained;
      if (copied != cudaSuccess) {
        AbortBackendExecution();
        return fail(AsrFailure::kDevice, "Speaker weight upload failed");
      }
      audio::SpeakerDeviceWeights bound;
      if (!audio::BindSpeakerDeviceWeights(pending, assets.WeightBytes().size(),
                                           assets.layout, &bound))
        return fail(AsrFailure::kAssets, "Speaker weight binding failed");
      if (execution) execution->Completed();
      if (cancelled()) return interrupted();
      d_speaker_weights_ = pending;
      pending = nullptr;
      speaker_weights_ = bound;
    }
    speaker_health_.Loaded(weights, weights ? assets.WeightBytes().size() : 0);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(AsrFailure::kResource, "Speaker host allocation exhausted");
  }
}
}  // namespace q4t::server
