// Independent of GPU slots: retain ownership until grammar and output die.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <mutex>

namespace q4t::server {

inline constexpr size_t kDecodedVocabularyStorageLimit = 32 * 1024 * 1024;
inline constexpr size_t kCompiledGrammarLimit = 64 * 1024 * 1024;

class StructuredAdmission {
 private:
  friend class StructuredRequestLease;
  enum class State { kFree, kRunning, kFinishing };
  std::mutex mutex_;
  std::condition_variable changed_;
  State state_ = State::kFree;
};

class StructuredRequestLease {
 public:
  enum class Result { kAcquired, kBusy, kCancelled };
  StructuredRequestLease(StructuredAdmission& admission,
                         std::atomic<int>& active)
      : admission_(admission), active_(active) {}
  ~StructuredRequestLease() {
    if (!owned_) return;
    {
      const std::lock_guard lock(admission_.mutex_);
      active_.fetch_sub(1, std::memory_order_release);
      admission_.state_ = StructuredAdmission::State::kFree;
    }
    admission_.changed_.notify_all();
  }
  StructuredRequestLease(const StructuredRequestLease&) = delete;
  StructuredRequestLease& operator=(const StructuredRequestLease&) = delete;
  bool TryAcquire() {
    const std::lock_guard lock(admission_.mutex_);
    return AcquireLocked();
  }
  // Only a terminal owner's cleanup may be awaited. Running work still
  // rejects immediately. Waiters already hold the bounded text admission
  // lease; cancellation includes the request deadline, disconnect and stop.
  template <typename Cancelled>
  Result Acquire(const Cancelled& cancelled) {
    std::unique_lock lock(admission_.mutex_);
    for (;;) {
      if (cancelled()) return Result::kCancelled;
      if (AcquireLocked()) return Result::kAcquired;
      if (admission_.state_ != StructuredAdmission::State::kFinishing)
        return Result::kBusy;
      admission_.changed_.wait_for(lock, std::chrono::milliseconds(5));
    }
  }
  // Called before any terminal bytes become visible, never after writing.
  // This does not release ownership or allow overlapping grammar/output RAM.
  void BeginHandoff() noexcept {
    if (!owned_) return;
    const std::lock_guard lock(admission_.mutex_);
    admission_.state_ = StructuredAdmission::State::kFinishing;
  }

 private:
  bool AcquireLocked() {
    if (owned_) return true;
    if (admission_.state_ != StructuredAdmission::State::kFree) return false;
    admission_.state_ = StructuredAdmission::State::kRunning;
    active_.fetch_add(1, std::memory_order_relaxed);
    owned_ = true;
    return true;
  }
  StructuredAdmission& admission_;
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
