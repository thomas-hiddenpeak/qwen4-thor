// Actual decoder selections and stable, append-only streaming ASR output.
#pragma once

#include <functional>

#include "q4t/server/asr_request.h"
#include "q4t/server/asr_response.h"
#include "q4t/server/asr_stream_state.h"

namespace q4t::server {
struct AsrStreamSelection {
  AsrStreamToken identity;
  double logprob = 0, raw_logprob = 0, temperature = 0;
  SampledToken Sample() const {
    return {identity.id, logprob, {}, raw_logprob};
  }
};

// Filled only at actual native decoder selection sites. EOS consumes output
// budget but never becomes content/prompt. A result is usable only after the
// owner also verifies native execution, cleanup and cancellation succeeded.
class AsrStreamPass {
 public:
  explicit AsrStreamPass(const AsrPrompt& prompt, size_t byte_limit = 65536);
  // Native execution must bind its exact prompt before recording selections.
  Status Begin(const AsrPrompt& actual);
  Status Observe(const SampledToken& selected, std::string_view bytes,
                 double temperature);
  bool Valid() const { return valid_; }
  bool Complete() const { return eos_ && !failed_; }
  size_t Generated() const { return generated_; }
  const AsrPrompt& Prompt() const { return prompt_; }
  std::span<const AsrStreamSelection> Selections() const { return selected_; }

 private:
  Status Fail();
  AsrPrompt prompt_;
  size_t byte_limit_, generated_ = 0, bytes_ = 0;
  bool valid_ = false, begun_ = false, eos_ = false, failed_ = false;
  std::vector<AsrStreamSelection> selected_;
};

// One item owns one output budget and one transcript across real audio passes.
// It reuses the original selected probabilities of forced prefix tokens; only
// newly executed selections are output work. Replayed prefixes are input work
// in the next actual prompt and must be accounted there by the executor.
class AsrStreamOutput {
 public:
  using Sink = std::function<bool(const AsrTextDelta&)>;
  AsrStreamOutput(uint64_t epoch, size_t output_tokens, bool logprobs,
                  std::string language, AsrStreamPrefixLimits limits = {});
  bool Valid() const { return valid_; }
  // Extends a fresh BuildAsrPrompt result and seeds a fresh per-pass
  // transcript. The caller holds the item's settings constant. No tokenizer
  // round trip.
  Status Prepare(AsrPrompt* prompt, size_t context_limit,
                 AsrTranscript* transcript) const;
  Status Accept(const AsrStreamPass& pass, bool final, const Sink& emit);
  // Exact-boundary input EOF reuses the last real EOS without another pass.
  Status Finalize(const Sink& emit);
  const AsrTranscript& Transcript() const { return transcript_; }
  std::span<const AsrStreamSelection> Prefix() const {
    return std::span(latest_).first(prefix_.Prefix().size());
  }
  size_t Generated() const { return generated_; }
  size_t Remaining() const { return limit_ - generated_; }
  bool Finished() const {
    return !failed_ && prefix_.Finished() && transcript_.Complete();
  }

 private:
  Status Fail(std::string message);
  Status EmitTo(size_t count, bool final, const Sink& emit);
  const uint64_t epoch_;
  const size_t limit_;
  AsrStreamPrefix prefix_;
  AsrTranscript transcript_;
  std::vector<AsrStreamSelection> latest_;
  size_t generated_ = 0, emitted_ = 0;
  bool valid_ = false, failed_ = false;
};
}  // namespace q4t::server
