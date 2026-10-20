// Ownership contract for individually reserved startup allocations.
#ifndef Q4T_RUNTIME_RESERVATION_OWNER_H_
#define Q4T_RUNTIME_RESERVATION_OWNER_H_

#include <algorithm>
#include <cstddef>
#include <limits>
#include <span>
#include <type_traits>
#include <utility>
#include <vector>

namespace q4t::runtime::reservation {

enum class Error {
  kNone,
  kInvalidPlan,
  kWrongPhase,
  kAllocation,
  kInvalidPointer,
  kSizeMismatch,
  kPlanIncomplete,
  kNotOwned,
  kNotHanded,
  kAlreadyReleased,
  kRelease,
  kHandedStillLive,
};

inline const char* ErrorName(Error error) {
  switch (error) {
    case Error::kNone:
      return "none";
    case Error::kInvalidPlan:
      return "invalid_plan";
    case Error::kWrongPhase:
      return "wrong_phase";
    case Error::kAllocation:
      return "allocation_failed";
    case Error::kInvalidPointer:
      return "invalid_pointer";
    case Error::kSizeMismatch:
      return "size_mismatch";
    case Error::kPlanIncomplete:
      return "plan_incomplete";
    case Error::kNotOwned:
      return "not_owned";
    case Error::kNotHanded:
      return "not_handed";
    case Error::kAlreadyReleased:
      return "already_released";
    case Error::kRelease:
      return "release_completion_unknown";
    case Error::kHandedStillLive:
      return "handed_still_live";
  }
  return "unknown";
}

enum class ReleaseReason { kApplication, kRollback, kExitUnhanded };

inline const char* ReasonName(ReleaseReason reason) {
  switch (reason) {
    case ReleaseReason::kApplication:
      return "application";
    case ReleaseReason::kRollback:
      return "rollback";
    case ReleaseReason::kExitUnhanded:
      return "exit_unhanded";
  }
  return "unknown";
}

struct Result {
  Error error = Error::kNone;
  int backend_rc = 0;
  size_t index = 0;
  bool ok() const { return error == Error::kNone; }
};

struct Summary {
  Result result;
  size_t count = 0;
  size_t allocated = 0;
  size_t consumed = 0;
  size_t freed = 0;
  size_t unhanded_remaining = 0;
  size_t handed_live = 0;
  size_t release_unknown = 0;
  bool prepared = false;
  bool finalized = false;
};

// Backend contract:
//   int Allocate(size_t index, size_t bytes, void** output) noexcept;
//   int Release(size_t, size_t, void*, ReleaseReason) noexcept;
// A nonzero Release result leaves completion unknown and is never retried.
// Handed buffers remain the application's responsibility. Finalize only frees
// unhanded buffers and reports any live handed buffers as an acceptance
// failure.
// Callers serialize this object and invoke Finalize before CUDA teardown.
template <class Backend>
class Owner {
 public:
  // Metadata construction may throw; it never invokes the backend. Copying
  // the plan avoids borrowing the caller's temporary metadata. Physical
  // allocation is stable descending by bytes; Take preserves logical order.
  Owner(std::span<const size_t> plan, Backend& backend)
      : plan_(plan.begin(), plan.end()),
        backend_(backend),
        blocks_(plan.size()),
        physical_order_(plan.size()),
        allocated_order_(plan.size()) {
    static_assert(noexcept(std::declval<Backend&>().Allocate(
        size_t{}, size_t{}, static_cast<void**>(nullptr))));
    static_assert(noexcept(std::declval<Backend&>().Release(
        size_t{}, size_t{}, nullptr, ReleaseReason::kApplication)));
    for (size_t i = 0; i < plan.size(); ++i) physical_order_[i] = i;
    std::stable_sort(
        physical_order_.begin(), physical_order_.end(),
        [&](size_t left, size_t right) { return plan_[left] > plan_[right]; });
  }
  ~Owner() noexcept { Finalize(); }
  Owner(const Owner&) = delete;
  Owner& operator=(const Owner&) = delete;

  bool startup_complete() const noexcept { return startup_complete_; }
  bool started() const { return started_; }
  bool prepared() const { return prepared_; }
  bool complete() const { return consumed_ == plan_.size(); }
  bool healthy() const { return error_.ok() && !finalized_; }
  size_t consumed() const { return consumed_; }

  Result Prepare() noexcept {
    if (started_ || finalized_ || !error_.ok()) {
      return Fail(Error::kWrongPhase);
    }
    started_ = true;
    if (plan_.empty()) return Fail(Error::kInvalidPlan);
    size_t total = 0;
    for (size_t bytes : plan_) {
      if (bytes == 0 || bytes > std::numeric_limits<size_t>::max() - total) {
        return Fail(Error::kInvalidPlan);
      }
      total += bytes;
    }
    for (size_t i : physical_order_) {
      void* pointer = nullptr;
      const int rc = backend_.Allocate(i, plan_[i], &pointer);
      if (rc != 0) {
        Fail(Error::kAllocation, rc, i);
        ReleaseUnhanded(ReleaseReason::kRollback);
        return error_;
      }
      if (pointer == nullptr || Find(pointer) != blocks_.size()) {
        Fail(Error::kInvalidPointer, 0, i);
        ReleaseUnhanded(ReleaseReason::kRollback);
        return error_;
      }
      blocks_[i] = {pointer, State::kUnhanded};
      allocated_order_[allocated_] = i;
      ++allocated_;
    }
    prepared_ = true;
    return {};
  }

