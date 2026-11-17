#include "chat_server_internal.h"

#include <array>

#include "object_protocol.h"
#include "q4t/server/asr_alignment_output.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::FormatAlignedTranscription(
    int fd, const AsrRequest& request, const audio::AsrFeatures& features,
    int64_t duration_ms, const AsrTranscript& transcript,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, ClientRateLease& rate,
    ActualUsageCounter& usage, std::string* response, int64_t origin_ms,
    size_t first_cue, bool header, size_t* cue_count) {
  using object_detail::Error;
  auto* http = detail::HttpResponseContext::Current(fd);
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Alignment cancelled");
  };
  if (!http || !response || !transcript.Complete())
    return Error(500, "alignment_owner_invalid", "Alignment owner unavailable");
  constexpr std::array<std::string_view, 11> languages{
      "zh", "yue", "en", "de", "es", "fr", "it", "pt", "ru", "ko", "ja"};
  if (!transcript.Text().empty() &&
      std::find(languages.begin(), languages.end(), transcript.Language()) ==
          languages.end())
    return Error(502, "alignment_language_unavailable",
                 "Detected language is outside the alignment model contract");
  diagnostic.Stage("alignment_tokenization");
  std::vector<audio::AlignerWord> words;
  auto status = audio::SplitAlignerWords(
      transcript.Text(), transcript.Language(),
      {.max_words = size_t(aligner_limits_.max_words)}, &words, cancelled);
  if (!status) {
    if (cancelled()) return interrupted();
    return Error(502, "alignment_word_limit", status.message());
  }
  audio::AlignerTimes times;
  if (!words.empty()) {
    AlignerPrompt prompt;
    status = BuildAlignerPrompt(
        words, audio::AsrEncodedFrames(features.frames), aligner_limits_,
        [&](std::string_view text, std::vector<uint32_t>* ids) {
          RequestDiagnostics::Scope waiting(
              diagnostic, RequestDiagnostics::Wait::kTokenizer);
          auto lock = LockUnlessCancelled(aligner_tok_mu_, cancelled);
          if (!lock.owns_lock())
            return Status::Fail("Alignment tokenization cancelled");
          return aligner_tok_->Encode(
              text, ids,
              {.cancelled = cancelled, .recognize_special_tokens = false});
        },
        &prompt, cancelled);
    if (!status) {
      if (cancelled()) return interrupted();
      return Error(502, "alignment_context_limit", status.message());
    }
    const auto prior = usage.Total();
    if (!prior)
      return Error(502, "asr_usage_invalid", "Transcription usage unavailable");
    if (const auto* limits = http->Client().Limits();
        limits &&
        (prompt.tokens.size() > limits->max_prompt_tokens ||
         prior->input > limits->max_prompt_tokens - prompt.tokens.size()))
      return Error(400, "client_prompt_limit_exceeded",
                   "Transcription and alignment exceed client input limit");
    const auto reserved =
        rate.ExtendPrompt(clients_, http->Client(), prompt.tokens.size());
    if (reserved != ClientRateResult::kAllowed)
      return Error(reserved == ClientRateResult::kTpm ? 429 : 503,
                   reserved == ClientRateResult::kTpm ? "client_tpm_exhausted"
                   : reserved == ClientRateResult::kFrozen
                       ? "service_draining"
                       : "client_rate_invalid",
                   "Alignment quota unavailable");
    std::vector<uint32_t> classes;
    auto reply = RunNativeAligner(fd, features, prompt, control, diagnostic,
                                  rate, usage, prior->input, &classes);
    if (!reply.ok()) return reply;
    status = audio::RepairAlignerTimestamps(classes, words.size(), duration_ms,
                                            &times, cancelled);
    if (!status) {
      if (cancelled()) return interrupted();
      return Error(502, "alignment_prediction_invalid", status.message());
    }
  }
  if (cancelled()) return interrupted();
  // Preserve raw and fixed model boundaries in the request owner until the
  // response is composed. Never substitute proportional timings on failure.
  if (request.segment_timestamps) {
    diagnostic.Stage("asr_segment_statistics");
    AsrCompression compression;
    GrammarControl statistics;
    statistics.cancelled = cancelled;
    statistics.compile_deadline = control->deadline();
    GrammarFailure cause;
    {
      RequestDiagnostics::Scope waiting(diagnostic,
                                        RequestDiagnostics::Wait::kStorage);
      status = MeasureAsrCompression(transcript.Text(), &compression,
                                     std::move(statistics), &cause);
    }
    if (!status) {
      if (cancelled()) return interrupted();
      // An auxiliary formatting dependency failure is request-local. It must
      // not degrade ASR/Chat readiness or trigger a model restart.
      return Error(503,
                   cause == GrammarFailure::kDependency
                       ? "asr_statistics_dependency_unavailable"
                       : "asr_statistics_unavailable",
                   status.message());
    }
    status =
        AlignedAsrSegmentsJson(transcript, duration_ms, words, times,
                               compression, request.word_timestamps, response);
  } else if (request.format == AsrFormat::kVerboseJson) {
    status = AlignedAsrWordsJson(transcript.Text(),
                                 AsrLanguageName(transcript.Language()),
                                 duration_ms, words, times, response);
  } else {
    status = AlignedAsrSubtitle(transcript.Text(), duration_ms, words, times,
                                request.format == AsrFormat::kVtt, response,
                                origin_ms, first_cue, header, cue_count);
  }
  if (!status) return Error(502, "alignment_output_invalid", status.message());
  return {};
}
}  // namespace q4t::server
