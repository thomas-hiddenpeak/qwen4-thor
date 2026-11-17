// Incremental trained ASR output -> transcription text/events. No GPU access.
#pragma once

#include <deque>
#include <limits>
#include <optional>

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

struct AsrTextToken {
  int32_t id = -1;
  double logprob = 0, temperature = 0;
  // Actual selected token's intersection with the visible normalized text.
  // A token crossing a boundary keeps its original ID and probability.
  size_t begin = 0, end = 0;
};

class AsrTranscript {
 public:
  explicit AsrTranscript(bool logprobs, std::string forced_language = {},
                         bool statistics = false);
  // bytes are the actual selected token's decoded bytes. EOS is accounted by
  // the caller and never passed as content. Malformed UTF-8 uses the shared
  // stream repair; logprobs retain original bytes and real selected values.
  Status Append(const SampledToken& token, std::string_view bytes,
                AsrTextDelta* delta, double selected_temperature = 0);
  // Only call after actual model EOS. Budget/time exhaustion is an error,
  // never a successful done event. One terminal; failures remain sticky.
  Status Finish(AsrTextDelta* delta);
  const std::string& Text() const { return text_; }
  const std::string& Language() const { return language_; }
  bool Complete() const { return complete_; }
  std::span<const AsrTextToken> TextTokens() const { return text_tokens_; }
  // Must come from the model's trained no-speech label posterior, not a
  // constant, empty-transcript heuristic or average frame-VAD probability.
  Status SetNoSpeechProbability(double probability);
  std::optional<double> NoSpeechProbability() const { return no_speech_; }
  std::string DeltaJson(const AsrTextDelta& delta) const;
  Status ResultJson(const ActualUsage& usage, uint64_t audio_input_tokens,
                    bool stream, std::string* json) const;

 private:
  Status Consume(bool final, AsrTextDelta* delta);
  Status Fail(std::string message);
  void MapStatistics(std::span<const detail::Utf8Span> spans);
  void EmitStatistics(size_t begin, size_t end, size_t text_offset);
  void DiscardStatistics(size_t offset);
  struct PendingToken {
    AsrTextToken value;
    size_t raw_begin = 0, raw_end = 0, mapped_end = 0;
    size_t begin = 0, end = 0;
    size_t emitted = std::numeric_limits<size_t>::max();
    bool mapped = false;
  };
  bool logprobs_ = false, body_ = false, text_started_ = false;
  bool statistics_ = false;
  bool complete_ = false;
  size_t offset_ = 0, raw_bytes_ = 0;
  std::string pending_, text_, language_;
  Status status_;
  detail::Utf8Stream utf8_;
  TokenLogprobs probabilities_;
  size_t selected_tokens_ = 0;
  std::deque<PendingToken> pending_tokens_;
  std::vector<AsrTextToken> text_tokens_;
  std::optional<double> no_speech_;
};
}  // namespace q4t::server
