// Requested-byte quota. This is not a physical RAM or CUDA pool size limit.
// Queue identifiers must be unique across stream lifetimes (0 means device).
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <mutex>

namespace q4t::runtime::quota {

enum class Kind { kDevice, kHostAlloc, kMallocHost, kManaged, kAsyncDevice };
enum class Release { kDevice, kHost, kAsync };
enum class Error {
  kNone,
  kInvalidArgument,
  kByteLimit,
  kEntryLimit,
  kStaleTicket,
  kUnknownPointer,
  kWrongState,
  kWrongRelease,
  kBackendFailure,
  kOwnershipUnknown,
  kSequenceExhausted,
  kCompletionFailed,
};

inline const char* ErrorName(Error error) noexcept {
  switch (error) {
    case Error::kNone:
      return "none";
    case Error::kInvalidArgument:
      return "invalid_argument";
    case Error::kByteLimit:
      return "byte_limit";
    case Error::kEntryLimit:
      return "entry_limit";
    case Error::kStaleTicket:
      return "stale_ticket";
    case Error::kUnknownPointer:
      return "unknown_pointer";
    case Error::kWrongState:
      return "wrong_state";
    case Error::kWrongRelease:
      return "wrong_release";
    case Error::kBackendFailure:
      return "backend_failure";
    case Error::kOwnershipUnknown:
      return "ownership_unknown";
    case Error::kSequenceExhausted:
      return "sequence_exhausted";
    case Error::kCompletionFailed:
      return "completion_failed";
  }
  return "invalid_error";
}

struct Result {
  Error error = Error::kNone;
  int backend_error = 0;
  bool ok() const noexcept { return error == Error::kNone; }
};

struct Ticket {
  const void* owner = nullptr;
  size_t index = 0;
  uint64_t generation = 0;
};

struct Admission {
  Result result;
  Ticket ticket;
  bool ok() const noexcept { return result.ok(); }
};

struct Summary {
  uint64_t limit_bytes = 0;
  uint64_t charged_bytes = 0;
  uint64_t peak_bytes = 0;
  uint64_t reserved_bytes = 0;
  uint64_t live_bytes = 0;
  uint64_t releasing_bytes = 0;
  uint64_t pending_bytes = 0;
  uint64_t unknown_bytes = 0;
  size_t entries = 0;
  size_t capacity = 0;
  bool poisoned = false;
};

// Reserve/commit surrounds each backend operation. The metadata mutex is not
// held while CUDA runs; in-flight reservations already count against the cap.
// No reset/resize: scopes cannot discard a ledger with unresolved ownership.
// The caller owns process teardown; this destructor performs no backend calls.
template <size_t Capacity = 16384>
class Ledger {
  static_assert(Capacity > 0);

 public:
  class Fence {
   public:
    Fence() = default;

   private:
    friend class Ledger;
    const Ledger* owner_ = nullptr;
    uint64_t frontier_ = 0;
    uint64_t queue_ = 0;
  };

  explicit Ledger(uint64_t limit_bytes) noexcept : limit_(limit_bytes) {}
  Ledger(const Ledger&) = delete;
  Ledger& operator=(const Ledger&) = delete;

  Admission Reserve(uint64_t bytes, Kind kind) noexcept {
    const std::lock_guard lock(mutex_);
    if (!ValidKind(kind) || bytes == 0 || limit_ == 0)
      return {{Error::kInvalidArgument}, {}};
    if (poisoned_) return {{Error::kOwnershipUnknown}, {}};
    if (bytes > limit_ - charged_) return {{Error::kByteLimit}, {}};
    size_t index = 0;
    while (index < Capacity && entries_[index].state != State::kEmpty) ++index;
    if (index == Capacity) return {{Error::kEntryLimit}, {}};
    if (generation_ == std::numeric_limits<uint64_t>::max()) {
      poisoned_ = true;
      return {{Error::kSequenceExhausted}, {}};
    }
    Entry& entry = entries_[index];
    entry = {};
    entry.state = State::kReserved;
    entry.bytes = bytes;
    entry.kind = kind;
    entry.generation = ++generation_;
    charged_ += bytes;
    if (charged_ > peak_) peak_ = charged_;
    return {{}, {this, index, entry.generation}};
  }

