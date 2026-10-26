// Bounded lazy full-vocabulary classification for fixed-k3 suffix admission.
#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <new>
#include <span>
#include <string>
#include <vector>

#include "q4t/mtp/suffix_drafter.h"
#include "q4t/text/tokenizer.h"

namespace q4t::server {

// Global-ID properties only: never stores request history. The caller must
// serialize access with the existing tokenizer mutex and keep tok alive.
class SuffixTokenCache {
 public:
  static constexpr size_t kMaxBytes = size_t{256} * 1024;
  struct Stats {
    uint64_t checks = 0;
    uint64_t lookups = 0;
    uint64_t decodes = 0;
    uint64_t unknown = 0;
    uint64_t allocations = 0;
    uint64_t allocation_ns = 0;  // Nested in the caller's numeric/query time.
    uint64_t release_ns = 0;
    size_t peak_bytes = 0;
  };

  Status Configure(const text::Tokenizer* tok, int vocab) {
    Reset();
    stats_ = {};
    if (!tok || vocab <= 0 || static_cast<size_t>(vocab) > kMaxBytes)
      return Status::Fail("invalid suffix classifier vocabulary");
    tok_ = tok;
    vocab_ = static_cast<size_t>(vocab);
    return Status();
  }

  Status Classify(int32_t token, uint8_t* flags) {
    if (!tok_ || !flags || token < 0 || static_cast<size_t>(token) >= vocab_)
      return Status::Fail("invalid suffix classifier global ID");
    ++stats_.lookups;
    if (!classes_) {
      const uint64_t started = Now();
      try {
        classes_ = std::make_unique<uint8_t[]>(vocab_);
      } catch (const std::bad_alloc&) {
        stats_.allocation_ns += Now() - started;
        return Status::Fail("suffix class allocation failed");
      }
      stats_.allocation_ns += Now() - started;
      ++stats_.allocations;
      stats_.peak_bytes = vocab_;
    }
    // 0 is unseen; 0x80 is a cached zero. Unknown/padding and nonnumeric
    // tokens must not be decoded repeatedly or mistaken for unseen entries.
    const uint8_t cached = classes_[token];
    if (cached & kKnown) {
      *flags = cached & kFlags;
      return Status();
    }
    try {
      std::vector<uint32_t> one{static_cast<uint32_t>(token)};
      std::string decoded;
      ++stats_.decodes;
      // Preserve special bytes, including padding/unknown flags of zero.
      const Status status = tok_->Decode(one, false, &decoded);
      *flags = 0;
      if (!status.ok() || decoded.empty()) {
        ++stats_.unknown;
      } else {
        *flags = mtp::ClassifySuffixToken(decoded);
      }
      classes_[token] = kKnown | *flags;
    } catch (const std::bad_alloc&) {
      return Status::Fail("suffix class decode allocation failed");
    }
    return Status();
  }

  Status Check(std::span<const int32_t> ids, bool* numeric) {
    if (!numeric || ids.empty() || ids.size() > mtp::SuffixDrafter::kMaxMatch)
      return Status::Fail("invalid suffix classification span");
    *numeric = false;
    ++stats_.checks;
    bool allowed = true;
    bool digit = false;
    for (int32_t id : ids) {
      uint8_t flags = 0;
      const Status status = Classify(id, &flags);
      if (!status.ok()) return status;
      allowed = allowed && (flags & mtp::kSuffixAllowedAscii) != 0;
      digit = digit || (flags & mtp::kSuffixContainsDigit) != 0;
    }
    *numeric = allowed && digit;
    return Status();
  }

  void Reset() {
    if (classes_) {
      const uint64_t started = Now();
      classes_.reset();
      stats_.release_ns += Now() - started;
    }
    tok_ = nullptr;
    vocab_ = 0;
  }
  size_t MemoryBytes() const { return classes_ ? vocab_ : 0; }
  const Stats& Counters() const { return stats_; }

 private:
  static constexpr uint8_t kKnown = 0x80;
  static constexpr uint8_t kFlags =
      mtp::kSuffixAllowedAscii | mtp::kSuffixContainsDigit;
  static uint64_t Now() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  }
  const text::Tokenizer* tok_ = nullptr;
  std::unique_ptr<uint8_t[]> classes_;
  size_t vocab_ = 0;
  Stats stats_;
};

static_assert(sizeof(SuffixTokenCache) <= 128);

}  // namespace q4t::server
