#include "chat_server_internal.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
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
    ActualUsageCounter& usage, AsrTranscript* transcript,
    std::string_view leading_text) {
  auto* http = detail::HttpResponseContext::Current(fd);
  const auto previous = usage.Total();
  if (!previous || !http || !transcript)
    return Error(500, "asr_usage_invalid", "ASR prior usage unavailable");
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
    rate.StartPrompt(previous->input + committed);
    rate.Completion(previous->output + generated);
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
        std::max(prompt.tokens.size() + prompt.output_tokens - 1,
                 prompt.no_speech_prefix.size()),
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
    rate.StartPrompt(previous->input + committed);
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
  std::vector<uint16_t> logits(C::kVocab);
  // Selection excludes checkpoint padding rows without changing the raw
  // probability denominator of the actual full model head.
  std::vector<uint8_t> allowed(C::kVocab, 0);
  std::fill_n(allowed.begin(), asr_tok_->VocabSize(), uint8_t{1});
  const auto decode = [&](std::span<const int32_t> prefix,
                          uint64_t input_total) {
    audio::AsrDecoderStep decoder;
    auto result = perform(ProgressStage::kDecode, [&] {
      return decoder.Begin(asr_weights_, prefix, prompt.audio_start, audio,
                           decoder_memory, decoder_layout.bytes);
    });
    if (!result.ok()) return result;
    committed = input_total;
    rate.StartPrompt(previous->input + committed);
    usage.Observe({committed, generated});
    for (int layer = 0; layer < C::kDecoderLayers; ++layer) {
      result =
          perform(ProgressStage::kDecode, [&] { return decoder.NextLayer(); });
      if (!result.ok()) return result;
    }
    return perform(ProgressStage::kDecode,
                   [&] { return decoder.Finish(logits); });
  };
  uint64_t prior_input = 0;
  std::optional<double> no_speech;
  if (request.segment_timestamps) {
    diagnostic.Stage("asr_no_speech_score");
    if (prompt.no_speech_prefix.empty() || prompt.no_speech_token != 2240)
      return Error(500, "asr_label_invalid", "ASR label prefix unavailable");
    reply = decode(prompt.no_speech_prefix, prompt.no_speech_prefix.size());
    if (!reply.ok()) return reply;
    if (cancelled()) return interrupted();
    SamplingOptions label_options;
    label_options.temperature = 0;
    label_options.logprobs = true;
    TokenSampler label_sampler;
    SampledToken label;
    auto scored = label_sampler.Init(label_options, C::kVocab, 0);
    if (scored)
      scored = label_sampler.Select(logits, &label, {}, prompt.no_speech_token);
    if (scored) {
      no_speech = std::exp(label.raw_logprob);
      scored = transcript->SetNoSpeechProbability(*no_speech);
    }
    if (!scored)
      return Error(502, "asr_label_invalid", "ASR no-speech scoring failed");
    // Bounded numeric diagnostics only. Logging failure never changes model
    // health, the request outcome or the actual posterior used below.
    std::fprintf(stderr,
                 "[asr-no-speech] request=%s token=%d raw_logprob=%.17g "
                 "probability=%.17g input=%llu\n",
                 control->id().c_str(), label.token, label.raw_logprob,
                 *no_speech, static_cast<unsigned long long>(committed));
    prior_input = committed;
  }
  SamplingOptions options;
  options.temperature = request.temperature;
  options.logprobs = true;
  std::random_device entropy;
  const auto seed = (uint64_t(entropy()) << 32) ^ entropy();
  AsrSegmentTemperature segment_temperature(request.temperature);
  const detail::WriteOptions write{control->deadline(), control.get()};
  bool first_delta = true;
  const auto emit = [&](const AsrTextDelta& delta) -> bool {
    if (!request.stream || delta.text.empty()) return !cancelled();
    if (*http->State() == detail::ResponseState::kUnsent) {
      if (!detail::BeginStreamingResponse(fd, http->State(), write))
        return false;
      http->StartHeartbeats(control);
    }
    auto outgoing = delta;
    if (first_delta) outgoing.text.insert(0, leading_text);
    first_delta = false;
    return detail::WriteAll(fd,
                            "event: transcript.text.delta\r\ndata: " +
                                transcript->DeltaJson(outgoing) + "\r\n\r\n",
                            write);
  };
  for (unsigned attempt = 0;; ++attempt) {
    TokenSampler sampler;
    auto sampled = sampler.Init(options, C::kVocab, seed, attempt);
    if (!sampled)
      return failed(AsrFailure::kCompute, "ASR sampler unavailable");
    AsrTemperature temperature(request.temperature);
    auto prefix = prompt.tokens;
    bool eos = false;
    diagnostic.Stage("decode");
    while (generated < prompt.output_tokens) {
      reply = decode(prefix, prior_input + prompt.tokens.size());
      if (!reply.ok()) return reply;
      if (cancelled()) return interrupted();
      SampledToken token;
      const double selected_temperature = request.segment_timestamps
                                              ? segment_temperature.Value()
                                              : temperature.Value();
      sampled = sampler.SetTemperature(selected_temperature);
      if (sampled) sampled = sampler.Select(logits, &token, allowed);
      if (sampled) sampled = sampler.Observe(token.token);
      if (!sampled)
        return failed(AsrFailure::kCompute, "ASR token selection failed");
      temperature.Observe(token.raw_logprob);
      ++generated;
      rate.Completion(previous->output + generated);
      usage.Observe({committed, generated});
      if (token.token == C::kEos || token.token == C::kImEnd) {
        eos = true;
        break;
      }
      std::string bytes;
      sampled =
          asr_tok_->Decode({static_cast<uint32_t>(token.token)}, false, &bytes);
      AsrTextDelta delta;
      if (sampled)
        sampled =
            transcript->Append(token, bytes, &delta, selected_temperature);
      if (!sampled) return Error(502, "asr_output_invalid", sampled.message());
      if (!emit(delta)) return interrupted();
      prefix.push_back(token.token);
    }
    if (!eos)
      return Error(502, "asr_output_limit",
                   "ASR cumulative output budget exhausted before EOS");
    AsrTextDelta final;
    sampled = transcript->Finish(&final);
    if (!sampled) return Error(502, "asr_output_invalid", sampled.message());
    if (!emit(final)) return interrupted();
    if (!request.segment_timestamps ||
        !segment_temperature.Retry(transcript->TextTokens()))
      break;
    if (generated == prompt.output_tokens)
      return Error(502, "asr_output_limit",
                   "ASR output budget exhausted before confidence retry");
    // The buffered attempt was never published. Reuse its encoder only;
    // every new decoder input and generated token remains charged, with the
    // original RPM admission, output ceiling and request deadline.
    if (const auto* limits = http->Client().Limits();
        limits && (prompt.tokens.size() > limits->max_prompt_tokens ||
                   previous->input + committed >
                       limits->max_prompt_tokens - prompt.tokens.size()))
      return Error(400, "client_prompt_limit_exceeded",
                   "ASR confidence retry exceeds client input limit");
    const auto reserved =
        rate.ExtendPrompt(clients_, http->Client(), prompt.tokens.size());
    if (reserved != ClientRateResult::kAllowed)
      return Error(reserved == ClientRateResult::kTpm ? 429 : 503,
                   reserved == ClientRateResult::kTpm ? "client_tpm_exhausted"
                   : reserved == ClientRateResult::kFrozen
                       ? "service_draining"
                       : "client_rate_invalid",
                   "ASR confidence retry quota unavailable");
    prior_input = committed;
    *transcript = AsrTranscript(false, request.language, true);
    sampled = transcript->SetNoSpeechProbability(*no_speech);
    if (!sampled) return Error(502, "asr_label_invalid", sampled.message());
  }
  release.Run();
  if (!cleaned)
    return Error(503, "gpu_unhealthy", "ASR workspace cleanup failed");
  if (cancelled()) return interrupted();
  asr_health_.ComputeSucceeded();
  return {};
}
}  // namespace q4t::server
