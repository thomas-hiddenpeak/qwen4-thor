#pragma once

#include <cstdint>
#include <optional>
#include <random>
#include <span>
#include <string>
#include <utility>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

struct SamplingOptions {
  double temperature = 1;
  double top_p = 1;
  double frequency_penalty = 0;
  double presence_penalty = 0;
  int choices = 1;
  std::optional<int64_t> seed;
  bool logprobs = false;
  int top_logprobs = 0;
  std::vector<std::pair<int32_t, double>> logit_bias;

  bool PlainGreedy() const {
    return temperature == 0 && frequency_penalty == 0 &&
           presence_penalty == 0 && logit_bias.empty() && !logprobs;
  }
};

Status ParseSamplingOptions(const io::Json& request, SamplingOptions* options,
                            std::string* error_param = nullptr);

struct TokenLogprob {
  int32_t token = -1;
  double logprob = -9999;
};

struct SampledToken {
  int32_t token = -1;
  // Raw finite model distribution, before selection penalties/masks, matching
  // vLLM's default raw_logprobs convention. Not the truncated nucleus mass.
  double logprob = -9999;
  std::vector<TokenLogprob> top;
};

// Per-choice state; no shared RNG/history, model mutation, or CUDA ownership.
// Select is speculative with respect to history: Observe only after the token
// was actually accepted. A mask is one byte per model token (zero disallows).
class TokenSampler {
 public:
  Status Init(const SamplingOptions& options, int vocab, uint64_t entropy_seed,
              uint64_t choice_index = 0, int token_vocab = 0);
  Status Select(std::span<const uint16_t> bf16_logits, SampledToken* sample,
                std::span<const uint8_t> allowed = {},
                int32_t forced_token = -1);
  Status Observe(int32_t token);

 private:
  SamplingOptions options_;
  int token_vocab_ = 0;
  std::vector<uint64_t> counts_;
  std::vector<double> biases_;
  std::mt19937_64 random_;
};

}  // namespace q4t::server
