#include "chat_server_internal.h"

#include <algorithm>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/audio/separator_resample.h"
#include "q4t/audio/separator_tracks.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::RunNativeSeparator(
    int fd, std::span<const float> input,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, std::array<std::vector<float>, 2>* tracks,
    size_t* track_count) {
  using Clock = std::chrono::steady_clock;
  using object_detail::Error;
  const size_t max_samples =
      size_t(separator_limits_.max_audio_seconds) * 16000;
  if (!tracks || !track_count || input.empty() || input.size() > max_samples)
    return Error(400, "separator_input_limit", "Separator duration invalid");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Separator execution cancelled");
  };
  if (cancelled()) return interrupted();
  SeparatorResourcePlan resources;
  auto status = PlanSeparatorServiceResources(separator_limits_, &resources);
  if (!status || !runtime::ReadSystemMemory().CanAllocate(
                     resources.request_host_bytes + (512ULL << 20)))
    return Error(503, "separator_resource_exhausted",
                 "Separator host memory unavailable");
  diagnostic.Stage("separator_resample");
  std::vector<float> pcm;
  status = audio::DownsampleSeparation(input, max_samples, &pcm, cancelled);
  if (!status) {
    if (cancelled()) return interrupted();
    return Error(400, "separator_input_invalid", status.message());
  }
  const size_t original_samples = pcm.size();
  // A sub-millisecond input still keeps its exact original duration. Padding
  // only supplies the minimum encoder receptive field; trim before restoring.
  if (pcm.size() < 16) pcm.resize(16, 0);
  std::unique_lock<std::mutex> auxiliary;
  diagnostic.Stage("separator_slot_admission");
  int slot;
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kSlot);
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
                 "Separator sequence admission unavailable");
  diagnostic.Admitted(slot);
  ScopeExit admitted([&] {
    FreeSeqId(slot);
    diagnostic.Released();
  });
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kSlot);
    auxiliary = LockUnlessCancelled(auxiliary_mu_, cancelled);
  }
  if (!auxiliary.owns_lock() || cancelled()) return interrupted();
  if (auto switched = ActivateAuxiliaryFamily(false, cancelled); !switched.ok())
    return switched;
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
    auto reply = EnsureNativeSeparator(
        true, {{}, control->deadline(), cancelled}, &diagnostic);
    if (!reply.ok()) return reply;
  }

  audio::SeparatorWorkspaceLayout layout;
  status = audio::PlanSeparatorWorkspace(
      pcm.size(), max_samples / 2,
      uint64_t(separator_limits_.workspace_mib) << 20, &layout);
  if (!status) return Error(400, "separator_workspace_limit", status.message());
  uint8_t* memory = nullptr;
  bool cleaned = true;
  ScopeExit release([&] {
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const bool had = memory;
    const auto freed = detail::ReleaseCudaMemory(
        memory, "native_separator.workspace_free", control->id().c_str());
    RecordDeviceMemoryFailure(freed, "native_separator.workspace_free");
    cleaned = freed.ok();
    if (!cleaned) {
      separator_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
      AbortBackendExecution();
    } else if (had) {
      work.Completed();
    }
  });
  const auto failed = [&](AsrFailure kind, const char* message) {
    separator_health_.ComputeFailed(kind, Clock::now());
    if (kind == AsrFailure::kDevice) AbortBackendExecution();
    return Error(503, SeparatorFailureCode(kind), message);
  };
  try {
    {
      auto gpu = LockUnlessCancelled(model_mu_, cancelled);
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto shared = ObserveHealth();
      if (!shared.Healthy())
        return Error(503, shared.ErrorCode(), "Shared backend unavailable");
      if (!runtime::ReadSystemMemory().CanAllocate(layout.bytes +
                                                   (512ULL << 20)))
        return failed(AsrFailure::kResource, "Separator memory unavailable");
      RequestDiagnostics::ExecutionScope work(diagnostic,
                                              ProgressStage::kCache);
      const auto allocated = detail::AllocateCudaMemory(
          memory, layout.bytes, "native_separator.workspace",
          control->id().c_str());
      RecordDeviceMemoryFailure(allocated, "native_separator.workspace");
      if (!allocated.ok())
        return failed(
            allocated.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Separator workspace allocation unavailable");
      work.Completed();
    }
    const auto perform = [&](const auto& execute) {
      std::unique_lock<std::mutex> gpu;
      {
        RequestDiagnostics::Scope waiting(diagnostic,
                                          RequestDiagnostics::Wait::kModel);
        gpu = LockUnlessCancelled(model_mu_, cancelled);
      }
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto shared = ObserveHealth();
      if (!shared.Healthy())
        return Error(503, shared.ErrorCode(), "Shared backend unavailable");
      RequestDiagnostics::ExecutionScope work(diagnostic,
                                              ProgressStage::kPrefill);
      OnException drain([&] {
        if (cudaStreamSynchronize(nullptr) != cudaSuccess) {
          separator_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto result = execute();
      if (!result.ok())
        return failed(result.device_error == cudaSuccess ? AsrFailure::kCompute
                                                         : AsrFailure::kDevice,
                      "Native separator execution failed");
      work.Completed();
      metrics_.execution_steps_total.fetch_add(1);
      return ObjectReply{};
    };
    audio::SeparatorRun run;
    diagnostic.Stage("separator_inference");
    auto reply = perform([&] {
      return run.Begin(separator_weights_, pcm, memory, layout.bytes);
    });
    if (!reply.ok()) return reply;
    for (int stage = 0; stage < audio::SeparatorRun::kStages; ++stage) {
      reply = perform([&] { return run.NextStage(); });
      if (!reply.ok()) return reply;
    }
    std::vector<float> separated(2 * pcm.size());
    reply = perform([&] { return run.Finish(separated); });
    if (!reply.ok()) return reply;
    release.Run();
    if (!cleaned)
      return Error(503, "gpu_unhealthy", "Separator workspace cleanup failed");
    if (cancelled()) return interrupted();
    diagnostic.Stage("separator_track_inspection");
    audio::SeparationTrackDecision decision;
    status = audio::InspectSeparationTracks(
        std::span<const float>(separated).first(original_samples),
        std::span<const float>(separated).subspan(pcm.size(), original_samples),
        &decision, cancelled);
    if (!status) {
      if (cancelled()) return interrupted();
      return failed(AsrFailure::kCompute, "Separator track inspection failed");
    }
    diagnostic.Stage("separator_restore");
    std::array<std::vector<float>, 2> output;
    if (decision.use_original) output[0].assign(input.begin(), input.end());
    for (size_t track = 0; !decision.use_original && track < 2; ++track) {
      status = audio::RestoreSeparation(
          std::span<const float>(separated).subspan(track * pcm.size(),
                                                    original_samples),
          input.size(), max_samples, &output[track], cancelled);
      if (status)
        status = audio::NormalizeSeparation(output[track], max_samples,
                                            &output[track], cancelled);
      if (!status) {
        if (cancelled()) return interrupted();
        return failed(AsrFailure::kCompute, "Separator restoration failed");
      }
    }
    if (cancelled()) return interrupted();
    separator_health_.ComputeSucceeded();
    *tracks = std::move(output);
    *track_count = decision.use_original ? 1 : 2;
    return {};
  } catch (const std::bad_alloc&) {
    return failed(AsrFailure::kResource, "Separator host allocation exhausted");
  }
}
}  // namespace q4t::server
