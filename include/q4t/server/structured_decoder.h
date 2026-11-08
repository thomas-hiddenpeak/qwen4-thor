#pragma once

#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <vector>

#include "q4t/server/output_constraints.h"
#include "q4t/server/grammar_process.h"
#include "q4t/status.h"

namespace q4t::server {

class TokenSampler;
struct SampledToken;

#ifdef Q4T_ENABLE_XGRAMMAR
inline constexpr bool kStructuredDecodingAvailable = true;
#else
inline constexpr bool kStructuredDecodingAvailable = false;
#endif
inline constexpr char kXGrammarRevision[] =
    "ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7";

// Called before CLI/model initialization; -1 means an ordinary invocation.
int MaybeRunGrammarWorker(int argc, char** argv);

// Host-only adapter. Immutable decoded token bytes must exactly match the
// response decoder; empty/special and padded ids are never normal text.
class StructuredDecoder {
 public:
  ~StructuredDecoder();
  static Status Create(const OutputConstraints& plan,
                       const std::vector<std::string>& decoded_vocab,
                       int model_vocab, const std::vector<int32_t>& stop_ids,
                       std::unique_ptr<StructuredDecoder>* out,
                       const GrammarControl& control = {},
                       GrammarFailure* failure = nullptr);
  // Masks before greedy or explicit sampler selection, then accepts the
  // selected token exactly once. No unmasked fallback on failure. A sampler
  // requires sample; caller commits its history after successful acceptance.
  // forced_token >= 0 must itself be grammar-allowed and finite. A rejected
  // forced token leaves the matcher unchanged; it is never substituted later.
  Status Select(std::span<const uint16_t> logits, int32_t* token,
                int32_t forced_token = -1, TokenSampler* sampler = nullptr,
                SampledToken* sample = nullptr,
                std::span<const uint8_t> additional_mask = {});
  bool Terminated() const;

 private:
  struct Impl;
  explicit StructuredDecoder(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};

}  // namespace q4t::server
