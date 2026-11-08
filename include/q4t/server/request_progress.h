// Fixed-capacity observation. Never waits on inference, allocates, or does I/O.
#pragma once

#include <time.h>

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <utility>

namespace q4t::server {

inline uint64_t ProgressNowNs() noexcept {
  timespec now{};
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0 || now.tv_sec < 0) return 0;
  return static_cast<uint64_t>(now.tv_sec) * 1000000000 + now.tv_nsec;
}

enum class ProgressStage : uint64_t {
  kInput, kMedia, kGrammar, kAdmission, kCache, kPrefill, kDecode, kOutput,
  kStorage, kCleanup, kCount
};
enum class ProgressWait : uint64_t {
  kNone, kSlot, kModel, kTokenizer, kScheduler, kWrite, kGrammar, kMedia,
  kStorage, kCache, kCount
};
enum class ProgressWork : uint64_t {
  kNone, kLoad, kPleRead, kMedia, kCache, kPrefill, kDecode, kDraft, kCount
};

struct ProgressRow {
  uint64_t ticket = 0;
  uint64_t deadline_ns = 0;
  ProgressStage stage = ProgressStage::kInput;
  uint64_t stage_since_ns = 0;
  ProgressWait wait = ProgressWait::kNone;
  uint64_t wait_since_ns = 0;
  ProgressWork work = ProgressWork::kNone;
  uint64_t work_id = 0;
  uint64_t work_since_ns = 0;
  uint64_t completed = 0;
  uint64_t last_completed_ns = 0;
  uint64_t updated_ns = 0;
};

class RequestProgress;
class ProgressHandle;

// Atomic fields and a bounded sequence check avoid data races even when a
// publisher is stopped midway through updating a row. Sequential consistency
// makes the read-before/read-after validation unambiguous in the C++ model.
class ProgressCell {
 private:
  friend class RequestProgress;
  friend class ProgressHandle;
  friend struct ProgressTestAccess;
  enum Field { kDeadline, kStage, kStageSince, kWait, kWaitSince, kWork,
               kWorkId, kWorkSince, kCompleted, kLastCompleted, kUpdated,
               kFieldCount };
  static constexpr uint64_t kReserved = std::numeric_limits<uint64_t>::max();
  std::atomic<uint64_t> ticket_{0};
  std::atomic<uint64_t> sequence_{0};
  std::atomic<bool> invalid_{false};
  std::array<std::atomic<uint64_t>, kFieldCount> fields_{};

  template <typename Change>
  bool Write(uint64_t ticket, uint64_t now, Change change) noexcept {
    if (ticket_.load() != ticket) return false;
    auto before = sequence_.load();
    if ((before & 1) ||
        !sequence_.compare_exchange_strong(before, before + 1)) {
      // A lost update must never masquerade as a coherent, stalled worker.
      // Keep this row untrusted for its lifetime; business still proceeds.
      if (ticket_.load() == ticket) invalid_.store(true);
      return false;
    }
    const bool owned = ticket_.load() == ticket;
    bool valid = owned && now != 0 && now >= fields_[kUpdated].load();
    if (valid) {
      valid = change();
      fields_[kUpdated].store(now);
    }
    if (owned && !valid) invalid_.store(true);
    sequence_.store(before + 2);
    return valid;
  }

  bool Reset(uint64_t deadline, uint64_t now) noexcept {
    auto before = sequence_.load();
    if ((before & 1) ||
        !sequence_.compare_exchange_strong(before, before + 1)) return false;
    for (auto& field : fields_) field.store(0);
    fields_[kDeadline].store(deadline);
    fields_[kStageSince].store(now);
    fields_[kWaitSince].store(now);
    fields_[kUpdated].store(now);
    invalid_.store(now == 0);
    sequence_.store(before + 2);
    return true;
  }

