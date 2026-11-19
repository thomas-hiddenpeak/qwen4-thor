#include "q4t/server/asr_stream_output.h"

#include <algorithm>
#include <cmath>

#include "q4t/audio/asr_config.h"

namespace q4t::server {
namespace {
Status Invalid() {
  return Status::Fail("streaming ASR output state or budget invalid");
}
}  // namespace
AsrStreamPass::AsrStreamPass(const AsrPrompt& prompt, size_t byte_limit)
    : byte_limit_(byte_limit) {
  valid_ = prompt.output_tokens && prompt.output_tokens <= 4096 &&
           byte_limit_ && byte_limit_ <= 65536 && !prompt.tokens.empty() &&
           prompt.tokens.size() <= 8192 &&
           prompt.output_tokens - 1 <= 8192 - prompt.tokens.size() &&
           prompt.no_speech_prefix.empty() && prompt.audio_tokens &&
           prompt.audio_start <= prompt.tokens.size() &&
           prompt.audio_tokens <= prompt.tokens.size() - prompt.audio_start &&
           prompt.continuation_tokens <= prompt.tokens.size() -
                                             prompt.audio_start -
                                             prompt.audio_tokens &&
           std::ranges::all_of(prompt.tokens, [](int32_t id) {
             return id >= 0 && id < audio::AsrConfig::kVocab;
           });
  if (valid_) prompt_ = prompt;
}
Status AsrStreamPass::Fail() {
  failed_ = true;
  return Invalid();
}
Status AsrStreamPass::Begin(const AsrPrompt& actual) {
  if (!valid_ || failed_ || begun_ || actual.tokens != prompt_.tokens ||
      actual.audio_start != prompt_.audio_start ||
      actual.audio_tokens != prompt_.audio_tokens ||
      actual.output_tokens != prompt_.output_tokens ||
      actual.continuation_tokens != prompt_.continuation_tokens ||
      !actual.no_speech_prefix.empty())
    return Fail();
  begun_ = true;
  return {};
}
Status AsrStreamPass::Observe(const SampledToken& selected,
                              std::string_view bytes, double temperature) {
  using C = audio::AsrConfig;
  if (!valid_ || !begun_ || failed_ || eos_ ||
      generated_ == prompt_.output_tokens || selected.token < 0 ||
      selected.token >= C::kVocab || !std::isfinite(selected.logprob) ||
      selected.logprob > 0 || !std::isfinite(selected.raw_logprob) ||
      selected.raw_logprob > 0 || !selected.top.empty() ||
      !std::isfinite(temperature) || temperature < 0 || temperature > 1 ||
      bytes.size() > byte_limit_ - bytes_)
    return Fail();
  const bool eos = selected.token == C::kEos || selected.token == C::kImEnd;
  if (eos && !bytes.empty()) return Fail();
  if (!eos)
    selected_.push_back({{selected.token, std::string(bytes)},
                         selected.logprob,
                         selected.raw_logprob,
                         temperature});
  bytes_ += bytes.size();
  ++generated_;
  eos_ = eos;
  return {};
}

AsrStreamOutput::AsrStreamOutput(uint64_t epoch, size_t output_tokens,
                                 bool logprobs, std::string language,
                                 AsrStreamPrefixLimits limits)
    : epoch_(epoch),
      limit_(output_tokens),
      prefix_(epoch, limits),
      transcript_(logprobs, std::move(language), true) {
  valid_ = prefix_.Valid() && limit_ && limit_ <= 4096;
}
Status AsrStreamOutput::Fail(std::string message) {
  failed_ = true;
  return Status::Fail(std::move(message));
}
Status AsrStreamOutput::Prepare(AsrPrompt* prompt, size_t context_limit,
                                AsrTranscript* transcript) const {
  if (!valid_ || failed_ || prefix_.Finished() || !prompt || !transcript ||
      context_limit < 128 || context_limit > 8192 ||
      prompt->tokens.size() > context_limit || prompt->output_tokens == 0 ||
      prompt->output_tokens > Remaining() ||
      Prefix().size() > context_limit - prompt->tokens.size() ||
      prompt->output_tokens >
          context_limit - prompt->tokens.size() - Prefix().size() ||
      !prompt->no_speech_prefix.empty() || prompt->continuation_tokens)
    return Invalid();
  // Capacity is checked before either target is mutated. These are private,
  // fresh objects; a failed token mapper is never published as an invocation.
  prompt->tokens.reserve(prompt->tokens.size() + Prefix().size());
  for (const auto& token : Prefix()) {
    AsrTextDelta ignored;
    const auto status = transcript->Append(token.Sample(), token.identity.bytes,
                                           &ignored, token.temperature);
    if (!status) return status;
    prompt->tokens.push_back(token.identity.id);
  }
  prompt->continuation_tokens = Prefix().size();
  return {};
}
Status AsrStreamOutput::EmitTo(size_t count, bool final, const Sink& emit) {
  if (!emit || count < emitted_ || count > latest_.size())
    return Fail("Invalid streaming ASR emission owner");
  while (emitted_ < count) {
    const auto& token = latest_[emitted_];
    AsrTextDelta delta;
    const auto status = transcript_.Append(token.Sample(), token.identity.bytes,
                                           &delta, token.temperature);
    if (!status) return Fail(status.message());
    ++emitted_;
    if (!delta.text.empty() && !emit(delta))
      return Fail("Streaming transcription output interrupted");
  }
  if (final) {
    AsrTextDelta delta;
    const auto status = transcript_.Finish(&delta);
    if (!status) return Fail(status.message());
    if (!delta.text.empty() && !emit(delta))
      return Fail("Streaming transcription output interrupted");
  }
  return {};
}
Status AsrStreamOutput::Accept(const AsrStreamPass& pass, bool final,
                               const Sink& emit) {
  if (!valid_ || failed_ || prefix_.Finished() || !emit || !pass.Complete() ||
      pass.Generated() > Remaining() ||
      pass.Prompt().continuation_tokens != Prefix().size())
    return Fail("Incomplete or excessive actual streaming ASR pass");
  const auto actual_prefix =
      std::span(pass.Prompt().tokens).last(Prefix().size());
  for (size_t i = 0; i < actual_prefix.size(); ++i)
    if (actual_prefix[i] != Prefix()[i].identity.id)
      return Fail("Actual decoder input changed the fixed ASR prefix");
  std::vector<AsrStreamSelection> next(Prefix().begin(), Prefix().end());
  next.insert(next.end(), pass.Selections().begin(), pass.Selections().end());
  std::vector<AsrStreamToken> identities;
  identities.reserve(next.size());
  for (const auto& token : next) identities.push_back(token.identity);
  const auto accepted = prefix_.Accept(epoch_, identities, true, final);
  if (!accepted) return Fail(accepted.message());
  latest_ = std::move(next);
  generated_ += pass.Generated();
  return EmitTo(final ? latest_.size() : Prefix().size(), final, emit);
}
Status AsrStreamOutput::Finalize(const Sink& emit) {
  if (!valid_ || failed_ || Finished() || !emit || !prefix_.Finalize(epoch_))
    return Fail("Streaming ASR finalization lacks an owned actual EOS");
  return EmitTo(latest_.size(), true, emit);
}
}  // namespace q4t::server
