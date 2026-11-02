#pragma once

#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <vector>

#include "q4t/server/output_constraints.h"
#include "q4t/status.h"

namespace q4t::server {

#ifdef Q4T_ENABLE_XGRAMMAR
inline constexpr bool kStructuredDecodingAvailable = true;
#else
inline constexpr bool kStructuredDecodingAvailable = false;
#endif
inline constexpr char kXGrammarRevision[] =
    "ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7";

// Host-only adapter. Immutable decoded token bytes must exactly match the
// response decoder; empty/special and padded ids are never normal text.
class StructuredDecoder {
 public:
  ~StructuredDecoder();
  static Status Create(const OutputConstraints& plan,
                       const std::vector<std::string>& decoded_vocab,
                       int model_vocab, const std::vector<int32_t>& stop_ids,
                       std::unique_ptr<StructuredDecoder>* out);
  // Masks *before* greedy selection and accepts the selected token exactly
  // once. No fallback to an unmasked token on failure.
  Status Select(std::span<const uint16_t> logits, int32_t* token);
  bool Terminated() const;

 private:
  struct Impl;
  explicit StructuredDecoder(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};

}  // namespace q4t::server