  // 0 absent, 1 coherent, -1 unknown. No retries/spins on the observer path.
  int Read(ProgressRow* row) const noexcept {
    const auto ticket = ticket_.load();
    if (!ticket) return 0;
    if (ticket == kReserved) return -1;
    const auto before = sequence_.load();
    if ((before & 1) || invalid_.load()) return -1;
    row->ticket = ticket;
    row->deadline_ns = fields_[kDeadline].load();
    row->stage = static_cast<ProgressStage>(fields_[kStage].load());
    row->stage_since_ns = fields_[kStageSince].load();
    row->wait = static_cast<ProgressWait>(fields_[kWait].load());
    row->wait_since_ns = fields_[kWaitSince].load();
    row->work = static_cast<ProgressWork>(fields_[kWork].load());
    row->work_id = fields_[kWorkId].load();
    row->work_since_ns = fields_[kWorkSince].load();
    row->completed = fields_[kCompleted].load();
    row->last_completed_ns = fields_[kLastCompleted].load();
    row->updated_ns = fields_[kUpdated].load();
    return sequence_.load() == before && ticket_.load() == ticket &&
                   !invalid_.load() ? 1 : -1;
  }
};

// Borrow only while the owner lease is alive. Ticket verification prevents a
// delayed callback from changing a replacement request that reused its slot.
class ProgressHandle {
 public:
  ProgressHandle() = default;
  bool valid() const noexcept {
    return cell_ && cell_->ticket_.load() == ticket_;
  }
  uint64_t ticket() const noexcept { return ticket_; }
  bool Stage(ProgressStage stage, uint64_t now = ProgressNowNs()) const noexcept {
    return Update(now, [&] {
      if (stage >= ProgressStage::kCount) return false;
      if (cell_->fields_[ProgressCell::kStage].load() != uint64_t(stage)) {
        cell_->fields_[ProgressCell::kStage].store(uint64_t(stage));
        cell_->fields_[ProgressCell::kStageSince].store(now);
      }
      return true;
    });
  }
  bool Wait(ProgressWait wait, uint64_t now = ProgressNowNs()) const noexcept {
    return Update(now, [&] {
      if (wait >= ProgressWait::kCount) return false;
      if (cell_->fields_[ProgressCell::kWait].load() != uint64_t(wait)) {
        cell_->fields_[ProgressCell::kWait].store(uint64_t(wait));
        cell_->fields_[ProgressCell::kWaitSince].store(now);
      }
      return true;
    });
  }
  bool Begin(ProgressWork work,
             uint64_t now = ProgressNowNs()) const noexcept {
    return Update(now, [&] {
      if (work == ProgressWork::kNone || work >= ProgressWork::kCount ||
          cell_->fields_[ProgressCell::kWork].load() != 0) return false;
      cell_->fields_[ProgressCell::kWork].store(uint64_t(work));
      cell_->fields_[ProgressCell::kWorkId].fetch_add(1);
      cell_->fields_[ProgressCell::kWorkSince].store(now);
      return true;
    });
  }
  // Call only after a successful actual completion boundary, never after
  // submission or on a heartbeat. A chunk may complete within a longer work.
  bool Completed(uint64_t now = ProgressNowNs()) const noexcept {
    return Update(now, [&] {
      if (cell_->fields_[ProgressCell::kWork].load() == 0) return false;
      cell_->fields_[ProgressCell::kCompleted].fetch_add(1);
      cell_->fields_[ProgressCell::kLastCompleted].store(now);
      return true;
    });
  }
  // Ending/aborting a drained operation does not assert it succeeded.
  bool End(uint64_t now = ProgressNowNs()) const noexcept {
    return Update(now, [&] {
      if (cell_->fields_[ProgressCell::kWork].load() == 0) return false;
      cell_->fields_[ProgressCell::kWork].store(0);
      return true;
    });
  }

