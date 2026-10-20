// CPU-only failure contracts; no CUDA headers, library, or device access.
#include "owner.h"

#include <array>
#include <cstdio>
#include <map>
#include <stdexcept>
#include <vector>

namespace reserve = q4t::verify::device_reserve;

namespace {
void Check(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

struct FakeBackend {
  int fail_allocate = -1;
  int fail_release = -1;
  int allocate_calls = 0;
  int release_calls = 0;
  std::map<void*, size_t> live;
  std::vector<size_t> freed_indices;
  std::vector<reserve::ReleaseReason> reasons;

  ~FakeBackend() {
    for (auto [pointer, _] : live) delete[] static_cast<char*>(pointer);
  }

  int Allocate(size_t index, size_t bytes, void** output) {
    ++allocate_calls;
    if (static_cast<int>(index) == fail_allocate) return 2;
    *output = new char[bytes];
    Check(live.emplace(*output, index).second, "fake reused live allocation");
    return 0;
  }

  int Release(size_t index, size_t, void* pointer,
              reserve::ReleaseReason reason) {
    ++release_calls;
    Check(live.contains(pointer), "physical double-free");
    if (static_cast<int>(index) == fail_release) return 7;
    delete[] static_cast<char*>(pointer);
    live.erase(pointer);
    freed_indices.push_back(index);
    reasons.push_back(reason);
    return 0;
  }
};

void CompleteLifecycle() {
  FakeBackend backend;
  const std::array<size_t, 2> plan{16, 32};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  Check(backend.allocate_calls == 2, "reserve all before handoff");
  void* first = nullptr;
  void* second = nullptr;
  Check(owner.Take(16, &first).ok() && owner.Take(32, &second).ok(), "take");
  Check(owner.Release(first).ok() && owner.Release(second).ok(), "release");
  const auto done = owner.Finalize();
  Check(done.result.ok() && done.consumed == 2 && done.freed == 2, "finalize");
  Check(owner.Finalize().result.ok() && backend.release_calls == 2,
        "idempotent finalize");
}

void AllocationFailureRollsBack() {
  FakeBackend backend;
  backend.fail_allocate = 2;
  const std::array<size_t, 3> plan{16, 32, 48};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().error == reserve::Error::kAllocation, "alloc error");
  Check(backend.freed_indices == std::vector<size_t>{1, 0}, "reverse rollback");
  const auto done = owner.Finalize();
  Check(!done.result.ok() && done.unhanded_remaining == 0 &&
            done.handed_live == 0 && backend.release_calls == 2,
        "rollback final status");
}

void WrongSizeFailsAndReleasesUnhanded() {
  FakeBackend backend;
  const std::array<size_t, 2> plan{16, 32};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(32, &pointer).error == reserve::Error::kSizeMismatch,
        "size mismatch accepted");
  Check(pointer == nullptr && owner.consumed() == 0, "mismatch handed pointer");
  const auto done = owner.Finalize();
  Check(!done.result.ok() && done.freed == 2 && backend.live.empty(),
        "mismatch cleanup");
}

void PartialConsumptionCannotPass() {
  FakeBackend backend;
  const std::array<size_t, 2> plan{16, 32};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(16, &pointer).ok() && owner.Release(pointer).ok(),
        "take/free");
  const auto done = owner.Finalize();
  Check(done.result.error == reserve::Error::kPlanIncomplete && done.freed == 2,
        "unconsumed plan silently accepted");
  Check(backend.reasons.back() == reserve::ReleaseReason::kExitUnhanded,
        "unused pointer cleanup reason");
}

void HandedOwnershipIsNotStolen() {
  FakeBackend backend;
  const std::array<size_t, 2> plan{16, 32};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(16, &pointer).ok(), "take");
  const auto done = owner.Finalize();
  Check(!done.result.ok() && done.handed_live == 1 && done.freed == 1 &&
            backend.live.contains(pointer),
        "atexit stole application ownership");
  Check(owner.Release(pointer).ok(),
        "application cannot release after finalize");
  Check(backend.release_calls == 2, "double-free after finalize");
}

void FullConsumptionNeedsRelease() {
  FakeBackend backend;
  const std::array<size_t, 1> plan{16};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(16, &pointer).ok(), "take");
  Check(owner.Finalize().result.error == reserve::Error::kHandedStillLive,
        "live handed pointer accepted");
  Check(backend.release_calls == 0, "handed pointer stolen");
  Check(owner.Release(pointer).ok(), "release");
}

void DoubleFreeRejected() {
  FakeBackend backend;
  const std::array<size_t, 1> plan{16};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(16, &pointer).ok() && owner.Release(pointer).ok(),
        "take/free");
  Check(owner.Release(pointer).error == reserve::Error::kAlreadyReleased,
        "duplicate release accepted");
  Check(backend.release_calls == 1 && !owner.Finalize().result.ok(),
        "duplicate release was forwarded");
}

void UnknownReleaseIsNeverRetried() {
  FakeBackend backend;
  backend.fail_release = 0;
  const std::array<size_t, 1> plan{16};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(16, &pointer).ok(), "take");
  Check(owner.Release(pointer).error == reserve::Error::kRelease,
        "release error");
  Check(owner.Release(pointer).error == reserve::Error::kRelease,
        "unknown free");
  const auto done = owner.Finalize();
  Check(done.release_unknown == 1 && backend.release_calls == 1,
        "unknown release retried");
}

void RuntimeReuseRetiresTombstone() {
  FakeBackend backend;
  const std::array<size_t, 1> plan{16};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "prepare");
  void* pointer = nullptr;
  Check(owner.Take(16, &pointer).ok() && owner.Release(pointer).ok(),
        "take/free");
  Check(owner.ObserveExternalAllocation(pointer).ok(), "reuse observation");
  Check(!owner.Contains(pointer) &&
            owner.Release(pointer).error == reserve::Error::kNotOwned,
        "runtime reuse misidentified as reserved");
  Check(owner.Finalize().result.ok(), "runtime reuse changed plan accounting");
}

void PinnedBeforeReserveIsRejected() {
  FakeBackend backend;
  const std::array<size_t, 1> plan{16};
  reserve::Owner owner(plan, backend);
  Check(!owner.RequirePrepared().ok() && !owner.Prepare().ok(), "early pinned");
  Check(backend.allocate_calls == 0 && !owner.Finalize().result.ok(),
        "invalid early call recovered silently");
}

void ZeroBytePlanIsRejected() {
  FakeBackend backend;
  const std::array<size_t, 2> plan{16, 0};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().error == reserve::Error::kInvalidPlan &&
            backend.allocate_calls == 0, "zero-size plan accepted");
}
}  // namespace

int main() {
  const std::array tests{CompleteLifecycle, AllocationFailureRollsBack,
                        WrongSizeFailsAndReleasesUnhanded,
                        PartialConsumptionCannotPass,
                        HandedOwnershipIsNotStolen,
                        FullConsumptionNeedsRelease, DoubleFreeRejected,
                        UnknownReleaseIsNeverRetried,
                        RuntimeReuseRetiresTombstone,
                        PinnedBeforeReserveIsRejected, ZeroBytePlanIsRejected};
  try {
    for (auto test : tests) test();
  } catch (const std::exception& error) {
    std::fprintf(stderr, "owner host contract FAILED: %s\n", error.what());
    return 1;
  }
  std::printf("{\"status\":\"PASS\",\"host_contracts\":%zu}\n", tests.size());
}
