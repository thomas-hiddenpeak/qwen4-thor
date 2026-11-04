// Standalone host-only contracts. No CUDA runtime is linked or initialized.
#include "q4t/runtime/allocation_quota.h"

#include <atomic>
#include <barrier>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>
#include <thread>

#include "q4t/runtime/reservation_owner.h"

namespace {
namespace quota = q4t::runtime::quota;
using quota::Error;
using quota::Kind;
using quota::Release;
thread_local bool count_new = false;
std::atomic<int> new_calls{0};

void Check(bool condition, const char* message) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", message);
    std::exit(1);
  }
}

void* Pointer(size_t index) {
  return reinterpret_cast<void*>(index * 4096 + 4096);
}

template <size_t N>
void Charge(const quota::Ledger<N>& ledger, uint64_t bytes, size_t count) {
  const auto s = ledger.Snapshot();
  Check(s.charged_bytes == bytes && s.entries == count, "charge/count");
  Check(s.charged_bytes <= s.limit_bytes && s.peak_bytes <= s.limit_bytes,
        "quota ceiling");
  Check(s.charged_bytes == s.reserved_bytes + s.live_bytes + s.releasing_bytes +
                               s.pending_bytes + s.unknown_bytes,
        "state partition");
}

struct Backend {
  int allocations = 0;
  int frees = 0;
  int synchronizations = 0;
  int allocation_error = 0;
  int release_error = 0;
  int sync_error = 0;
  bool pointer_on_error = false;
  bool null_on_success = false;
  void* forced_pointer = nullptr;
  Kind last_kind = Kind::kDevice;
  uint64_t last_queue = 0;
  size_t last_bytes = 0;

  int Allocate(Kind kind, size_t bytes, void** pointer,
               uint64_t queue) noexcept {
    ++allocations;
    last_kind = kind;
    last_bytes = bytes;
    last_queue = queue;
    if ((allocation_error && !pointer_on_error) || null_on_success)
      *pointer = nullptr;
    else
      *pointer = forced_pointer ? forced_pointer : Pointer(allocations);
    return allocation_error;
  }
  int Release(quota::Release, void*, uint64_t queue) noexcept {
    ++frees;
    last_queue = queue;
    return release_error;
  }
  int Synchronize(uint64_t queue) noexcept {
    ++synchronizations;
    last_queue = queue;
    return sync_error;
  }
};

template <size_t N = 8>
struct Fixture {
  explicit Fixture(uint64_t limit = 100)
      : ledger(limit), api(ledger, backend) {}
  quota::Ledger<N> ledger;
  Backend backend;
  quota::Allocator<Backend, N> api;

  void* Allocate(size_t bytes, Kind kind = Kind::kDevice, uint64_t queue = 0) {
    void* pointer = Pointer(99);
    Check(api.Allocate(kind, bytes, &pointer, queue).ok() && pointer,
          "allocation");
    return pointer;
  }
};

void MixedLimit() {
  Fixture fixture(50);
  const std::array kinds{Kind::kDevice, Kind::kHostAlloc, Kind::kMallocHost,
                         Kind::kManaged, Kind::kAsyncDevice};
  std::array<void*, 5> pointers{};
  for (size_t i = 0; i < kinds.size(); ++i) {
    pointers[i] = fixture.Allocate(10, kinds[i], i == 4 ? 7 : 0);
    Check(fixture.backend.last_kind == kinds[i] &&
              fixture.backend.last_bytes == 10,
          "backend arguments");
  }
  void* denied = Pointer(90);
  const auto result = fixture.api.Allocate(Kind::kDevice, 1, &denied);
  Check(result.error == Error::kByteLimit && !denied &&
            fixture.backend.allocations == 5,
        "mixed quota before backend");
  Charge(fixture.ledger, 50, 5);
  for (size_t i = 0; i < pointers.size(); ++i) {
    Release release = i == 1 || i == 2 ? Release::kHost : Release::kDevice;
    if (i == 4) release = Release::kAsync;
    Check(fixture.api.Free(pointers[i], release, i == 4 ? 7 : 0).ok(), "free");
  }
  Charge(fixture.ledger, 10, 1);
  Check(fixture.api.Synchronize(7).ok(), "async completion");
  Charge(fixture.ledger, 0, 0);
}