 private:
  friend class RequestProgress;
  friend struct ProgressTestAccess;
  ProgressHandle(ProgressCell* cell, uint64_t ticket)
      : cell_(cell), ticket_(ticket) {}
  template <typename Change>
  bool Update(uint64_t now, Change change) const noexcept {
    return cell_ && cell_->Write(ticket_, now, change);
  }
  ProgressCell* cell_ = nullptr;
  uint64_t ticket_ = 0;
};

class RequestProgress {
 public:
  static constexpr size_t kCapacity = 1024;
  struct Snapshot {
    uint64_t observed_ns = 0;
    uint64_t active = 0;
    uint64_t untracked = 0;
    bool complete = false;
    size_t count = 0;
    std::array<ProgressRow, kCapacity> rows{};
  };
  class Lease {
   public:
    Lease() = default;
    Lease(const Lease&) = delete;
    Lease& operator=(const Lease&) = delete;
    Lease(Lease&& other) noexcept
        : owner_(std::exchange(other.owner_, nullptr)),
          handle_(std::exchange(other.handle_, {})) {}
    Lease& operator=(Lease&& other) noexcept {
      if (this != &other) {
        Release();
        owner_ = std::exchange(other.owner_, nullptr);
        handle_ = std::exchange(other.handle_, {});
      }
      return *this;
    }
    ~Lease() { Release(); }
    ProgressHandle Handle() const noexcept { return handle_; }
    void Release() noexcept {
      if (!owner_) return;
      if (handle_.cell_) {
        auto ticket = handle_.ticket_;
        handle_.cell_->ticket_.compare_exchange_strong(ticket, uint64_t{0});
      } else {
        owner_->untracked_.fetch_sub(1);
      }
      owner_->active_.fetch_sub(1);
      owner_ = nullptr;
      handle_ = {};
    }

   private:
    friend class RequestProgress;
    Lease(RequestProgress* owner, ProgressHandle handle)
        : owner_(owner), handle_(handle) {}
    RequestProgress* owner_ = nullptr;
    ProgressHandle handle_;
  };

  Lease Acquire(uint64_t deadline_ns, uint64_t now = ProgressNowNs()) noexcept {
    active_.fetch_add(1);
    uint64_t ticket = next_.load();
    bool numbered = false;
    for (int attempt = 0; attempt < 8 && ticket < ProgressCell::kReserved;
         ++attempt) {
      if (next_.compare_exchange_strong(ticket, ticket + 1)) {
        numbered = true;
        break;
      }
    }
    if (numbered) {
      for (auto& cell : cells_) {
        uint64_t empty = 0;
        if (!cell.ticket_.compare_exchange_strong(empty,
                                                 ProgressCell::kReserved))
          continue;
        if (cell.Reset(deadline_ns, now)) {
          cell.ticket_.store(ticket);
          return Lease(this, ProgressHandle(&cell, ticket));
        }
        cell.ticket_.store(0);
      }
    }
    // Exhaustion affects observation only; the caller's existing admission
    // contract decides whether its business operation can proceed.
    untracked_.fetch_add(1);
    return Lease(this, {});
  }

  Snapshot Read(uint64_t now = ProgressNowNs()) const noexcept {
    Snapshot result;
    result.observed_ns = now;
    result.active = active_.load();
    result.untracked = untracked_.load();
    result.complete = now != 0 && result.untracked == 0;
    for (const auto& cell : cells_) {
      ProgressRow row;
      const int state = cell.Read(&row);
      if (state < 0) result.complete = false;
      if (state > 0) {
        if (row.updated_ns > now) result.complete = false;
        result.rows[result.count++] = row;
      }
    }
    result.complete = result.complete && result.active == result.count &&
                      result.active == active_.load() &&
                      result.untracked == untracked_.load();
    return result;
  }

 private:
  std::array<ProgressCell, kCapacity> cells_{};
  std::atomic<uint64_t> next_{1};
  std::atomic<uint64_t> active_{0};
  std::atomic<uint64_t> untracked_{0};
};

}  // namespace q4t::server
