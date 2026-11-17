#include "chat_server_internal.h"

#include <algorithm>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using C = audio::AlignerConfig;
using object_detail::Error;
constexpr uint64_t kMargin = 512ULL << 20;
}  // namespace

ObjectReply ChatServer::RunNativeAligner(
    int fd, const audio::AsrFeatures& features, const AlignerPrompt& prompt,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    ActualUsageCounter& usage, uint64_t prior_input,
    std::vector<uint32_t>* classes) {
  if (!classes)
    return Error(500, "alignment_owner_invalid", "Alignment owner unavailable");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Transcription execution cancelled");
  };
  {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
    auto loaded = EnsureNativeAligner(
        true, {{}, control->deadline(), cancelled}, &diagnostic);
    if (!loaded.ok()) return loaded;
  }
  diagnostic.Stage("slot_admission");
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
                 "alignment sequence admission unavailable");
  diagnostic.Admitted(slot);
  uint64_t committed = 0;
  usage.BeginStep();
  ScopeExit accounting([&] {
    usage.Observe({committed, 0});
    usage.EndStep();
    rate.StartPrompt(prior_input + committed);
    metrics_.prompt_tokens_total.fetch_add(committed);
    FreeSeqId(slot);
    diagnostic.Released();
  });
  audio::AsrEncoderLayout encoder_layout;
  audio::AlignerDecoderLayout decoder_layout;
  const size_t budget = uint64_t(aligner_limits_.workspace_mib) << 20;
  auto planned = audio::PlanAlignerEncoderWorkspace(
      features.frames, size_t(aligner_limits_.max_audio_seconds) * 100, budget,
      &encoder_layout);
  if (planned)
    planned = audio::PlanAlignerDecoderWorkspace(
        prompt.tokens.size(), prompt.positions.size(),
        aligner_limits_.max_context_tokens, budget - encoder_layout.bytes,
        &decoder_layout);
  if (!planned)
    return Error(400, "alignment_workspace_limit",
                 "alignment workspace budget exceeded");
  uint8_t* encoder_memory = nullptr;
  uint8_t* decoder_memory = nullptr;
  bool cleaned = true;
  ScopeExit release([&] {
    const std::lock_guard lock(model_mu_);
    RequestDiagnostics::ExecutionScope work(diagnostic,
                                            ProgressStage::kCleanup);
    const bool had = encoder_memory || decoder_memory;
    for (auto* pointer : {&encoder_memory, &decoder_memory}) {
      const auto freed = detail::ReleaseCudaMemory(
          *pointer, "native_aligner.workspace_free", control->id().c_str());
      RecordDeviceMemoryFailure(freed, "native_aligner.workspace_free");
      cleaned &= freed.ok();
    }
    if (!cleaned) {
      aligner_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
      AbortBackendExecution();
    } else if (had)
      work.Completed();
  });
  const auto failed = [&](AsrFailure kind, const char* message) {
    aligner_health_.ComputeFailed(kind, Clock::now());
    if (kind == AsrFailure::kDevice) AbortBackendExecution();
    return Error(503, AlignerFailureCode(kind), message);
  };
  const auto perform = [&](ProgressStage stage, const auto& execute) {
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
    RequestDiagnostics::ExecutionScope work(diagnostic, stage);
    OnException drain([&] {
      if (cudaStreamSynchronize(nullptr) != cudaSuccess) {
        aligner_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
        AbortBackendExecution();
      }
    });
    const auto result = execute();
    if (!result.ok())
      return failed(result.device_error == cudaSuccess ? AsrFailure::kCompute
                                                       : AsrFailure::kDevice,
                    "Native alignment execution failed");
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
            encoder_layout.bytes + decoder_layout.bytes + kMargin))
      return failed(AsrFailure::kResource,
                    "alignment workspace memory unavailable");
    RequestDiagnostics::ExecutionScope work(diagnostic, ProgressStage::kCache);
    for (const auto& [pointer, bytes] :
         {std::pair{&encoder_memory, encoder_layout.bytes},
          std::pair{&decoder_memory, decoder_layout.bytes}}) {
      const auto memory = detail::AllocateCudaMemory(
          *pointer, bytes, "native_aligner.workspace", control->id().c_str());
      RecordDeviceMemoryFailure(memory, "native_aligner.workspace");
      if (!memory.ok())
        return failed(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "alignment workspace allocation unavailable");
    }
    work.Completed();
  }
  audio::AlignerEncoderRun encoder;
  diagnostic.Stage("prefill");
  auto reply = perform(ProgressStage::kPrefill, [&] {
    return encoder.Begin(aligner_weights_, features.mel, features.frames,
                         encoder_memory, encoder_layout.bytes);
  });
  if (!reply.ok()) return reply;
  for (int i = 0; i < encoder_layout.chunks; ++i) {
    reply =
        perform(ProgressStage::kPrefill, [&] { return encoder.NextChunk(); });
    if (!reply.ok()) return reply;
    // Count only actually computed audio positions. The final partial chunk
    // has its trained rounded output, not an invented full 13-token chunk.
    committed = std::min<size_t>(prompt.audio_tokens, size_t(i + 1) * 13);
    usage.Observe({committed, 0});
    rate.StartPrompt(prior_input + committed);
  }
  for (int i = 0; i < C::kEncoderLayers; ++i) {
    reply =
        perform(ProgressStage::kPrefill, [&] { return encoder.NextLayer(); });
    if (!reply.ok()) return reply;
  }
  reply = perform(ProgressStage::kPrefill, [&] { return encoder.Finish(); });
  if (!reply.ok()) return reply;
  const auto audio = encoder.output();
  if (audio.size() != prompt.audio_tokens * C::kHidden)
    return failed(AsrFailure::kCompute, "alignment audio layout changed");
  audio::AlignerDecoderRun decoder;
  reply = perform(ProgressStage::kPrefill, [&] {
    return decoder.Begin(aligner_weights_, prompt.tokens, 1, audio,
                         decoder_memory, decoder_layout.bytes);
  });
  if (!reply.ok()) return reply;
  committed = prompt.tokens.size();
  usage.Observe({committed, 0});
  rate.StartPrompt(prior_input + committed);
  for (int i = 0; i < C::kDecoderLayers; ++i) {
    reply =
        perform(ProgressStage::kPrefill, [&] { return decoder.NextLayer(); });
    if (!reply.ok()) return reply;
  }
  std::vector<uint16_t> logits(prompt.positions.size() * C::kClasses);
  reply =
      perform(ProgressStage::kPrefill, [&] { return decoder.Finish(logits); });
  if (!reply.ok()) return reply;
  std::vector<uint32_t> selected;
  const auto status = SelectAlignerClasses(logits, prompt.positions.size() / 2,
                                           &selected, cancelled);
  if (!status) {
    if (cancelled()) return interrupted();
    return failed(AsrFailure::kCompute, "Alignment classification invalid");
  }
  release.Run();
  if (!cleaned)
    return Error(503, "gpu_unhealthy", "Alignment workspace cleanup failed");
  if (cancelled()) return interrupted();
  aligner_health_.ComputeSucceeded();
  *classes = std::move(selected);
  return {};
}
}  // namespace q4t::server