void ByteOverflow() {
  quota::Ledger<3> ledger(std::numeric_limits<uint64_t>::max());
  auto a =
      ledger.Reserve(std::numeric_limits<uint64_t>::max() - 5, Kind::kDevice);
  Check(a.ok(), "large reservation");
  Check(ledger.Reserve(6, Kind::kHostAlloc).result.error == Error::kByteLimit,
        "overflow-safe rejection");
  Check(ledger.Reserve(5, Kind::kManaged).ok(), "exact max");
  Check(ledger.Reserve(1, Kind::kDevice).result.error == Error::kByteLimit,
        "maximum ceiling");
  Charge(ledger, std::numeric_limits<uint64_t>::max(), 2);
}

void EntryLimit() {
  Fixture<1> fixture;
  void* p = fixture.Allocate(1);
  void* rejected = nullptr;
  Check(fixture.api.Allocate(Kind::kDevice, 1, &rejected).error ==
                Error::kEntryLimit &&
            fixture.backend.allocations == 1,
        "entry bound before backend");
  Check(fixture.api.Free(p, Release::kDevice).ok(), "slot release");
  fixture.Allocate(1);
  Charge(fixture.ledger, 1, 1);
}

void InvalidArguments() {
  Fixture fixture;
  void* p = Pointer(90);
  Check(fixture.api.Allocate(Kind::kDevice, 1, nullptr).error ==
            Error::kInvalidArgument,
        "null output");
  Check(fixture.api.Allocate(Kind::kDevice, 0, &p).error ==
                Error::kInvalidArgument &&
            !p,
        "zero size");
  Check(fixture.api.Allocate(Kind::kAsyncDevice, 1, &p).error ==
            Error::kInvalidArgument,
        "async missing queue");
  Check(fixture.api.Allocate(Kind::kDevice, 1, &p, 1).error ==
            Error::kInvalidArgument,
        "unexpected queue");
  Check(fixture.ledger.Reserve(1, static_cast<Kind>(99)).result.error ==
            Error::kInvalidArgument,
        "invalid kind");
  Check(fixture.api.Free(nullptr, Release::kDevice).error ==
            Error::kInvalidArgument,
        "null release is boundary concern");
  quota::Ledger<1> zero(0);
  Check(!zero.Reserve(1, Kind::kDevice).ok(), "zero limit not bypass");
  Check(fixture.backend.allocations == 0 && fixture.backend.frees == 0,
        "invalid requests never reach backend");
}

void AllocationFailure() {
  Fixture fixture;
  fixture.backend.allocation_error = 2;
  void* p = Pointer(90);
  const auto result = fixture.api.Allocate(Kind::kDevice, 100, &p);
  Check(
      result.error == Error::kBackendFailure && result.backend_error == 2 && !p,
      "original allocation error");
  Charge(fixture.ledger, 0, 0);
  Check(!fixture.ledger.Snapshot().poisoned, "known failure recoverable");
  fixture.backend.allocation_error = 0;
  fixture.Allocate(100);
}

void AllocationUnknown() {
  Fixture fixture;
  fixture.backend.allocation_error = 700;
  fixture.backend.pointer_on_error = true;
  void* p = nullptr;
  auto result = fixture.api.Allocate(Kind::kDevice, 10, &p);
  Check(result.error == Error::kOwnershipUnknown &&
            result.backend_error == 700 && !p,
        "unknown output not handed");
  Check(fixture.ledger.Snapshot().unknown_bytes == 10, "unknown charged");
  result = fixture.api.Allocate(Kind::kDevice, 1, &p);
  Check(result.error == Error::kOwnershipUnknown &&
            fixture.backend.allocations == 1 && fixture.backend.frees == 0,
        "unknown never auto-free/retry");
}

