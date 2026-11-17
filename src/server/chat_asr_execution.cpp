#include "chat_server_internal.h"

#include <algorithm>
#include <random>

#include "cuda_memory_backend.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using C = audio::AsrConfig;
using object_detail::Error;
constexpr uint64_t kMargin = 512ULL << 20;
}  // namespace

ObjectReply ChatServer::RunNativeAsr(
    int fd, const AsrRequest& request, const audio::AsrFeatures& features,
    const AsrPrompt& prompt, const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    ActualUsageCounter& usage, AsrTranscript* transcript) {
  auto* http = detail::HttpResponseContext::Current(fd);
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
    auto loaded = EnsureNativeAsr(true, {{}, control->deadline(), cancelled},
                                  &diagnostic);
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
                 "ASR sequence admission unavailable");
  diagnostic.Admitted(slot);
  uint64_t committed = 0, generated = 0;
  usage.BeginStep();
  ScopeExit accounting([&] {
    usage.Observe({committed, generated});
    usage.EndStep();
    rate.StartPrompt(committed);
    rate.Completion(generated);
    metrics_.prompt_tokens_total.fetch_add(committed);
    metrics_.generation_tokens_total.fetch_add(generated);
    FreeSeqId(slot);
    diagnostic.Released();
  });
  audio::AsrEncoderLayout encoder_layout;
  audio::AsrDecoderLayout decoder_layout;
  const size_t budget = uint64_t(asr_limits_.workspace_mib) << 20;
  auto planned = audio::PlanAsrEncoderWorkspace(
      features.frames, size_t(asr_limits_.max_audio_seconds) * 100, budget,
      &encoder_layout);
  if (planned)
    planned = audio::PlanAsrDecoderWorkspace(
        prompt.tokens.size() + prompt.output_tokens - 1,
        asr_limits_.max_context_tokens, budget - encoder_layout.bytes,
        &decoder_layout);
  if (!planned)
    return Error(400, "asr_workspace_limit", "ASR workspace budget exceeded");
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
          *pointer, "native_asr.workspace_free", control->id().c_str());
      RecordDeviceMemoryFailure(freed, "native_asr.workspace_free");
      cleaned &= freed.ok();
    }
    if (!cleaned) {
      asr_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
      AbortBackendExecution();
    } else if (had)
      work.Completed();
  });
  const auto failed = [&](AsrFailure kind, const char* message) {
    asr_health_.ComputeFailed(kind, Clock::now());
    if (kind == AsrFailure::kDevice) AbortBackendExecution();
    return Error(503, AsrFailureCode(kind), message);
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
        asr_health_.ComputeFailed(AsrFailure::kDevice, Clock::now());
        AbortBackendExecution();
      }
    });
    const auto result = execute();
    if (!result.ok())
      return failed(result.device_error == cudaSuccess ? AsrFailure::kCompute
                                                       : AsrFailure::kDevice,
                    "Native ASR execution failed");
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
      return failed(AsrFailure::kResource, "ASR workspace memory unavailable");
    RequestDiagnostics::ExecutionScope work(diagnostic, ProgressStage::kCache);
    for (const auto& [pointer, bytes] :
         {std::pair{&encoder_memory, encoder_layout.bytes},
          std::pair{&decoder_memory, decoder_layout.bytes}}) {
      const auto memory = detail::AllocateCudaMemory(
          *pointer, bytes, "native_asr.workspace", control->id().c_str());
      RecordDeviceMemoryFailure(memory, "native_asr.workspace");
      if (!memory.ok())
        return failed(
            memory.exhausted() ? AsrFailure::kResource : AsrFailure::kDevice,
            "ASR workspace allocation unavailable");
    }
    work.Completed();
  }
  audio::AsrEncoderRun encoder;
  diagnostic.Stage("prefill");
  auto reply = perform(ProgressStage::kPrefill, [&] {
    return encoder.Begin(asr_weights_, features.mel, features.frames,
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
    usage.Observe({committed, generated});
    rate.StartPrompt(committed);
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
    return failed(AsrFailure::kCompute, "ASR audio layout changed");
  auto prefix = prompt.tokens;
  std::vector<uint16_t> logits(C::kVocab);
  // Selection excludes checkpoint padding rows without changing the raw
  // probability denominator of the actual full model head.
  std::vector<uint8_t> allowed(C::kVocab, 0);
  std::fill_n(allowed.begin(), asr_tok_->VocabSize(), uint8_t{1});
  SamplingOptions options;
  options.temperature = request.temperature;
  options.logprobs = true;
  TokenSampler sampler;
  std::random_device entropy;
  auto sampled =
      sampler.Init(options, C::kVocab, (uint64_t(entropy()) << 32) ^ entropy());
  if (!sampled) return failed(AsrFailure::kCompute, "ASR sampler unavailable");
  AsrTemperature temperature(request.temperature);
  const detail::WriteOptions write{control->deadline(), control.get()};
  const auto emit = [&](const AsrTextDelta& delta) -> bool {
    if (!request.stream || delta.text.empty()) return !cancelled();
    if (*http->State() == detail::ResponseState::kUnsent) {
      if (!detail::BeginStreamingResponse(fd, http->State(), write))
        return false;
      http->StartHeartbeats(control);
    }
    return detail::WriteAll(fd,
                            "event: transcript.text.delta\r\ndata: " +
                                transcript->DeltaJson(delta) + "\r\n\r\n",
                            write);
  };
  bool eos = false;
  diagnostic.Stage("decode");
  for (size_t index = 0; index < prompt.output_tokens; ++index) {
    audio::AsrDecoderStep decoder;
    reply = perform(ProgressStage::kDecode, [&] {
      return decoder.Begin(asr_weights_, prefix, prompt.audio_start, audio,
                           decoder_memory, decoder_layout.bytes);
    });
    if (!reply.ok()) return reply;
    committed = prompt.tokens.size();
    rate.StartPrompt(committed);
    usage.Observe({committed, generated});
    for (int layer = 0; layer < C::kDecoderLayers; ++layer) {
      reply =
          perform(ProgressStage::kDecode, [&] { return decoder.NextLayer(); });
      if (!reply.ok()) return reply;
    }
    reply =
        perform(ProgressStage::kDecode, [&] { return decoder.Finish(logits); });
    if (!reply.ok()) return reply;
    if (cancelled()) return interrupted();
    SampledToken token;
    sampled = sampler.SetTemperature(temperature.Value());
    if (sampled) sampled = sampler.Select(logits, &token, allowed);
    if (sampled) sampled = sampler.Observe(token.token);
    if (!sampled)
      return failed(AsrFailure::kCompute, "ASR token selection failed");
    temperature.Observe(token.raw_logprob);
    ++generated;
    rate.Completion(generated);
    usage.Observe({committed, generated});
    if (token.token == C::kEos || token.token == C::kImEnd) {
      eos = true;
      break;
    }
    std::string bytes;
    sampled =
        asr_tok_->Decode({static_cast<uint32_t>(token.token)}, false, &bytes);
    AsrTextDelta delta;
    if (sampled) sampled = transcript->Append(token, bytes, &delta);
    if (!sampled) return Error(502, "asr_output_invalid", sampled.message());
    if (!emit(delta)) return interrupted();
    prefix.push_back(token.token);
  }
  if (!eos)
    return Error(502, "asr_output_limit",
                 "ASR output budget exhausted before EOS");
  AsrTextDelta final;
  sampled = transcript->Finish(&final);
  if (!sampled) return Error(502, "asr_output_invalid", sampled.message());
  if (!emit(final)) return interrupted();
  release.Run();
  if (!cleaned)
    return Error(503, "gpu_unhealthy", "ASR workspace cleanup failed");
  if (cancelled()) return interrupted();
  asr_health_.ComputeSucceeded();
  return {};
}
}  // namespace q4t::server
