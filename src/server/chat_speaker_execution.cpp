#include "chat_server_internal.h"

#include <algorithm>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/audio/speaker_features.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::RunNativeSpeaker(
    int fd, std::span<const std::span<const float>> clips,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic,
    std::vector<audio::SpeakerEmbedding>* embeddings) {
  using Clock = std::chrono::steady_clock;
  using object_detail::Error;
  if (!embeddings || clips.size() > size_t(speaker_limits_.max_windows) + 4)
    return Error(400, "speaker_input_limit", "Speaker clip count invalid");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Speaker execution cancelled");
  };
  size_t max_samples = 640;
  for (const auto clip : clips) {
    if (clip.empty() ||
        clip.size() > size_t(std::max(10000, speaker_limits_.window_ms)) * 16)
      return Error(400, "speaker_input_limit", "Speaker clip duration invalid");
    max_samples = std::max(max_samples, clip.size());
  }
  if (cancelled()) return interrupted();
  if (clips.empty()) {
    embeddings->clear();
    return {};
  }
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
    auto reply = EnsureNativeSpeaker(true, {{}, control->deadline(), cancelled},
                                     &diagnostic);
    if (!reply.ok()) return reply;
  }
  diagnostic.Stage("speaker_slot_admission");
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
                 "Speaker sequence admission unavailable");
  diagnostic.Admitted(slot);
  ScopeExit admitted([&] {
    FreeSeqId(slot);
    diagnostic.Released();
  });
  audio::SpeakerWorkspaceLayout layout;
  auto status = audio::PlanSpeakerWorkspace(
      1 + max_samples / 160, audio::SpeakerWorkspaceLayout::kMaxFrames,
      uint64_t(speaker_limits_.workspace_mib) << 20, &layout);
  if (!status) return Error(400, "speaker_workspace_limit", status.message());
  uint8_t* memory = nullptr;
  bool cleaned = true;
  ScopeExit release([&] {
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const bool had = memory;
    const auto freed = detail::ReleaseCudaMemory(
        memory, "native_speaker.workspace_free", control->id().c_str());
    RecordDeviceMemoryFailure(freed, "native_speaker.workspace_free");
    cleaned = freed.ok();
    if (!cleaned) {
      speaker_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
      AbortBackendExecution();
    } else if (had) {
      work.Completed();
    }
  });
  const auto failed = [&](AsrFailure kind, const char* message) {
    speaker_health_.ComputeFailed(kind, Clock::now());
    if (kind == AsrFailure::kDevice) AbortBackendExecution();
    return Error(503, SpeakerFailureCode(kind), message);
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
        return failed(AsrFailure::kResource, "Speaker memory unavailable");
      RequestDiagnostics::ExecutionScope work(diagnostic,
                                              ProgressStage::kCache);
      const auto allocated = detail::AllocateCudaMemory(
          memory, layout.bytes, "native_speaker.workspace",
          control->id().c_str());
      RecordDeviceMemoryFailure(allocated, "native_speaker.workspace");
      if (!allocated.ok())
        return failed(
            allocated.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Speaker workspace allocation unavailable");
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
          speaker_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto result = execute();
      if (!result.ok())
        return failed(result.device_error == cudaSuccess ? AsrFailure::kCompute
                                                         : AsrFailure::kDevice,
                      "Native speaker execution failed");
      work.Completed();
      metrics_.execution_steps_total.fetch_add(1);
      return ObjectReply{};
    };
    std::vector<audio::SpeakerEmbedding> output;
    output.reserve(clips.size());
    for (auto clip : clips) {
      if (cancelled()) return interrupted();
      diagnostic.Stage("speaker_features");
      std::vector<float> padded;
      if (clip.size() < 640) {
        padded.assign(640, 0);
        std::copy(clip.begin(), clip.end(), padded.begin());
        clip = padded;
      }
      audio::SpeakerFeatures features;
      status = audio::ComputeSpeakerFeatures(
          clip, 16000, {max_samples, size_t(layout.frames)}, &features,
          cancelled);
      if (!status) {
        if (cancelled()) return interrupted();
        return Error(400, "speaker_features_invalid", status.message());
      }
      audio::SpeakerRun run;
      diagnostic.Stage("speaker_embedding");
      auto reply = perform([&] {
        return run.Begin(speaker_weights_, features.mel, features.frames,
                         memory, layout.bytes);
      });
      if (!reply.ok()) return reply;
      for (int stage = 0; stage < 7; ++stage) {
        reply = perform([&] { return run.NextStage(); });
        if (!reply.ok()) return reply;
      }
      audio::SpeakerEmbedding embedding;
      reply = perform([&] { return run.Finish(embedding); });
      if (!reply.ok()) return reply;
      output.push_back(embedding);
    }
    release.Run();
    if (!cleaned)
      return Error(503, "gpu_unhealthy", "Speaker workspace cleanup failed");
    if (cancelled()) return interrupted();
    speaker_health_.ComputeSucceeded();
    *embeddings = std::move(output);
    return {};
  } catch (const std::bad_alloc&) {
    return failed(AsrFailure::kResource, "Speaker host allocation exhausted");
  }
}
}  // namespace q4t::server
