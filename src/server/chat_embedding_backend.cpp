#include "chat_server_internal.h"

#include <algorithm>
#include <new>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/server/embeddings_execution.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using C = model::EmbeddingConfig;
using object_detail::Error;
constexpr uint64_t kHostMargin = 512ULL * 1024 * 1024;
constexpr auto kRetryDelay = std::chrono::seconds(5);
}  // namespace

std::string ChatServer::NativeEmbeddingHealthJson() const {
  const char* reason = "none";
  switch (embedding_failure_.load()) {
    case EmbeddingFailure::kNone:
      break;
    case EmbeddingFailure::kAssets:
      reason = "embedding_assets_invalid";
      break;
    case EmbeddingFailure::kTimeout:
      reason = "embedding_assets_timeout";
      break;
    case EmbeddingFailure::kWorker:
      reason = "embedding_worker_unavailable";
      break;
    case EmbeddingFailure::kResource:
      reason = "embedding_resource_exhausted";
      break;
    case EmbeddingFailure::kDevice:
      reason = "gpu_unhealthy";
      break;
    case EmbeddingFailure::kCompute:
      reason = "embedding_compute_failed";
      break;
  }
  const bool ready = embedding_ready_.load();
  const char* state = embedding_model_dir_.empty()         ? "disabled"
                      : embedding_loading_.load()          ? "loading"
                      : std::string_view(reason) != "none" ? "degraded"
                      : ready                              ? "ready"
                      : embedding_tokenizer_ready_.load()  ? "metadata_ready"
                                                           : "configured";
  return std::string(R"({"state":")") + state + R"(","reason":")" + reason +
         R"(","core_readiness_dependency":false,"model":")" + C::kModelId +
         R"(","device_bytes":)" +
         std::to_string(embedding_device_bytes_.load()) +
         R"(,"load_attempts":)" + std::to_string(embedding_load_attempts_) +
         R"(,"load_failures":)" + std::to_string(embedding_load_failures_) +
         R"(,"recovery":"request_driven","retry_delay_ms":5000})";
}