void NullSuccess() {
  Fixture fixture;
  fixture.backend.null_on_success = true;
  void* p = nullptr;
  Check(fixture.api.Allocate(Kind::kDevice, 10, &p).error ==
            Error::kOwnershipUnknown,
        "successful null is impossible");
  Charge(fixture.ledger, 10, 1);
}

void DuplicateLive() {
  Fixture fixture;
  const auto p = fixture.Allocate(10);
  fixture.backend.forced_pointer = p;
  void* duplicate = nullptr;
  Check(fixture.api.Allocate(Kind::kDevice, 10, &duplicate).error ==
                Error::kOwnershipUnknown &&
            !duplicate,
        "duplicate live");
  Check(fixture.ledger.Snapshot().unknown_bytes == 20, "both aliases unknown");
  Check(
      !fixture.api.Free(p, Release::kDevice).ok() && fixture.backend.frees == 0,
      "no ambiguous alias release");
}

void TicketGeneration() {
  quota::Ledger<1> ledger(10);
  const auto old = ledger.Reserve(10, Kind::kDevice);
  Check(ledger.CommitAllocation(old.ticket, nullptr, 2).error ==
            Error::kBackendFailure,
        "first rollback");
  const auto fresh = ledger.Reserve(10, Kind::kDevice);
  Check(old.ticket.index == fresh.ticket.index &&
            old.ticket.generation != fresh.ticket.generation,
        "reused slot");
  Check(ledger.CommitAllocation(old.ticket, Pointer(1), 0).error ==
            Error::kStaleTicket,
        "old ticket blocked");
  Check(ledger.CommitAllocation({}, nullptr, 2).error == Error::kStaleTicket,
        "default ticket invalid");
  Check(ledger.CommitAllocation(fresh.ticket, Pointer(1), 0).ok(), "fresh");
  Check(ledger.CommitAllocation(fresh.ticket, nullptr, 2).error ==
            Error::kWrongState,
        "repeated commit rejected");
  Charge(ledger, 10, 1);
}

void ReleaseClaim() {
  Fixture fixture;
  const auto p = fixture.Allocate(10);
  auto first = fixture.ledger.BeginRelease(p, Release::kDevice);
  Check(first.ok(), "release claim");
  Check(fixture.api.Free(p, Release::kDevice).error == Error::kWrongState &&
            fixture.backend.frees == 0,
        "concurrent second free blocked");
  Check(fixture.ledger.Snapshot().releasing_bytes == 10, "in-flight charged");
  Check(fixture.ledger.CommitRelease(first.ticket, 0).ok(), "release done");
  Check(fixture.ledger.CommitRelease(first.ticket, 0).error ==
            Error::kStaleTicket,
        "double credit blocked");
  Charge(fixture.ledger, 0, 0);
}

void ReleaseFailure() {
  Fixture fixture;
  const auto p = fixture.Allocate(10);
  const auto healthy = fixture.Allocate(20);
  fixture.backend.release_error = 700;
  const auto result = fixture.api.Free(p, Release::kDevice);
  Check(result.error == Error::kOwnershipUnknown && result.backend_error == 700,
        "release error preserved");
  fixture.backend.release_error = 0;
  Check(
      !fixture.api.Free(p, Release::kDevice).ok() && fixture.backend.frees == 1,
      "failed release not retried");
  Check(fixture.api.Free(healthy, Release::kDevice).ok(),
        "unrelated known owner may clean up");
  Charge(fixture.ledger, 10, 1);
  Check(fixture.ledger.Snapshot().unknown_bytes == 10, "retained unknown");
}

