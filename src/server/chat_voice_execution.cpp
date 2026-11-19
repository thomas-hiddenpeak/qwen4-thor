#include "chat_server_internal.h"

#include <algorithm>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/audio/tts_speaker_features.h"
#include "q4t/audio/tts_encoder.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::RunNativeVoiceCondition(
    int fd, std::span<const float> pcm,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, std::vector<float>* condition,
    std::vector<audio::TtsCodes>* reference_codes) {
  using Clock = std::chrono::steady_clock;
  using object_detail::Error;
  TtsVoiceResourcePlan resources;
  if (!condition || !reference_codes ||
      !PlanTtsVoiceResources(
          pcm.size(), uint64_t(tts_limits_.workspace_mib) << 20, &resources, true))
    return Error(400, "voice_input_limit",
                 "Voice condition input exceeds capacity");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Voice execution cancelled");
  };
  if (cancelled()) return interrupted();
  std::unique_lock<std::mutex> auxiliary;
  diagnostic.Stage("voice_slot_admission");
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
                 "Voice sequence admission unavailable");
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
    auto reply =
        EnsureNativeVoice({{}, control->deadline(), cancelled}, diagnostic);
    if (!reply.ok()) return reply;
  }

  uint8_t* memory = nullptr;
  bool cleaned = true;
  ScopeExit release([&] {
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const bool had = memory;
    const auto freed = detail::ReleaseCudaMemory(
        memory, "native_voice.workspace_free", control->id().c_str());
    RecordDeviceMemoryFailure(freed, "native_voice.workspace_free");
    cleaned = freed.ok();
    if (!cleaned) {
      voice_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
      AbortBackendExecution();
    } else if (had) {
      work.Completed();
    }
  });
  const auto failed = [&](AsrFailure kind, const char* message) {
    voice_health_.ComputeFailed(kind, Clock::now());
    if (kind == AsrFailure::kDevice) AbortBackendExecution();
    return Error(503, TtsFailureCode(kind), message);
  };
  try {
    {
      auto gpu = LockUnlessCancelled(model_mu_, cancelled);
      if (!gpu.owns_lock() || cancelled()) return interrupted();
      const auto shared = ObserveHealth();
      if (!shared.Healthy())
        return Error(503, shared.ErrorCode(), "Shared backend unavailable");
      if (!runtime::ReadSystemMemory().CanAllocate(
              resources.device_workspace_bytes + (512ULL << 20) +
              resources.host_feature_bytes))
        return failed(AsrFailure::kResource, "Voice memory unavailable");
      RequestDiagnostics::ExecutionScope work(diagnostic,
                                              ProgressStage::kCache);
      const auto allocated = detail::AllocateCudaMemory(
          memory, resources.device_workspace_bytes, "native_voice.workspace",
          control->id().c_str());
      RecordDeviceMemoryFailure(allocated, "native_voice.workspace");
      if (!allocated.ok())
        return failed(
            allocated.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "Voice workspace allocation unavailable");
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
          voice_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
          AbortBackendExecution();
        }
      });
      const auto result = execute();
      if (!result.ok())
        return failed(result.device_error == cudaSuccess ? AsrFailure::kCompute
                                                         : AsrFailure::kDevice,
                      "Native voice execution failed");
      work.Completed();
      metrics_.execution_steps_total.fetch_add(1);
      return ObjectReply{};
    };
    if (cancelled()) return interrupted();
    diagnostic.Stage("voice_condition_features");
    audio::TtsSpeakerFeatures features;
    const auto status =
        audio::ComputeTtsSpeakerFeatures(pcm, 24000, {}, &features, cancelled);
    if (!status) {
      if (cancelled()) return interrupted();
      return Error(400, "voice_features_invalid", status.message());
    }
    audio::TtsSpeakerRun run;
    diagnostic.Stage("voice_condition_embedding");
    auto reply = perform([&] {
      return run.Begin(voice_weights_.speaker, features.mel, features.frames,
                       memory, resources.speaker_workspace_bytes);
    });
    if (!reply.ok()) return reply;
    for (int stage = 0; stage < audio::TtsSpeakerRun::kStages; ++stage) {
      reply = perform([&] { return run.NextStage(); });
      if (!reply.ok()) return reply;
    }
    std::vector<float> output(2048);
    reply = perform([&] { return run.Finish(output); });
    if (!reply.ok()) return reply;
    audio::TtsEncoderRun encoder;
    diagnostic.Stage("voice_reference_codes");
    reply = perform([&] {
      return encoder.Begin(voice_weights_.encoder, pcm, 24000, memory,
                            resources.encoder_workspace_bytes);
    });
    if (!reply.ok()) return reply;
    size_t advances = 0;
    while (!encoder.done()) {
      if (++advances > 8192)
        return failed(AsrFailure::kCompute, "Voice encoder progress limit");
      reply = perform([&] { return encoder.Advance(); });
      if (!reply.ok()) return reply;
    }
    if (encoder.codes().size() != resources.reference_frames)
      return failed(AsrFailure::kCompute, "Voice encoder frame count differs");
    std::vector<audio::TtsCodes> codes(encoder.codes().begin(),
                                      encoder.codes().end());
    release.Run();
    if (!cleaned)
      return Error(503, "gpu_unhealthy", "Voice workspace cleanup failed");
    if (cancelled()) return interrupted();
    voice_health_.ComputeSucceeded();
    *condition = std::move(output);
    *reference_codes = std::move(codes);
    return {};
  } catch (const std::bad_alloc&) {
    return failed(AsrFailure::kResource, "Voice host allocation exhausted");
  }
}
}  // namespace q4t::server
