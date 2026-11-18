#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/separator_assets.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
constexpr uint64_t kHostMargin = 512ULL * 1024 * 1024;
}  // namespace

std::string ChatServer::NativeSeparatorHealthJson() const {
  auto result =
      SeparatorHealthJson(separator_health_.Snapshot(), gpu_healthy_.load());
  result.pop_back();
  result += R"(,"asset_cleanup_pending":)";
  result += detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets)
                ? "true}"
                : "false}";
  return result;
}

ObjectReply ChatServer::EnsureNativeSeparator(bool weights,
                                              const BinaryAccess& access,
                                              RequestDiagnostics* diagnostic) {
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "separator_cancelled", "Separator asset load cancelled");
  };
  if (separator_model_dir_.empty())
    return Error(503, "separator_not_configured",
                 "Separator model is not configured");
  // No ordinary Chat/ASR/health path takes this mutex. Asset reads never take
  // model_mu_; only the later bounded device allocation/upload does.
  auto lock = LockUnlessCancelled(separator_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  switch (separator_health_.BeginLoad(weights, Clock::now())) {
    case AsrLoadAdmission::kReady:
      return {};
    case AsrLoadAdmission::kDisabled:
      return Error(503, "separator_not_configured",
                   "Separator model is not configured");
    case AsrLoadAdmission::kCooldown:
      return Error(503, "separator_retry_pending",
                   "Separator recovery cooldown");
    case AsrLoadAdmission::kLoading:
      return Error(503, "separator_loading", "Separator load already active");
    case AsrLoadAdmission::kDevice:
      return Error(503, "gpu_unhealthy", "Shared GPU is unavailable");
    case AsrLoadAdmission::kStart:
      break;
  }
  ScopeExit loading([&] { separator_health_.CancelLoad(); });
  const auto fail = [&](AsrFailure kind, const char* message) {
    separator_health_.LoadFailed(kind, Clock::now());
    return Error(503, SeparatorFailureCode(kind), message);
  };
  try {
    const uint64_t estimate =
        kHostMargin + (weights ? 2 * audio::SeparatorConfig::kFileBytes : 0);
    if (!runtime::ReadSystemMemory().CanAllocate(estimate))
      return fail(AsrFailure::kResource,
                  "Separator host memory admission unavailable");
    SeparatorHostAssets assets;
    GrammarFailure cause;
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = std::min(
        access.deadline,
        Clock::now() + std::chrono::milliseconds(kModelAssetTimeoutMs));
    const auto loaded = LoadSeparatorAssets(separator_model_dir_, &assets,
                                            control, &cause, weights);
    if (!loaded) {
      if (cancelled() || cause == GrammarFailure::kCancelled)
        return interrupted();
      if (cause == GrammarFailure::kResource)
        return fail(AsrFailure::kResource,
                    "Separator asset resources unavailable");
      if (cause == GrammarFailure::kTimeout)
        return fail(AsrFailure::kTimeout, "Separator asset load timed out");
      if (cause == GrammarFailure::kDependency)
        return fail(AsrFailure::kAssets,
                    "Separator assets are absent or fail identity checks");
      return fail(AsrFailure::kWorker, "Separator asset worker unavailable");
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
                    "Separator device memory admission unavailable");
      std::unique_ptr<RequestDiagnostics::ExecutionScope> execution;
      if (diagnostic)
        execution = std::make_unique<RequestDiagnostics::ExecutionScope>(
            *diagnostic, ProgressStage::kCache);
      uint8_t* pending = nullptr;
      ScopeExit cleanup([&] {
        const auto freed = detail::ReleaseCudaMemory(
            pending, "native_separator.weights_cleanup");
        RecordDeviceMemoryFailure(freed, "native_separator.weights_cleanup");
        if (!freed.ok()) {
          separator_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto memory = detail::AllocateCudaMemory(
          pending, assets.WeightBytes().size(), "native_separator.weights");
      RecordDeviceMemoryFailure(memory, "native_separator.weights");
      if (!memory.ok()) {
        if (memory.state == DeviceMemoryState::kFailure)
          AbortBackendExecution();
        return fail(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Separator weight allocation unavailable");
      }
      auto copied = cudaMemcpyAsync(pending, assets.WeightBytes().data(),
                                    assets.WeightBytes().size(),
                                    cudaMemcpyHostToDevice, nullptr);
      const auto drained = cudaStreamSynchronize(nullptr);
      if (copied == cudaSuccess) copied = drained;
      if (copied != cudaSuccess) {
        AbortBackendExecution();
        return fail(AsrFailure::kDevice, "Separator weight upload failed");
      }
      audio::SeparatorDeviceWeights bound;
      if (!audio::BindSeparatorDeviceWeights(
              pending, assets.WeightBytes().size(), assets.layout, &bound))
        return fail(AsrFailure::kAssets, "Separator weight binding failed");
      if (execution) execution->Completed();
      if (cancelled()) return interrupted();
      d_separator_weights_ = pending;
      pending = nullptr;
      separator_weights_ = bound;
    }
    separator_health_.Loaded(weights,
                             weights ? assets.WeightBytes().size() : 0);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(AsrFailure::kResource, "Separator host allocation exhausted");
  }
}
}  // namespace q4t::server