void ReleaseTypes() {
  const std::array kinds{Kind::kDevice, Kind::kHostAlloc, Kind::kMallocHost,
                         Kind::kManaged, Kind::kAsyncDevice};
  const std::array releases{Release::kDevice, Release::kHost, Release::kAsync};
  const bool expected[5][3]{{true, false, true},
                            {false, true, false},
                            {false, true, false},
                            {true, false, false},
                            {false, false, true}};
  for (size_t i = 0; i < kinds.size(); ++i) {
    for (size_t j = 0; j < releases.size(); ++j) {
      Fixture fixture;
      const auto p = fixture.Allocate(10, kinds[i], i == 4 ? 1 : 0);
      auto result = fixture.api.Free(p, releases[j], j == 2 ? 1 : 0);
      Check(result.ok() == expected[i][j], "release type contract");
      Check(fixture.backend.frees == int(expected[i][j]), "free dispatch");
    }
  }
}

void ForeignPointer() {
  Fixture fixture;
  Check(fixture.api.Free(Pointer(70), Release::kDevice).error ==
                Error::kUnknownPointer &&
            fixture.backend.frees == 0,
        "foreign pointer");
  Charge(fixture.ledger, 0, 0);
}

void AsyncRemainsCharged() {
  Fixture fixture(10);
  const auto p = fixture.Allocate(10, Kind::kAsyncDevice, 1);
  Check(fixture.api.Free(p, Release::kAsync, 1).ok(), "async submit");
  Check(fixture.ledger.Snapshot().pending_bytes == 10, "pending charged");
  void* denied = nullptr;
  Check(fixture.api.Allocate(Kind::kDevice, 1, &denied).error ==
            Error::kByteLimit,
        "no early credit");
  Check(fixture.api.Synchronize(1).ok(), "completion");
  Charge(fixture.ledger, 0, 0);
  fixture.Allocate(10);
}

void CrossQueue() {
  Fixture fixture;
  const auto a = fixture.Allocate(10, Kind::kAsyncDevice, 1);
  const auto b = fixture.Allocate(20, Kind::kAsyncDevice, 2);
  Check(fixture.api.Free(a, Release::kAsync, 1).ok(), "free a");
  Check(fixture.api.Free(b, Release::kAsync, 2).ok(), "free b");
  Check(fixture.api.Synchronize(1).ok(), "stream a complete");
  Charge(fixture.ledger, 20, 1);
  Check(fixture.api.Synchronize().ok(), "device complete");
  Charge(fixture.ledger, 0, 0);
}

void LaterSubmission() {
  Fixture fixture;
  const auto a = fixture.Allocate(10, Kind::kAsyncDevice, 1);
  const auto b = fixture.Allocate(20, Kind::kAsyncDevice, 1);
  Check(fixture.api.Free(a, Release::kAsync, 1).ok(), "first submit");
  const auto fence = fixture.ledger.Capture(1);
  Check(fixture.api.Free(b, Release::kAsync, 1).ok(), "later submit");
  Check(fixture.ledger.Complete(fence, 0).ok(), "old wait complete");
  Charge(fixture.ledger, 20, 1);
  Check(fixture.ledger.Complete(fence, 0).ok(), "repeat wait inert");
  Charge(fixture.ledger, 20, 1);
  Check(fixture.api.Synchronize(1).ok(), "new wait");
  Charge(fixture.ledger, 0, 0);
}

void InflightSubmission() {
  Fixture fixture;
  const auto p = fixture.Allocate(10, Kind::kAsyncDevice, 1);
  auto release = fixture.ledger.BeginRelease(p, Release::kAsync, 1);
  const auto fence = fixture.ledger.Capture(1);
  Check(fixture.ledger.CommitRelease(release.ticket, 0).ok(), "late publish");
  Check(fixture.ledger.Complete(fence, 0).ok(), "old capture");
  Charge(fixture.ledger, 10, 1);
  Check(fixture.api.Synchronize(1).ok(), "after publication");
  Charge(fixture.ledger, 0, 0);
}

