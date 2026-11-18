#include "chat_server_internal.h"

#include <algorithm>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/audio/tts_synthesis.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using object_detail::Error;
}  // namespace

ObjectReply ChatServer::RunNativeTts(
    int fd, const SpeechRequest& request, const audio::TtsPrompt& prompt,
    size_t max_frames, const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    ActualUsageCounter& usage,
    const std::function<ObjectReply(std::string_view)>& sink,
    SpeechEncodingReceipt* receipt) {
  auto* http = detail::HttpResponseContext::Current(fd);
  const auto previous = usage.Total();
  if (!previous || !http || !receipt)
    return Error(500, "tts_usage_invalid", "TTS prior usage unavailable");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Speech synthesis cancelled");
  };
  {
    RequestDiagnostics::Scope wait(diagnostic,
                                   RequestDiagnostics::Wait::kStorage);
    auto loaded = EnsureNativeTts(true, {{}, control->deadline(), cancelled},
                                  &diagnostic);
    if (!loaded.ok()) return loaded;
  }
  int slot;
  {
    RequestDiagnostics::Scope wait(diagnostic, RequestDiagnostics::Wait::kSlot);
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
                 "TTS sequence admission unavailable");
  diagnostic.Admitted(slot);
  audio::TtsSynthesisRun synthesis;
  uint64_t committed = 0;
  usage.BeginStep();
  const auto account = [&] {
    usage.Observe({committed, synthesis.accepted_tokens()});
    rate.StartPrompt(previous->input + committed);
    rate.Completion(previous->output + synthesis.accepted_tokens());
  };
  ScopeExit accounting([&] {
    account();
    usage.EndStep();
    metrics_.prompt_tokens_total.fetch_add(committed);
    metrics_.generation_tokens_total.fetch_add(synthesis.accepted_tokens());
    FreeSeqId(slot);
    diagnostic.Released();
  });
  TtsResourcePlan resources;
  if (!PlanTtsServiceResources(tts_limits_, prompt.text_ids.size(),
                               prompt.prefill.size(), max_frames, &resources))
    return Error(400, "tts_workspace_limit", "TTS workspace budget exceeded");
  uint8_t* generation = nullptr;
  uint8_t* codec = nullptr;
  bool cleaned = true;
  ScopeExit release([&] {
    synthesis.Cancel();
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const bool had = generation || codec;
    for (auto** pointer : {&generation, &codec}) {
      const auto freed = detail::ReleaseCudaMemory(
          *pointer, "native_tts.workspace_free", control->id().c_str());
      RecordDeviceMemoryFailure(freed, "native_tts.workspace_free");
      cleaned &= freed.ok();
    }
    if (!cleaned) {
      tts_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
      AbortBackendExecution();
    } else if (had) {
      work.Completed();
    }
  });
  const auto failed = [&](AsrFailure kind, const char* message) {
    tts_health_.ComputeFailed(kind, Clock::now());
    if (kind == AsrFailure::kDevice) AbortBackendExecution();
    return Error(503, TtsFailureCode(kind), message);
  };
  const auto perform = [&](ProgressStage stage, const auto& execute) {
    std::unique_lock<std::mutex> gpu;
    {
      RequestDiagnostics::Scope wait(diagnostic,
                                     RequestDiagnostics::Wait::kModel);
      gpu = LockUnlessCancelled(model_mu_, cancelled);
    }
    if (!gpu.owns_lock() || cancelled()) return interrupted();
    const auto shared = ObserveHealth();
    if (!shared.Healthy())
      return Error(503, shared.ErrorCode(), "Shared backend unavailable");
    RequestDiagnostics::ExecutionScope work(diagnostic, stage);
    OnException drain([&] {
      if (cudaStreamSynchronize(nullptr) != cudaSuccess) {
        tts_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
        AbortBackendExecution();
      }
    });
    const auto result = execute();
    account();
    if (!result.ok()) {
      if (result.device_error != cudaSuccess)
        return failed(AsrFailure::kDevice, "TTS device execution failed");
      if (synthesis.at_limit() && !synthesis.ended())
        return Error(502, "tts_output_limit",
                     "TTS frame budget exhausted before EOS");
      return failed(AsrFailure::kCompute, "TTS execution failed");
    }
    work.Completed();
    metrics_.execution_steps_total.fetch_add(1);
    return ObjectReply{};
  };
  {
    auto gpu = LockUnlessCancelled(model_mu_, cancelled);
    if (!gpu.owns_lock() || cancelled()) return interrupted();
    const auto shared = ObserveHealth();
    if (!shared.Healthy())
      return Error(503, shared.ErrorCode(), "Shared backend unavailable");
    if (!runtime::ReadSystemMemory().CanAllocate(
            resources.request_device_bytes + resources.request_host_bytes))
      return failed(AsrFailure::kResource, "TTS request memory unavailable");
    RequestDiagnostics::ExecutionScope work(diagnostic, ProgressStage::kCache);
    for (const auto& [pointer, bytes] :
         {std::pair{&generation, resources.generation_bytes},
          std::pair{&codec, resources.codec_bytes}}) {
      const auto memory = detail::AllocateCudaMemory(
          *pointer, bytes, "native_tts.workspace", control->id().c_str());
      RecordDeviceMemoryFailure(memory, "native_tts.workspace");
      if (!memory.ok())
        return failed(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "TTS workspace allocation unavailable");
    }
    work.Completed();
  }
  audio::TtsSampling sampling;
  sampling.sample = false;
  auto reply = perform(ProgressStage::kPrefill, [&] {
    auto result = synthesis.Begin(tts_weights_.talker, tts_weights_.codec,
                                  prompt, max_frames, 16, sampling, sampling, 0,
                                  {generation, resources.generation_bytes},
                                  {codec, resources.codec_bytes}, nullptr,
                                  request.format == SpeechFormat::kWav);
    if (result.ok()) committed = prompt.text_ids.size();
    return result;
  });
  if (!reply.ok()) return reply;
  size_t read_offset = 0;
  SpeechPcmSource source;
  source.final_samples = [&]() -> std::optional<size_t> {
    if (!synthesis.ended()) return std::nullopt;
    return synthesis.frames() * audio::TtsConfig::kSamplesPerFrame;
  };
  source.next = [&](std::vector<float>* out, bool* final) -> ObjectReply {
    if (cancelled()) return interrupted();
    while (!synthesis.output_ready() && !synthesis.done()) {
      auto result =
          perform(ProgressStage::kDecode, [&] { return synthesis.Advance(); });
      if (!result.ok()) return result;
    }
    if (synthesis.done()) {
      out->clear();
      *final = true;
      return {};
    }
    // Split one qualified 16-frame codec chunk into bounded tempo inputs;
    // acknowledgement occurs only after its final actual sample is copied.
    const auto device = synthesis.OutputDevicePcm().subspan(read_offset);
    const size_t count = std::min<size_t>(24000, device.size());
    out->resize(count);
    auto result = perform(ProgressStage::kOutput, [&] {
      auto copied =
          cudaMemcpyAsync(out->data(), device.data(), count * sizeof(float),
                          cudaMemcpyDeviceToHost, nullptr);
      const auto drained = cudaStreamSynchronize(nullptr);
      if (copied == cudaSuccess) copied = drained;
      return audio::TtsComputeResult{
          copied == cudaSuccess ? Status{}
                                : Status::Fail("TTS PCM readback failed"),
          copied};
    });
    if (!result.ok()) return result;
    read_offset += count;
    if (read_offset == synthesis.OutputDevicePcm().size()) {
      if (!synthesis.AcknowledgeOutput())
        return failed(AsrFailure::kCompute, "TTS PCM acknowledgement failed");
      read_offset = 0;
    }
    *final = false;
    return {};
  };
  SpeechEncodeLimits limits;
  const auto input_samples = max_frames * audio::TtsConfig::kSamplesPerFrame;
  limits.max_samples = input_samples * 4;
  limits.max_output_bytes = uint64_t(tts_limits_.max_output_mib) << 20;
  const auto& client = http->Client();
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  SpeechEncodingReceipt pending;
  reply = WriteSpeechAudio(request, input_samples, limits,
                           {scope, control->deadline(), cancelled}, source,
                           sink, &pending);
  if (!reply.ok()) return reply;
  release.Run();
  if (!cleaned)
    return Error(503, "gpu_unhealthy", "TTS workspace cleanup failed");
  if (cancelled()) return interrupted();
  tts_health_.ComputeSucceeded();
  *receipt = std::move(pending);
  return {};
}
}  // namespace q4t::server
