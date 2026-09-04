// GPT-2 style Byte-Level BPE tokenizer for q4t.
//
// Decodes the exact tokenizer.json shipped with the target model
// (Qwen3.8-Flash-Next / qwen4_exp): a BPE model with a GPT-2 byte-level
// pre-tokenizer (Regex Split + ByteLevel) and an NFC normalizer. Encoding
// applies NFC normalization, splits on the Unicode pre-tokenize regex (via
// ICU 74), maps each piece through the GPT-2 byte alphabet, and runs ranked
// BPE merges. Decoding reverses the byte-level mapping.
//
// The loader is fail-closed: it rejects schema drift (wrong vocab/merge
// counts, an unexpected pre_tokenizer/decoder/normalizer) instead of
// guessing at a related tokenizer configuration.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/status.h"

namespace q4t {
namespace text {

// Resource limits enforced while loading the tokenizer JSON and while
// encoding/decoding caller-controlled text.
struct TokenizerLimits {
  std::size_t max_tokenizer_bytes = 64'000'000;
  std::size_t max_vocab_size = 300'000;
  std::size_t max_merges = 300'000;
  std::size_t max_added_tokens = 256;
  std::size_t max_input_bytes = 4'000'000;
  std::size_t max_tokens = 4'000'000;
};

// GPT-2 style Byte-Level BPE tokenizer loaded from tokenizer.json.
//
// A loaded Tokenizer is immutable and safe to share across threads:
// Encode/Decode take const this and do not mutate shared state.
class Tokenizer {
 public:
  // Pinned invariants of the target model's tokenizer.json. The loader fails
  // closed unless these hold.
  static constexpr std::size_t kPinnedBaseVocabSize = 248'044;
  static constexpr std::size_t kPinnedMergeCount = 247'587;

  ~Tokenizer();
  Tokenizer(Tokenizer&&) noexcept;
  Tokenizer& operator=(Tokenizer&&) noexcept;
  Tokenizer(const Tokenizer&) = delete;
  Tokenizer& operator=(const Tokenizer&) = delete;

  // Load and validate tokenizer.json from disk. On success *out owns the
  // tokenizer and Status is ok; on failure Status carries a message and *out
  // is left unchanged.
  static Status Load(const std::string& path, const TokenizerLimits& limits,
                     std::unique_ptr<Tokenizer>* out);
  // Load and validate from an in-memory JSON document.
  static Status LoadJson(std::string_view json, const TokenizerLimits& limits,
                         std::unique_ptr<Tokenizer>* out);

  // Encode text into token ids. Added/special tokens are matched as whole
  // strings before the byte-level BPE path, mirroring the reference
  // `tokenizers` behavior.
  Status Encode(std::string_view text, std::vector<std::uint32_t>* out) const;
  // Decode token ids into text. skip_special_tokens drops added tokens whose
  // `special` flag is true (base tokens are never special).
  Status Decode(const std::vector<std::uint32_t>& ids,
                bool skip_special_tokens, std::string* out) const;

  std::size_t BaseVocabSize() const noexcept;
  std::size_t MergeCount() const noexcept;
  std::size_t AddedTokenCount() const noexcept;
  // Total decodable id space (base + added).
  std::uint32_t VocabSize() const noexcept;

 private:
  struct Impl;
  explicit Tokenizer(std::unique_ptr<Impl> impl) noexcept;
  std::unique_ptr<Impl> impl_;
};

}  // namespace text
}  // namespace q4t