void QueueGeneration() {
  Fixture fixture;
  const auto p = fixture.Allocate(10, Kind::kAsyncDevice, 100);
  Check(fixture.api.Free(p, Release::kAsync, 100).ok(), "old stream retire");
  // A reused CUDA handle must be assigned a distinct token by the integrator.
  Check(fixture.api.Synchronize(101).ok(), "new stream same raw handle");
  Charge(fixture.ledger, 10, 1);
  Check(fixture.api.Synchronize().ok(), "device covers old stream too");
  Charge(fixture.ledger, 0, 0);
}

void AddressReuse() {
  Fixture fixture;
  const auto p = fixture.Allocate(10, Kind::kAsyncDevice, 1);
  Check(fixture.api.Free(p, Release::kAsync, 1).ok(), "old address pending");
  fixture.backend.forced_pointer = p;
  const auto again = fixture.Allocate(20, Kind::kAsyncDevice, 1);
  Check(again == p, "address reused");
  Charge(fixture.ledger, 30, 2);
  Check(fixture.api.Free(again, Release::kAsync, 1).ok(), "new live selected");
  Check(fixture.ledger.Snapshot().pending_bytes == 30, "both pending");
  Check(fixture.api.Synchronize(1).ok(), "both completed");
  Charge(fixture.ledger, 0, 0);
}

void CompletionFailure() {
  Fixture fixture;
  const auto p = fixture.Allocate(10, Kind::kAsyncDevice, 1);
  Check(fixture.api.Free(p, Release::kAsync, 1).ok(), "retire");
  fixture.backend.sync_error = 719;
  const auto result = fixture.api.Synchronize(1);
  Check(result.error == Error::kCompletionFailed && result.backend_error == 719,
        "sync error kept");
  Check(fixture.ledger.Snapshot().unknown_bytes == 10, "unknown completion");
  fixture.backend.sync_error = 0;
  Check(fixture.api.Synchronize().ok(), "later success");
  Charge(fixture.ledger, 10, 1);
  Check(fixture.ledger.Snapshot().poisoned, "later success not forgiveness");
}

void ReleasePublicationRace() {
  for (int release_error : {0, 700}) {
    Fixture fixture;
    const auto pointer = fixture.Allocate(10, Kind::kAsyncDevice, 1);
    const auto releasing =
        fixture.ledger.BeginRelease(pointer, Release::kAsync, 1);
    Check(releasing.ok(), "old release claimed");
    // CUDA can recycle the address after the free was submitted but before
    // that host thread resumes to publish its result to the quota ledger.
    fixture.backend.forced_pointer = pointer;
    const auto again = fixture.Allocate(20, Kind::kAsyncDevice, 2);
    Check(again == pointer, "reuse during release publication");
    Charge(fixture.ledger, 30, 2);
    const auto result =
        fixture.ledger.CommitRelease(releasing.ticket, release_error);
    if (release_error) {
      Check(result.error == Error::kOwnershipUnknown &&
                result.backend_error == release_error &&
                fixture.ledger.Snapshot().unknown_bytes == 30,
            "ambiguous release poisons both address generations");
      Check(!fixture.api.Free(again, Release::kAsync, 2).ok() &&
                fixture.backend.frees == 0,
            "unknown alias must not be released again");
    } else {
      Check(result.ok(), "old release published");
      Check(fixture.api.Synchronize(1).ok(), "old generation completes");
      Charge(fixture.ledger, 20, 1);
      Check(fixture.api.Free(again, Release::kAsync, 2).ok(), "new release");
      Check(fixture.api.Synchronize(2).ok(), "new generation completes");
      Charge(fixture.ledger, 0, 0);
    }
  }
}

