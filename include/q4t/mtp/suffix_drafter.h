// Fixed-k3 request-local prompt lookup. All token IDs are global vocabulary
// IDs, including when the neural drafter uses an independent subset head.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string_view>

#include "q4t/status.h"

namespace q4t::mtp {

inline constexpr uint8_t kSuffixAllowedAscii = 1;
inline constexpr uint8_t kSuffixContainsDigit = 2;

// Classify decoded bytes without locale or Unicode normalization. Allowed
// bytes are ASCII digits, whitespace (09..0D, 20) and punctuation. The digit
// flag is independent of the allowed flag. Empty/unknown tokens have flags 0.
// Decode special tokens without dropping them before calling this function.
uint8_t ClassifySuffixToken(std::string_view decoded);

enum class SuffixReason {
  kReady,
  kInactive,
  kNoMatch,
  kIncomplete,
  kFirstTokenMismatch,
  kNumericRejected,
  kShortRejected,
  kInvalidToken,
  kCapacityExhausted,
};

const char* SuffixReasonName(SuffixReason reason);

struct SuffixProposal {
  bool ready = false;
  std::array<int32_t, 3> tokens{};
  int match_length = 0;
  uint32_t match_end = UINT32_MAX;
  uint32_t candidates_examined = 0;
  uint32_t compared_tokens = 0;
  SuffixReason reason = SuffixReason::kInactive;
};

struct SuffixCounters {
  uint64_t init_ns = 0;
  uint64_t append_ns = 0;
  uint64_t query_ns = 0;
  uint64_t release_ns = 0;
  uint64_t initial_tokens = 0;
  uint64_t append_calls = 0;
  uint64_t appended_tokens = 0;
  uint64_t queries = 0;
  uint64_t hits = 0;
  uint64_t candidates_examined = 0;
  uint64_t compared_tokens = 0;
  uint64_t matched_tokens = 0;
  uint64_t no_match = 0;
  uint64_t incomplete = 0;
  uint64_t first_token_mismatch = 0;
  uint64_t numeric_checks = 0;
  uint64_t classified_tokens = 0;
  uint64_t numeric_rejected = 0;
  uint64_t numeric_ns = 0;  // Included in query_ns, not an additional phase.
  uint64_t short_rejected = 0;
  uint64_t invalid_tokens = 0;
  uint64_t capacity_exhaustions = 0;
  size_t peak_memory_bytes = 0;
};

class SuffixDrafter {
 public:
  static constexpr int kDraftCount = 3;
  static constexpr int kMinMatch = 3;
  static constexpr int kMinAdmittedMatch = 32;
  static constexpr int kMaxMatch = 64;
  static constexpr size_t kCandidates = 4;
  static constexpr size_t kBucketCount = size_t{1} << 16;
  static constexpr size_t kMaxCapacity = size_t{1} << 18;
  static constexpr size_t kIndexBytes =
      kBucketCount * kCandidates * sizeof(uint32_t);
  static constexpr size_t kMaxMemoryBytes =
      kIndexBytes + kMaxCapacity * sizeof(int32_t);

  SuffixDrafter() = default;
  SuffixDrafter(const SuffixDrafter&) = delete;
  SuffixDrafter& operator=(const SuffixDrafter&) = delete;

  // Capacity is the committed-token budget for this request, never more than
  // kMaxCapacity. Invalid configuration, history or allocation returns failure
  // with no active index. A new Init starts fresh per-request counters.
  // Nonempty token_classes must cover the full global vocabulary and remain
  // alive and immutable until Reset. Empty disables numeric admission
  // filtering.
  // The borrowed table is not included in MemoryBytes; its owner accounts for
  // allocation, construction and release separately.
  Status Init(std::span<const int32_t> committed, int vocab,
              size_t capacity = kMaxCapacity,
              std::span<const uint8_t> token_classes = {});

  // Append only tokens already committed by the target model. Capacity
  // exhaustion disables lookup and returns success: the caller uses neural
  // MTP from then on. Invalid tokens disable lookup and return failure. Both
  // cases leave the indexed history unchanged; no partial append is possible.
  Status Append(std::span<const int32_t> committed);

  // Query the immutable view [committed history, pending]. The pending token
  // never enters the index. Select the longest of four candidates (newest on
  // ties), THEN require three available tokens and tokens[0] == d0. Do not
  // search another candidate just to pass a gate. With token_classes, reject
  // a selected full match containing a digit and only allowed ASCII bytes,
  // after the full-three and d0 gates. Periodic continuations may read the
  // pending token, but nothing beyond it. Finally require a match of at least
  // kMinAdmittedMatch tokens, including pending. A short rejection preserves
  // the selected proposal and history; it never searches another candidate.
  SuffixProposal ProposeAfter(int32_t pending, int32_t d0);

  // Release both arrays on cancellation, failure, termination or slot reuse.
  // Counters survive Reset so the owner can report the release cost. Allocator
  // bookkeeping and process RSS are outside MemoryBytes and need OS evidence.
  void Reset();
  bool Active() const { return active_; }
  size_t Size() const { return size_; }
  size_t Capacity() const { return capacity_; }
  size_t MemoryBytes() const {
    return buckets_ ? kIndexBytes + capacity_ * sizeof(int32_t) : 0;
  }
  SuffixReason DisabledReason() const { return disabled_reason_; }
  std::span<const int32_t> History() const { return {history_.get(), size_}; }
  const SuffixCounters& Counters() const { return counters_; }

 private:
  struct Bucket {
    std::array<uint32_t, kCandidates> ends{UINT32_MAX, UINT32_MAX, UINT32_MAX,
                                           UINT32_MAX};
  };
  static_assert(sizeof(Bucket) == kCandidates * sizeof(uint32_t));

  static size_t BucketFor(int32_t a, int32_t b, int32_t c);
  bool Valid(int32_t token) const { return token >= 0 && token < vocab_; }
  void AppendUnchecked(std::span<const int32_t> committed);

  std::unique_ptr<Bucket[]> buckets_;
  std::unique_ptr<int32_t[]> history_;
  std::span<const uint8_t> token_classes_;
  size_t size_ = 0;
  size_t capacity_ = 0;
  int vocab_ = 0;
  bool active_ = false;
  SuffixReason disabled_reason_ = SuffixReason::kInactive;
  SuffixCounters counters_;
};

}  // namespace q4t::mtp