  Result Complete() noexcept {
    const Summary summary = Snapshot();
    if (!prepared_ || !healthy() || !complete() || startup_complete_ ||
        summary.handed_live != summary.count || summary.freed != 0 ||
        summary.unhanded_remaining != 0 || summary.release_unknown != 0) {
      return Fail(Error::kPlanIncomplete);
    }
    startup_complete_ = true;
    return {};
  }

  Result RequireRuntime() noexcept {
    if (!startup_complete_ || !healthy()) return Fail(Error::kWrongPhase);
    return {};
  }

  Result RequirePrepared() noexcept {
    if (!prepared_ || !healthy()) return Fail(Error::kWrongPhase);
    return {};
  }

  Result Take(size_t bytes, void** output) noexcept {
    if (output == nullptr) return Fail(Error::kInvalidPointer);
    *output = nullptr;
    if (!prepared_ || !healthy() || complete()) {
      return Fail(Error::kWrongPhase);
    }
    const size_t index = consumed_;
    if (plan_[index] != bytes) return Fail(Error::kSizeMismatch, 0, index);
    Block& block = blocks_[index];
    if (block.state != State::kUnhanded) return Fail(Error::kWrongPhase);
    block.state = State::kHanded;
    *output = block.pointer;
    ++consumed_;
    return {Error::kNone, 0, index};
  }

  // kNotOwned is not sticky: the caller can route ordinary later allocations
  // through the original allocator. Known freed pointers reject double-free.
  Result Release(void* pointer) noexcept {
    const size_t index = Find(pointer);
    if (index == blocks_.size()) return {Error::kNotOwned, 0, index};
    Block& block = blocks_[index];
    if (block.state == State::kFreed) {
      return Fail(Error::kAlreadyReleased, 0, index);
    }
    if (block.state == State::kUnknown) return Fail(Error::kRelease, 0, index);
    if (block.state != State::kHanded) {
      return Fail(Error::kNotHanded, 0, index);
    }
    return FreeBlock(index, ReleaseReason::kApplication);
  }

  // Runtime allocations made after the plan may reuse a physically freed
  // address. Retire that tombstone rather than misclassifying a later free.
  Result ObserveExternalAllocation(void* pointer) noexcept {
    if (pointer == nullptr) return {};
    const size_t index = Find(pointer);
    if (index == blocks_.size()) return {};
    if (blocks_[index].state != State::kFreed) {
      return Fail(Error::kInvalidPointer, 0, index);
    }
    blocks_[index].pointer = nullptr;
    return {};
  }

  bool Contains(void* pointer) const noexcept {
    return Find(pointer) != blocks_.size();
  }

  Result Reject(Error reason) noexcept { return Fail(reason); }

  Summary Snapshot() const noexcept {
    Summary result{error_, plan_.size(), allocated_, consumed_, freed_};
    result.prepared = prepared_;
    result.finalized = finalized_;
    for (const Block& block : blocks_) {
      result.unhanded_remaining += block.state == State::kUnhanded;
      result.handed_live += block.state == State::kHanded;
      result.release_unknown += block.state == State::kUnknown;
    }
    return result;
  }

  Summary Finalize() noexcept {
    if (finalized_) return Snapshot();
    if (!startup_complete_) Fail(Error::kPlanIncomplete);
    ReleaseUnhanded(ReleaseReason::kExitUnhanded);
    if (Snapshot().handed_live != 0) Fail(Error::kHandedStillLive);
    finalized_ = true;
    return Snapshot();
  }

 private:
  enum class State { kEmpty, kUnhanded, kHanded, kFreed, kUnknown };
  struct Block {
    void* pointer = nullptr;
    State state = State::kEmpty;
  };

  size_t Find(void* pointer) const noexcept {
    if (pointer != nullptr) {
      for (size_t i = 0; i < blocks_.size(); ++i) {
        if (blocks_[i].pointer == pointer) return i;
      }
    }
    return blocks_.size();
  }

  Result Fail(Error error, int rc = 0, size_t index = 0) noexcept {
    const Result result{error, rc, index};
    if (error_.ok()) error_ = result;
    return result;
  }

  Result FreeBlock(size_t index, ReleaseReason reason) noexcept {
    Block& block = blocks_[index];
    const int rc = backend_.Release(index, plan_[index], block.pointer, reason);
    if (rc != 0) {
      block.state = State::kUnknown;
      return Fail(Error::kRelease, rc, index);
    }
    block.state = State::kFreed;
    ++freed_;
    return {Error::kNone, 0, index};
  }

  void ReleaseUnhanded(ReleaseReason reason) noexcept {
    for (size_t ordinal = allocated_; ordinal > 0; --ordinal) {
      const size_t index = allocated_order_[ordinal - 1];
      if (blocks_[index].state == State::kUnhanded) FreeBlock(index, reason);
    }
  }

  std::vector<size_t> plan_;
  Backend& backend_;
  std::vector<Block> blocks_;
  std::vector<size_t> physical_order_;
  // Sized before allocation; recording successful ownership cannot allocate.
  std::vector<size_t> allocated_order_;
  Result error_;
  size_t allocated_ = 0;
  size_t consumed_ = 0;
  size_t freed_ = 0;
  bool started_ = false;
  bool prepared_ = false;
  bool finalized_ = false;
  bool startup_complete_ = false;
};

}  // namespace q4t::runtime::reservation
#endif  // Q4T_RUNTIME_RESERVATION_OWNER_H_