void ForeignFence() {
  Fixture a;
  Fixture b;
  const auto first = a.ledger.Reserve(10, Kind::kDevice);
  Check(b.ledger.Reserve(10, Kind::kDevice).ok(),
        "matching foreign generation");
  Check(b.ledger.CommitAllocation(first.ticket, Pointer(1), 0).error ==
            Error::kStaleTicket,
        "foreign ticket");
  Check(
      b.ledger.Complete(a.ledger.Capture(), 0).error == Error::kInvalidArgument,
      "foreign fence");
  Check(a.ledger.Complete({}, 0).error == Error::kInvalidArgument,
        "default fence");
}

void ConcurrentReservations() {
  quota::Ledger<16> ledger(40);
  std::barrier gate(9);
  std::array<quota::Admission, 8> admissions{};
  std::array<std::thread, 8> workers;
  for (size_t i = 0; i < workers.size(); ++i) {
    workers[i] = std::thread([&, i] {
      gate.arrive_and_wait();
      admissions[i] = ledger.Reserve(10, Kind::kDevice);
      gate.arrive_and_wait();
      gate.arrive_and_wait();
      if (admissions[i].ok())
        Check(ledger.CommitAllocation(admissions[i].ticket, nullptr, 2).error ==
                  Error::kBackendFailure,
              "concurrent rollback");
    });
  }
  gate.arrive_and_wait();
  gate.arrive_and_wait();
  size_t admitted = 0;
  for (const auto& a : admissions) {
    if (a.ok())
      ++admitted;
    else
      Check(a.result.error == Error::kByteLimit, "concurrent denial");
  }
  Check(admitted == 4, "atomic quota admission");
  Charge(ledger, 40, 4);
  gate.arrive_and_wait();
  for (auto& worker : workers) worker.join();
  Charge(ledger, 0, 0);
}

void ConcurrentAdapter() {
  struct ConcurrentBackend {
    std::atomic<int> calls{0};
    std::barrier<> started{5};
    std::barrier<> finish{5};
    int Allocate(Kind, size_t, void** p, uint64_t) noexcept {
      const int ordinal = calls.fetch_add(1);
      started.arrive_and_wait();
      finish.arrive_and_wait();
      *p = Pointer(ordinal + 1);
      return 0;
    }
    int Release(quota::Release, void*, uint64_t) noexcept { return 0; }
    int Synchronize(uint64_t) noexcept { return 0; }
  } backend;
  quota::Ledger<8> ledger(40);
  quota::Allocator<ConcurrentBackend, 8> api(ledger, backend);
  std::array<std::thread, 4> workers;
  std::array<void*, 4> pointers{};
  for (size_t i = 0; i < workers.size(); ++i)
    workers[i] = std::thread([&, i] {
      Check(api.Allocate(Kind::kDevice, 10, &pointers[i]).ok(), "held backend");
    });
  backend.started.arrive_and_wait();
  void* denied = nullptr;
  Check(api.Allocate(Kind::kDevice, 1, &denied).error == Error::kByteLimit,
        "in-flight calls reserve quota");
  Charge(ledger, 40, 4);
  Check(ledger.Snapshot().reserved_bytes == 40 && backend.calls == 4,
        "no fifth backend invocation");
  backend.finish.arrive_and_wait();
  for (auto& worker : workers) worker.join();
  for (void* pointer : pointers)
    Check(api.Free(pointer, Release::kDevice).ok(), "concurrent cleanup");
  Charge(ledger, 0, 0);
}

void NoMetadataAllocation() {
  Fixture fixture;
  new_calls = 0;
  count_new = true;
  void* p = nullptr;
  const auto allocation = fixture.api.Allocate(Kind::kAsyncDevice, 10, &p, 1);
  const auto release = fixture.api.Free(p, Release::kAsync, 1);
  const auto completion = fixture.api.Synchronize(1);
  const auto snapshot = fixture.ledger.Snapshot();
  count_new = false;
  Check(allocation.ok() && release.ok() && completion.ok() &&
            snapshot.charged_bytes == 0 && new_calls == 0,
        "no C++ heap metadata on active paths");
}