  Result CommitAllocation(Ticket ticket, void* pointer,
                          int backend_error) noexcept {
    const std::lock_guard lock(mutex_);
    Entry* entry = Find(ticket);
    if (!entry) return {Error::kStaleTicket};
    if (entry->state != State::kReserved) return {Error::kWrongState};
    if (backend_error && !pointer) {
      Clear(*entry);
      return {Error::kBackendFailure, backend_error};
    }
    bool duplicate = false;
    for (Entry& other : entries_) {
      // A free can execute before its host thread publishes the return value.
      // Both releasing and pending generations may already have been recycled.
      if (other.pointer == pointer && pointer && other.state != State::kEmpty &&
          other.state != State::kPending && other.state != State::kReleasing) {
        duplicate = true;
        Unknown(other);
      }
    }
    entry->pointer = pointer;
    if (backend_error || !pointer || duplicate) {
      Unknown(*entry);
      return {Error::kOwnershipUnknown, backend_error};
    }
    entry->state = State::kLive;
    return {};
  }

  Admission BeginRelease(void* pointer, Release release,
                         uint64_t queue = 0) noexcept {
    const std::lock_guard lock(mutex_);
    if (!pointer || !ValidRelease(release) ||
        (release == Release::kAsync ? queue == 0 : queue != 0))
      return {{Error::kInvalidArgument}, {}};
    bool known = false;
    for (size_t index = 0; index < Capacity; ++index) {
      Entry& entry = entries_[index];
      if (entry.state == State::kEmpty || entry.pointer != pointer) continue;
      known = true;
      if (entry.state != State::kLive) continue;
      if (!CanRelease(entry.kind, release)) return {{Error::kWrongRelease}, {}};
      entry.state = State::kReleasing;
      entry.release = release;
      entry.queue = queue;
      return {{}, {this, index, entry.generation}};
    }
    return {{known ? Error::kWrongState : Error::kUnknownPointer}, {}};
  }

  Result CommitRelease(Ticket ticket, int backend_error) noexcept {
    const std::lock_guard lock(mutex_);
    Entry* entry = Find(ticket);
    if (!entry) return {Error::kStaleTicket};
    if (entry->state != State::kReleasing) return {Error::kWrongState};
    if (backend_error) {
      Unknown(*entry);
      return {Error::kOwnershipUnknown, backend_error};
    }
    if (entry->release != Release::kAsync) {
      Clear(*entry);
      return {};
    }
    if (frontier_ == std::numeric_limits<uint64_t>::max()) {
      Unknown(*entry);
      return {Error::kSequenceExhausted};
    }
    entry->state = State::kPending;
    entry->retired = ++frontier_;
    return {};
  }

  // Capture BEFORE starting a real completion wait. Only previously published
  // frees in this queue (or all queues for 0) may be credited by its success.
  Fence Capture(uint64_t queue = 0) const noexcept {
    const std::lock_guard lock(mutex_);
    Fence fence;
    fence.owner_ = this;
    fence.frontier_ = frontier_;
    fence.queue_ = queue;
    return fence;
  }

  Result Complete(const Fence& fence, int backend_error) noexcept {
    const std::lock_guard lock(mutex_);
    if (fence.owner_ != this) return {Error::kInvalidArgument};
    if (backend_error) poisoned_ = true;
    for (Entry& entry : entries_) {
      if (entry.state != State::kPending || entry.retired > fence.frontier_ ||
          (fence.queue_ && entry.queue != fence.queue_))
        continue;
      if (backend_error)
        Unknown(entry);
      else
        Clear(entry);
    }
    return backend_error ? Result{Error::kCompletionFailed, backend_error}
                         : Result{};
  }

  Summary Snapshot() const noexcept {
    const std::lock_guard lock(mutex_);
    Summary result;
    result.limit_bytes = limit_;
    result.charged_bytes = charged_;
    result.peak_bytes = peak_;
    result.capacity = Capacity;
    result.poisoned = poisoned_;
    for (const Entry& entry : entries_) {
      if (entry.state != State::kEmpty) ++result.entries;
      switch (entry.state) {
        case State::kEmpty:
          break;
        case State::kReserved:
          result.reserved_bytes += entry.bytes;
          break;
        case State::kLive:
          result.live_bytes += entry.bytes;
          break;
        case State::kReleasing:
          result.releasing_bytes += entry.bytes;
          break;
        case State::kPending:
          result.pending_bytes += entry.bytes;
          break;
        case State::kUnknown:
          result.unknown_bytes += entry.bytes;
          break;
      }
    }
    return result;
  }

