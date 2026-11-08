#pragma once

#include <array>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/sampling.h"

namespace q4t::server {

using CompletionTokenDecoder = std::function<Status(int32_t, std::string*)>;

struct CompletionProbabilityRow {
  std::string token;
  std::string logprob;
  std::string top;
  size_t Bytes() const { return token.size() + logprob.size() + top.size(); }
};

// A null sample denotes the first unconditional prompt token, never a made-up
// zero probability. Otherwise use actual untruncated selected-token scores.
Status CompletionProbability(int32_t token, const SampledToken* sample,
                              const CompletionTokenDecoder& decode,
                              CompletionProbabilityRow* row);

size_t Utf8Characters(std::string_view text);
// Preserve valid original echo text and map raw token starts through UTF-8
// repair. Multiple byte tokens belonging to one scalar share its offset.
Status CompletionEchoOffsets(std::string_view bytes,
                              std::span<const size_t> token_bytes,
                              std::string* text,
                              std::vector<size_t>* character_offsets);

class CompletionProbabilityRows {
 public:
  static constexpr size_t kLimit = 16 * 1024 * 1024;
  Status Add(const CompletionProbabilityRow& row, size_t character_offset);
  Status Append(const CompletionProbabilityRows& other);
  std::string Json() const;
  size_t Size() const { return count_; }

 private:
  std::array<std::string, 4> columns_;
  size_t bytes_ = 0;
  size_t count_ = 0;
};

struct CompletionCandidate {
  std::string text;
  std::string logprobs = "null";
  std::string finish;
  double sum_logprob = 0;
  uint64_t score_tokens = 0;
  uint64_t tokens = 0;
  int ordinal = 0;
  double Score() const {
    return score_tokens ? sum_logprob / score_tokens : 0;
  }
};

class CompletionChoices {
 public:
  static constexpr size_t kLimit = 32 * 1024 * 1024;
  CompletionChoices(int prompts, int n, int best_of, bool stream)
      : prompts_(prompts), n_(n), best_of_(best_of), stream_(stream) {}
  Status Add(int index, uint64_t prompt, CompletionCandidate candidate);
  uint64_t CompletionTokens() const { return completion_; }
  std::string UsageJson() const;
  Status Json(std::string_view id, std::string_view model, int64_t created,
              std::string* output) const;

 private:
  int prompts_;
  int n_;
  int best_of_;
  bool stream_;
  int accepted_ = 0;
  uint64_t prompt_ = 0;
  uint64_t group_prompt_ = 0;
  uint64_t completion_ = 0;
  size_t bytes_ = 0;
  std::vector<std::vector<CompletionCandidate>> selected_;
};

std::string CompletionChunk(std::string_view id, std::string_view model,
                             int64_t created, int index,
                             std::string_view text, std::string_view finish,
                             std::string_view logprobs, bool include_usage,
                             bool obfuscate);
std::string CompletionUsageChunk(std::string_view id, std::string_view model,
                                  int64_t created, std::string_view usage,
                                  bool obfuscate);

}  // namespace q4t::server