void StartupOwner() {
  namespace reserve = q4t::runtime::reservation;
  Fixture fixture(20);
  struct StartupBackend {
    Fixture<>& fixture;
    int Allocate(size_t, size_t bytes, void** p) noexcept {
      return fixture.api.Allocate(Kind::kDevice, bytes, p).ok() ? 0 : 2;
    }
    int Release(size_t, size_t, void* p, reserve::ReleaseReason) noexcept {
      return fixture.api.Free(p, quota::Release::kDevice).ok() ? 0 : 1;
    }
  } backend{fixture};
  {
    const std::array<size_t, 3> plan{8, 8, 8};
    reserve::Owner<StartupBackend> owner(plan, backend);
    Check(!owner.Prepare().ok(), "plan rejected by quota");
    owner.Finalize();
    Charge(fixture.ledger, 0, 0);
    Check(fixture.backend.allocations == 2 && fixture.backend.frees == 2,
          "only successful reservations rolled back");
  }
  {
    const std::array<size_t, 3> plan{8, 8, 4};
    reserve::Owner<StartupBackend> owner(plan, backend);
    Check(owner.Prepare().ok(), "exact plan accepted");
    std::array<void*, 3> pointers{};
    for (size_t i = 0; i < pointers.size(); ++i)
      Check(owner.Take(i == 2 ? 4 : 8, &pointers[i]).ok(), "handoff");
    Check(owner.Complete().ok(), "all handed");
    Charge(fixture.ledger, 20, 3);
    for (void* pointer : pointers)
      Check(owner.Release(pointer).ok(), "release");
    owner.Finalize();
    Charge(fixture.ledger, 0, 0);
  }
}

struct Test {
  const char* name;
  void (*run)();
};
constexpr Test tests[]{
    {"mixed_limit", MixedLimit},
    {"byte_overflow", ByteOverflow},
    {"entry_limit", EntryLimit},
    {"invalid_arguments", InvalidArguments},
    {"allocation_failure", AllocationFailure},
    {"allocation_unknown", AllocationUnknown},
    {"null_success", NullSuccess},
    {"duplicate_live", DuplicateLive},
    {"ticket_generation", TicketGeneration},
    {"release_claim", ReleaseClaim},
    {"release_failure", ReleaseFailure},
    {"release_types", ReleaseTypes},
    {"foreign_pointer", ForeignPointer},
    {"async_remains_charged", AsyncRemainsCharged},
    {"cross_queue", CrossQueue},
    {"later_submission", LaterSubmission},
    {"inflight_submission", InflightSubmission},
    {"queue_generation", QueueGeneration},
    {"address_reuse", AddressReuse},
    {"completion_failure", CompletionFailure},
    {"release_publication_race", ReleasePublicationRace},
    {"foreign_fence", ForeignFence},
    {"concurrent_reservations", ConcurrentReservations},
    {"concurrent_adapter", ConcurrentAdapter},
    {"no_metadata_allocation", NoMetadataAllocation},
    {"startup_owner", StartupOwner},
};
}  // namespace

void* operator new(std::size_t size) {
  if (count_new) new_calls.fetch_add(1);
  if (void* p = std::malloc(size ? size : 1)) return p;
  throw std::bad_alloc();
}
void* operator new[](std::size_t size) { return ::operator new(size); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

int main(int argc, char** argv) {
  if (argc > 2) return 2;
  size_t count = 0;
  for (const auto& test : tests) {
    if (argc == 2 && std::strcmp(argv[1], test.name)) continue;
    test.run();
    std::printf("PASS %s\n", test.name);
    ++count;
  }
  if (!count) return 2;
  std::printf("{\"passed\":true,\"tests\":%zu,\"cuda_linked\":false}\n", count);
  std::printf("{\"production_capacity\":16384,\"ledger_size_bytes\":%zu}\n",
              sizeof(quota::Ledger<>));
}
