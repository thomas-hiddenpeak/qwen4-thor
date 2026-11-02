// Independent of GPU slots: retain ownership until grammar and output die.
#pragma once

#include <atomic>
#include <cstddef>

namespace q4t::server {

inline constexpr size_t kDecodedVocabularyStorageLimit = 32 * 1024 * 1024;
inline constexpr size_t kCompiledGrammarLimit = 64 * 1024 * 1024;

class StructuredRequestLease {
 public:
  explicit StructuredRequestLease(std::atomic<int>& active) : active_(active) {}
  ~StructuredRequestLease() {
    if (owned_) active_.fetch_sub(1, std::memory_order_release);
  }
  StructuredRequestLease(const StructuredRequestLease&) = delete;
  StructuredRequestLease& operator=(const StructuredRequestLease&) = delete;
  bool TryAcquire() {
    if (owned_) return true;
    int expected = 0;
    owned_ = active_.compare_exchange_strong(expected, 1);
    return owned_;
  }

 private:
  std::atomic<int>& active_;
  bool owned_ = false;
};

// Conservative accounted storage, not allocator overhead or compiler RSS.
class VocabularyStorageBudget {
 public:
  bool Charge(size_t bytes) {
    if (bytes > kDecodedVocabularyStorageLimit - used_) return false;
    used_ += bytes;
    return true;
  }
  size_t Used() const { return used_; }

 private:
  size_t used_ = 0;
};

}  // namespace q4t::server