ObjectReply ChatServer::EnsureNativeEmbedding(bool weights,
                                              const BinaryAccess& access,
                                              RequestDiagnostics* diagnostic) {
  const auto cancelled = [&] {
    return stop_requested_.load() || Clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  const auto interrupted = [&] {
    return Error(409, "embedding_cancelled", "Embedding asset load cancelled");
  };
  if (embedding_model_dir_.empty())
    return Error(404, "model_not_found", "Embedding model is not configured");
  // Serializes independent metadata/full loading and their cooldown. Never
  // acquired by normal Chat, health endpoints, or GPU execution callbacks.
  auto lock = LockUnlessCancelled(embedding_mu_, cancelled);
  if (!lock.owns_lock() || cancelled()) return interrupted();
  if (weights ? embedding_ready_.load() : embedding_tokenizer_ready_.load())
    return {};
  if (Clock::now() < embedding_retry_after_)
    return Error(503, "embedding_retry_pending", "Embedding recovery cooldown");
  embedding_loading_.store(true);
  ScopeExit loading([&] { embedding_loading_.store(false); });
  const auto fail = [&](EmbeddingFailure kind, const char* code,
                        const char* message) {
    embedding_failure_.store(kind);
    embedding_load_failures_.fetch_add(1);
    embedding_retry_after_ = Clock::now() + kRetryDelay;
    return Error(503, code, message);
  };
  try {
    // Unified RAM: staging, device payload and possibly retained file pages.
    // These are estimates, not a reservation or a guarantee against host OOM.
    const uint64_t estimate =
        kHostMargin + (weights ? 3 * kNativeEmbeddingFileBytes : 0);
    if (!runtime::ReadSystemMemory().CanAllocate(estimate))
      return fail(EmbeddingFailure::kResource, "embedding_resource_exhausted",
                  "Embedding host memory admission unavailable");
    embedding_load_attempts_.fetch_add(1);
    EmbeddingHostAssets assets;
    GrammarFailure cause;
    GrammarControl control;
    control.cancelled = cancelled;
    control.compile_deadline = std::min(
        access.deadline,
        Clock::now() + std::chrono::milliseconds(kEmbeddingAssetTimeoutMs));
    const auto loaded = LoadEmbeddingAssets(embedding_model_dir_, &assets,
                                            control, &cause, weights);
    if (!loaded) {
      if (cancelled() || cause == GrammarFailure::kCancelled)
        return interrupted();
      if (cause == GrammarFailure::kResource)
        return fail(EmbeddingFailure::kResource, "embedding_resource_exhausted",
                    "Embedding asset resources unavailable");
      if (cause == GrammarFailure::kTimeout)
        return fail(EmbeddingFailure::kTimeout, "embedding_assets_timeout",
                    "Embedding asset load timed out");
      if (cause == GrammarFailure::kDependency)
        return fail(EmbeddingFailure::kAssets, "embedding_assets_invalid",
                    "Embedding assets are absent or fail identity checks");
      return fail(EmbeddingFailure::kWorker, "embedding_worker_unavailable",
                  "Embedding asset worker unavailable");
    }
    if (cancelled()) return interrupted();
    if (weights) {
      auto gpu = LockUnlessCancelled(model_mu_, cancelled);
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto healthy = ExecutionStatus();
      if (!healthy)
        return fail(EmbeddingFailure::kDevice, "gpu_unhealthy",
                    "Shared GPU is unavailable");
      if (!runtime::ReadSystemMemory().CanAllocate(assets.WeightBytes().size() +
                                                   kHostMargin))
        return fail(EmbeddingFailure::kResource, "embedding_resource_exhausted",
                    "Embedding device memory admission unavailable");
      std::unique_ptr<RequestDiagnostics::ExecutionScope> execution;
      if (diagnostic)
        execution = std::make_unique<RequestDiagnostics::ExecutionScope>(
            *diagnostic, ProgressStage::kCache);
      uint8_t* pending = nullptr;
      ScopeExit cleanup([&] {
        const auto freed = detail::ReleaseCudaMemory(
            pending, "native_embedding.weights_cleanup");
        RecordDeviceMemoryFailure(freed, "native_embedding.weights_cleanup");
        if (!freed.ok()) AbortBackendExecution();
      });
      const auto memory = detail::AllocateCudaMemory(
          pending, assets.WeightBytes().size(), "native_embedding.weights");
      RecordDeviceMemoryFailure(memory, "native_embedding.weights");
      if (!memory.ok()) {
        if (memory.state == DeviceMemoryState::kFailure)
          AbortBackendExecution();
        return fail(memory.exhausted() ? EmbeddingFailure::kResource
                                       : EmbeddingFailure::kDevice,
                    memory.exhausted() ? memory.Reason() : "gpu_unhealthy",
                    "Embedding weight allocation unavailable");
      }
      auto copied = cudaMemcpyAsync(pending, assets.WeightBytes().data(),
                                    assets.WeightBytes().size(),
                                    cudaMemcpyHostToDevice, nullptr);
      const auto drained = cudaStreamSynchronize(nullptr);
      if (copied == cudaSuccess) copied = drained;
      if (copied != cudaSuccess) {
        AbortBackendExecution();
        return fail(EmbeddingFailure::kDevice, "gpu_unhealthy",
                    "Embedding weight upload failed");
      }
      model::EmbeddingDeviceWeights bound;
      const auto binding = model::BindEmbeddingDeviceWeights(
          pending, assets.WeightBytes().size(), assets.layout, &bound);
      if (!binding)
        return fail(EmbeddingFailure::kAssets, "embedding_assets_invalid",
                    "Embedding weight binding failed");
      if (execution) execution->Completed();
      if (cancelled()) return interrupted();
      d_embedding_weights_ = pending;
      pending = nullptr;
      embedding_weights_ = bound;
      embedding_device_bytes_.store(assets.WeightBytes().size());
      embedding_ready_.store(true);
    }
    // Tokenizer is immutable after publication; ICU use has its own mutex.
    // Do not replace it when later full loading rereads the same fixed assets.
    if (!embedding_tok_) {
      embedding_tok_ = std::move(assets.tokenizer);
      embedding_tokenizer_ready_.store(true);
    }
    embedding_failure_.store(EmbeddingFailure::kNone);
    embedding_retry_after_ = {};
    return {};
  } catch (const std::bad_alloc&) {
    return fail(EmbeddingFailure::kResource, "embedding_resource_exhausted",
                "Embedding host allocation exhausted");
  }
}

ObjectReply ChatServer::RunNativeEmbeddingInput(
    int fd, const std::vector<int32_t>& tokens,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, EmbeddingStepResult* result) {
  *result = {};
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Embedding execution cancelled");
  };
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
    auto loaded = EnsureNativeEmbedding(
        true, {{}, control->deadline(), cancelled}, &diagnostic);
    if (!loaded.ok()) return loaded;
  }
  diagnostic.Stage("slot_admission");
  int slot;
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kSlot);
    // Shared fair admission bounds concurrency; this model owns no primary KV.
    slot = AllocSeqId(control.get(), fd, 1);
  }
  if (slot == -2) return interrupted();
  if (slot < 0)
    return Error(503,
                 slot == -4   ? "queue_capacity_exhausted"
                 : slot == -3 ? "gpu_unhealthy"
                 : slot == -5 ? "memory_pressure"
                 : slot == -6 ? "memory_observation_unavailable"
                              : "server_stopping",
                 "Embedding sequence admission unavailable");
  diagnostic.Admitted(slot);
  ScopeExit release_slot([&] {
    FreeSeqId(slot);
    diagnostic.Released();
    metrics_.prompt_tokens_total.fetch_add(result->committed_input);
  });
  model::EmbeddingWorkspaceLayout layout;
  const auto planned = model::PlanEmbeddingWorkspace(tokens.size(), &layout);
  if (!planned)
    return Error(500, "embedding_state_invalid", "Invalid embedding workspace");
  uint8_t* workspace = nullptr;
  bool cleaned = true;
  ScopeExit release([&] {
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const bool had = workspace != nullptr;
    const auto memory = detail::ReleaseCudaMemory(
        workspace, "native_embedding.workspace_free", control->id().c_str());
    RecordDeviceMemoryFailure(memory, "native_embedding.workspace_free");
    cleaned = memory.ok();
    if (!cleaned) AbortBackendExecution();
    if (had && cleaned) work.Completed();
  });
  result->hidden.resize(C::kHidden);
  model::EmbeddingRun run;
  const auto compute_error = [&](const model::EmbeddingComputeResult& step) {
    const bool device = step.device_error != cudaSuccess;
    if (device) AbortBackendExecution();
    embedding_failure_.store(device ? EmbeddingFailure::kDevice
                                    : EmbeddingFailure::kCompute);
    return Error(503, device ? "gpu_unhealthy" : "embedding_compute_failed",
                 "Native embedding execution failed");
  };
  diagnostic.Stage("prefill");
  for (int phase = -1; phase <= C::kLayers; ++phase) {
    std::unique_lock<std::mutex> gpu;
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kModel);
      gpu = LockUnlessCancelled(model_mu_, cancelled);
    }
    if (!gpu.owns_lock() || cancelled()) return interrupted();
    if (!ExecutionStatus())
      return Error(503, "gpu_unhealthy", "Shared GPU unavailable");
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kPrefill);
    // A host allocation/BLAS planning exception is local to this backend.
    // Drain submitted work before releasing its buffers; poison shared health
    // only if the actual CUDA completion fails.
    OnException failure([&] {
      if (cudaStreamSynchronize(nullptr) != cudaSuccess)
        AbortBackendExecution();
    });
    model::EmbeddingComputeResult step;
    if (phase == -1) {
      if (!runtime::ReadSystemMemory().CanAllocate(layout.bytes + kHostMargin))
        return Error(503, "embedding_resource_exhausted",
                     "Embedding workspace memory admission unavailable");
      const auto memory = detail::AllocateCudaMemory(
          workspace, layout.bytes, "native_embedding.workspace",
          control->id().c_str());
      RecordDeviceMemoryFailure(memory, "native_embedding.workspace");
      if (!memory.ok()) {
        if (memory.state == DeviceMemoryState::kFailure)
          AbortBackendExecution();
        return Error(503,
                     memory.exhausted() ? memory.Reason() : "gpu_unhealthy",
                     "Embedding workspace allocation unavailable");
      }
      step = run.Begin(embedding_weights_, tokens, workspace, layout.bytes);
      // Actual embedding lookup has consumed all input IDs after this sync.
      if (step.ok()) result->committed_input = tokens.size();
    } else if (phase < C::kLayers) {
      step = run.NextLayer();
    } else {
      step = run.Finish(result->hidden);
    }
    if (!step.ok()) return compute_error(step);
    work.Completed();
    metrics_.execution_steps_total.fetch_add(1);
  }
  release.Run();
  if (!cleaned)
    return Error(503, "gpu_unhealthy", "Embedding workspace cleanup failed");
  if (cancelled()) return interrupted();
  embedding_failure_.store(EmbeddingFailure::kNone);
  return {};
}
}  // namespace q4t::server
