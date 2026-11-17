// Incremental trained ASR output -> transcription text/events. No GPU access.
#pragma once

#include "q4t/server/actual_usage.h"
#include "q4t/server/token_logprobs.h"

namespace q4t::server {
// The API's temperature=0 uses confidence adaptation. These explicit native
// thresholds are implementation policy, not claimed OpenAI internal values.
class AsrTemperature {
 public:
  explicit AsrTemperature(double requested)
      : adaptive_(requested == 0), temperature_(requested) {}
  void Observe(double logprob);
  double Value() const { return temperature_; }

 private:
  bool adaptive_;
  double temperature_;
  double sum_ = 0;
  int count_ = 0, increases_ = 0;
};

struct AsrTextDelta {
  std::string text;
  std::string logprobs = "[]";
};

class AsrTranscript {
 public:
  explicit AsrTranscript(bool logprobs, std::string forced_language = {});
  // bytes are the actual selected token's decoded bytes. EOS is accounted by
  // the caller and never passed as content. Malformed UTF-8 uses the shared
  // stream repair; logprobs retain original bytes and real selected values.
  Status Append(const SampledToken& token, std::string_view bytes,
                AsrTextDelta* delta);
  // Only call after actual model EOS. Budget/time exhaustion is an error,
  // never a successful done event. One terminal; failures remain sticky.
  Status Finish(AsrTextDelta* delta);
  const std::string& Text() const { return text_; }
  const std::string& Language() const { return language_; }
  bool Complete() const { return complete_; }
  std::string DeltaJson(const AsrTextDelta& delta) const;
  Status ResultJson(const ActualUsage& usage, uint64_t audio_input_tokens,
                    bool stream, std::string* json) const;

 private:
  Status Consume(bool final, AsrTextDelta* delta);
  Status Fail(std::string message);
  bool logprobs_ = false, body_ = false, text_started_ = false;
  bool complete_ = false;
  size_t offset_ = 0, raw_bytes_ = 0;
  std::string pending_, text_, language_;
  Status status_;
  detail::Utf8Stream utf8_;
  TokenLogprobs probabilities_;
};
}  // namespace q4t::server
