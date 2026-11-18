#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/aligner_assets.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
constexpr uint64_t kHostMargin = 512ULL * 1024 * 1024;
}  // namespace

std::string ChatServer::NativeAlignerHealthJson() const {
  auto result =
      AlignerHealthJson(aligner_health_.Snapshot(), gpu_healthy_.load());
  result.pop_back();
  result += R"(,"asset_cleanup_pending":)";
  result += detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets)
                ? "true}"
                : "false}";
  return result;
}

ObjectReply ChatServer::EnsureNativeAligner(bool weights,
                                            const BinaryAccess& access,
                                            RequestDiagnostics* diagnostic) {
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "alignment_cancelled", "Alignment asset load cancelled");
  };
  if (aligner_model_dir_.empty())
    return Error(503, "alignment_not_configured",
                 "Alignment model is not configured");
  // No ordinary Chat/health path takes this mutex. Asset reads never take
  // model_mu_; only the later bounded device allocation/upload does.
  auto lock = LockUnlessCancelled(aligner_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  switch (aligner_health_.BeginLoad(weights, Clock::now())) {
    case AsrLoadAdmission::kReady:
      return {};
    case AsrLoadAdmission::kDisabled:
      return Error(503, "alignment_not_configured",
                   "Alignment model is not configured");
    case AsrLoadAdmission::kCooldown:
      return Error(503, "alignment_retry_pending",
                   "Alignment recovery cooldown");
    case AsrLoadAdmission::kLoading:
      return Error(503, "alignment_loading", "Alignment load already active");
    case AsrLoadAdmission::kDevice:
      return Error(503, "gpu_unhealthy", "Shared GPU is unavailable");
    case AsrLoadAdmission::kStart:
      break;
  }
  ScopeExit loading([&] { aligner_health_.CancelLoad(); });
  const auto fail = [&](AsrFailure kind, const char* message) {
    aligner_health_.LoadFailed(kind, Clock::now());
    return Error(503, AlignerFailureCode(kind), message);
  };
  try {
    const uint64_t estimate =
        kHostMargin + (weights ? 2 * kNativeAlignerFileBytes : 0);
    if (!runtime::ReadSystemMemory().CanAllocate(estimate))
      return fail(AsrFailure::kResource,
                  "Alignment host memory admission unavailable");
    AlignerHostAssets assets;
    GrammarFailure cause;
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = std::min(
        access.deadline,
        Clock::now() + std::chrono::milliseconds(kModelAssetTimeoutMs));
    const auto loaded = LoadAlignerAssets(aligner_model_dir_, &assets, control,
                                          &cause, weights);
    if (!loaded) {
      if (cancelled() || cause == GrammarFailure::kCancelled)
        return interrupted();
      if (cause == GrammarFailure::kResource)
        return fail(AsrFailure::kResource,
                    "Alignment asset resources unavailable");
      if (cause == GrammarFailure::kTimeout)
        return fail(AsrFailure::kTimeout, "Alignment asset load timed out");
      if (cause == GrammarFailure::kDependency)
        return fail(AsrFailure::kAssets,
                    "Alignment assets are absent or fail identity checks");
      return fail(AsrFailure::kWorker, "Alignment asset worker unavailable");
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
                    "Alignment device memory admission unavailable");
      std::unique_ptr<RequestDiagnostics::ExecutionScope> execution;
      if (diagnostic)
        execution = std::make_unique<RequestDiagnostics::ExecutionScope>(
            *diagnostic, ProgressStage::kCache);
      uint8_t* pending = nullptr;
      ScopeExit cleanup([&] {
        const auto freed = detail::ReleaseCudaMemory(
            pending, "native_aligner.weights_cleanup");
        RecordDeviceMemoryFailure(freed, "native_aligner.weights_cleanup");
        if (!freed.ok()) {
          aligner_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto memory = detail::AllocateCudaMemory(
          pending, assets.WeightBytes().size(), "native_aligner.weights");
      RecordDeviceMemoryFailure(memory, "native_aligner.weights");
      if (!memory.ok()) {
        if (memory.state == DeviceMemoryState::kFailure)
          AbortBackendExecution();
        return fail(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Alignment weight allocation unavailable");
      }
      auto copied = cudaMemcpyAsync(pending, assets.WeightBytes().data(),
                                    assets.WeightBytes().size(),
                                    cudaMemcpyHostToDevice, nullptr);
      const auto drained = cudaStreamSynchronize(nullptr);
      if (copied == cudaSuccess) copied = drained;
      if (copied != cudaSuccess) {
        AbortBackendExecution();
        return fail(AsrFailure::kDevice, "Alignment weight upload failed");
      }
      audio::AlignerDeviceWeights bound;
      if (!audio::BindAlignerDeviceWeights(pending, assets.WeightBytes().size(),
                                           assets.layout, &bound))
        return fail(AsrFailure::kAssets, "Alignment weight binding failed");
      if (execution) execution->Completed();
      if (cancelled()) return interrupted();
      d_aligner_weights_ = pending;
      pending = nullptr;
      aligner_weights_ = bound;
    }
    if (!aligner_tok_) aligner_tok_ = std::move(assets.tokenizer);
    aligner_health_.Loaded(weights, weights ? assets.WeightBytes().size() : 0);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(AsrFailure::kResource, "Alignment host allocation exhausted");
  }
}
}  // namespace q4t::server