 private:
  enum class State { kEmpty, kReserved, kLive, kReleasing, kPending, kUnknown };
  struct Entry {
    uint64_t bytes = 0;
    uint64_t generation = 0;
    uint64_t queue = 0;
    uint64_t retired = 0;
    void* pointer = nullptr;
    Kind kind = Kind::kDevice;
    Release release = Release::kDevice;
    State state = State::kEmpty;
  };

  static bool ValidKind(Kind kind) noexcept {
    return kind >= Kind::kDevice && kind <= Kind::kAsyncDevice;
  }
  static bool ValidRelease(Release release) noexcept {
    return release >= Release::kDevice && release <= Release::kAsync;
  }
  static bool CanRelease(Kind kind, Release release) noexcept {
    if (kind == Kind::kHostAlloc || kind == Kind::kMallocHost)
      return release == Release::kHost;
    if (kind == Kind::kAsyncDevice) return release == Release::kAsync;
    if (kind == Kind::kManaged) return release == Release::kDevice;
    return release == Release::kDevice || release == Release::kAsync;
  }
  Entry* Find(Ticket ticket) noexcept {
    if (ticket.owner != this || ticket.index >= Capacity ||
        ticket.generation == 0)
      return nullptr;
    Entry& entry = entries_[ticket.index];
    return entry.state != State::kEmpty && entry.generation == ticket.generation
               ? &entry
               : nullptr;
  }
  void Clear(Entry& entry) noexcept {
    charged_ -= entry.bytes;
    entry = {};
  }
  void Unknown(Entry& entry) noexcept {
    entry.state = State::kUnknown;
    poisoned_ = true;
    // A reused address may have several generations in flight. If the old
    // release result is ambiguous, none of its aliases may be blindly freed.
    if (entry.pointer) {
      for (Entry& other : entries_) {
        if (other.state != State::kEmpty && other.pointer == entry.pointer)
          other.state = State::kUnknown;
      }
    }
  }

  mutable std::mutex mutex_;
  std::array<Entry, Capacity> entries_{};
  const uint64_t limit_;
  uint64_t charged_ = 0;
  uint64_t peak_ = 0;
  uint64_t generation_ = 0;
  uint64_t frontier_ = 0;
  bool poisoned_ = false;
};

// Backend methods must be noexcept and must never re-enter this allocator.
// Allocate(kind, bytes, pointer, queue), Release(kind, pointer, queue), and
// Synchronize(queue) return the original backend error (0 means success).
// Positive-byte requests only: zero-size CUDA compatibility belongs at the
// eventual link boundary. This adapter does not discover bypassed allocations.
template <typename Backend, size_t Capacity = 16384>
class Allocator {
 public:
  Allocator(Ledger<Capacity>& ledger, Backend& backend) noexcept
      : ledger_(ledger), backend_(backend) {}

  Result Allocate(Kind kind, size_t bytes, void** pointer,
                  uint64_t queue = 0) noexcept {
    if (!pointer) return {Error::kInvalidArgument};
    *pointer = nullptr;
    if (kind == Kind::kAsyncDevice ? queue == 0 : queue != 0)
      return {Error::kInvalidArgument};
    const Admission admission = ledger_.Reserve(bytes, kind);
    if (!admission.ok()) return admission.result;
    void* allocated = nullptr;
    const int error = backend_.Allocate(kind, bytes, &allocated, queue);
    const Result result =
        ledger_.CommitAllocation(admission.ticket, allocated, error);
    if (result.ok()) *pointer = allocated;
    return result;
  }

  Result Free(void* pointer, Release release, uint64_t queue = 0) noexcept {
    const Admission admission = ledger_.BeginRelease(pointer, release, queue);
    if (!admission.ok()) return admission.result;
    const int error = backend_.Release(release, pointer, queue);
    return ledger_.CommitRelease(admission.ticket, error);
  }

  Result Synchronize(uint64_t queue = 0) noexcept {
    const auto fence = ledger_.Capture(queue);
    const int error = backend_.Synchronize(queue);
    return ledger_.Complete(fence, error);
  }

 private:
  Ledger<Capacity>& ledger_;
  Backend& backend_;
};
}  // namespace q4t::runtime::quota
