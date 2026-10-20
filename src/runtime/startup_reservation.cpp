#include "q4t/runtime/startup_reservation.h"

#include <cuda_runtime_api.h>

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <exception>
#include <string>
#include <vector>

#include "q4t/runtime/startup_plan.h"
#include "q4t/trace/sha256.h"

extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __real_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __real_cudaMallocAsync(void**, size_t, cudaStream_t);
extern "C" cudaError_t __real_cudaFreeAsync(void*, cudaStream_t);

namespace {
namespace reserve = q4t::runtime::reservation;
using Summary = q4t::runtime::StartupReservationSummary;

struct Backend {
  int Allocate(size_t, size_t bytes, void** pointer) noexcept {
    return static_cast<int>(__real_cudaMalloc(pointer, bytes));
  }
  int Release(size_t, size_t, void* pointer, reserve::ReleaseReason) noexcept {
    return static_cast<int>(__real_cudaFree(pointer));
  }
};
using Owner = reserve::Owner<Backend>;

// A process-lived, allocation-free lock avoids throwing during destructor
// cleanup. CUDA calls and scope transitions share this gate, so a wrapper
// cannot retain a dangling owner while another thread finalizes its scope.
struct Registry {
  std::atomic_flag busy = ATOMIC_FLAG_INIT;
  Owner* active = nullptr;
};
constinit Registry registry;

class Guard {
 public:
  Guard() noexcept {
    while (registry.busy.test_and_set(std::memory_order_acquire)) {
      registry.busy.wait(true, std::memory_order_relaxed);
    }
  }
  ~Guard() noexcept {
    registry.busy.clear(std::memory_order_release);
    registry.busy.notify_one();
  }
  Guard(const Guard&) = delete;
  Guard& operator=(const Guard&) = delete;
};

cudaError_t CudaResult(reserve::Result result) noexcept {
  if (result.ok()) return cudaSuccess;
  if (result.backend_rc != 0) return cudaError_t(result.backend_rc);
  return cudaErrorInvalidValue;
}

q4t::Status StatusResult(reserve::Result result) {
  if (result.ok()) return {};
  return q4t::Status::Fail(std::string("startup reservation: ") +
                           reserve::ErrorName(result.error) +
                           " index=" + std::to_string(result.index) +
                           " backend_rc=" + std::to_string(result.backend_rc));
}

// Observability is best effort. A stderr failure must never abort allocation
// or destructor cleanup. No per-allocation trace, output files, or exit hook.
void Report(const char* event, const Summary& summary) noexcept {
  const auto& state = summary.ownership;
  std::fprintf(stderr,
               "[startup-reservation] {\"schema\":1,\"event\":\"%s\","
               "\"status\":\"%s\",\"count\":%zu,\"device_bytes\":%zu,"
               "\"allocated\":%zu,\"consumed\":%zu,\"freed\":%zu,"
               "\"unhanded_remaining\":%zu,\"handed_live\":%zu,"
               "\"release_unknown\":%zu,\"error\":\"%s\","
               "\"backend_rc\":%d,\"error_index\":%zu,\"prepared\":%s,"
               "\"startup_complete\":%s,\"finalized\":%s,\"active\":%s,"
               "\"strategy\":\"stable_descending_bytes\","
               "\"plan_sha256\":\"%s\"}\n",
               event, state.result.ok() ? "PASS" : "FAILED", state.count,
               summary.device_bytes, state.allocated, state.consumed,
               state.freed, state.unhanded_remaining, state.handed_live,
               state.release_unknown, reserve::ErrorName(state.result.error),
               state.result.backend_rc, state.result.index,
               state.prepared ? "true" : "false",
               summary.startup_complete ? "true" : "false",
               state.finalized ? "true" : "false",
               summary.active ? "true" : "false", summary.plan_sha256.data());
}

std::vector<size_t> Sizes(const q4t::runtime::StartupPlan& plan) {
  std::vector<size_t> sizes;
  sizes.reserve(plan.entries().size());
  for (const auto& entry : plan.entries()) sizes.push_back(entry.bytes);
  return sizes;
}

std::array<char, 65> PlanDigest(const q4t::runtime::StartupPlan& plan) {
  // Versioned, length-prefixed canonical metadata; the strategy fixes the
  // physical permutation deterministically, including equal-size ties.
  std::string data = "q4t-startup-plan-v1\nstable_descending_bytes\n";
  for (const auto& entry : plan.entries()) {
    data += std::to_string(entry.name.size()) + ":" + entry.name + ":" +
            std::to_string(entry.bytes) + "\n";
  }
  const auto digest = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(data.data()), data.size()}));
  std::array<char, 65> result{};
  std::copy(digest.begin(), digest.end(), result.begin());
  return result;
}
}  // namespace

namespace q4t::runtime {
struct StartupReservation::Impl {
  explicit Impl(const StartupPlan& plan)
      : owner(Sizes(plan), backend),
        bytes(plan.device_bytes()),
        digest(PlanDigest(plan)) {}

  StartupReservationSummary Snapshot() const noexcept {
    return {owner.Snapshot(), bytes, digest, registry.active == &owner,
            owner.startup_complete()};
  }

  Backend backend;
  Owner owner;
  size_t bytes;
  std::array<char, 65> digest;
};

StartupReservation::StartupReservation() noexcept = default;
StartupReservation::~StartupReservation() noexcept { Finalize(); }

Status StartupReservation::Prepare(const model::ModelConfig& cfg,
                                   const server::ServerOptions& opts) {
  if (attempted_ || finalized_) {
    return Status::Fail("startup reservation: prepare called twice");
  }
  try {
    StartupPlan plan;
    const Status status = BuildStartupPlan(cfg, opts, &plan);
    if (status.ok()) return Prepare(plan);
    attempted_ = true;
    summary_.ownership.result.error = reservation::Error::kInvalidPlan;
    Report("prepare_failed", summary_);
    return status;
  } catch (const std::exception&) {
    attempted_ = true;
    summary_.ownership.result.error = reservation::Error::kInvalidPlan;
    Report("prepare_failed", summary_);
    return Status::Fail("startup reservation: metadata construction failed");
  }
}

Status StartupReservation::Prepare(const StartupPlan& plan) {
  const Guard guard;
  if (attempted_ || finalized_) {
    if (impl_) impl_->owner.Reject(reservation::Error::kWrongPhase);
    return Status::Fail("startup reservation: prepare called twice");
  }
  attempted_ = true;
  if (registry.active != nullptr) {
    summary_.ownership.result.error = reservation::Error::kWrongPhase;
    Report("prepare_failed", summary_);
    return Status::Fail("startup reservation: another scope is active");
  }
  try {
    impl_ = std::make_unique<Impl>(plan);
  } catch (const std::exception&) {
    summary_.ownership.result.error = reservation::Error::kInvalidPlan;
    Report("prepare_failed", summary_);
    return Status::Fail("startup reservation: metadata construction failed");
  }
  registry.active = &impl_->owner;
  const auto result = impl_->owner.Prepare();
  summary_ = impl_->Snapshot();
  Report(result.ok() ? "prepared" : "prepare_failed", summary_);
  return StatusResult(result);
}

Status StartupReservation::Complete() {
  const Guard guard;
  if (!impl_ || finalized_ || registry.active != &impl_->owner) {
    return Status::Fail("startup reservation: no active prepared scope");
  }
  const auto result = impl_->owner.Complete();
  summary_ = impl_->Snapshot();
  Report(result.ok() ? "startup_complete" : "complete_failed", summary_);
  return StatusResult(result);
}

bool StartupReservation::active() const noexcept { return Snapshot().active; }
bool StartupReservation::prepared() const noexcept {
  return Snapshot().ownership.prepared;
}

StartupReservationSummary StartupReservation::Snapshot() const noexcept {
  const Guard guard;
  return impl_ && !finalized_ ? impl_->Snapshot() : summary_;
}

StartupReservationSummary StartupReservation::Finalize() noexcept {
  const Guard guard;
  if (finalized_) return summary_;
  finalized_ = true;
  if (impl_) {
    impl_->owner.Finalize();
    if (registry.active == &impl_->owner) registry.active = nullptr;
    summary_ = impl_->Snapshot();
  } else {
    summary_.ownership.finalized = true;
  }
  if (attempted_) Report("finalized", summary_);
  return summary_;
}
}  // namespace q4t::runtime

extern "C" cudaError_t __wrap_cudaMalloc(void** pointer, size_t bytes) {
  const Guard guard;
  Owner* owner = registry.active;
  if (!owner) return __real_cudaMalloc(pointer, bytes);
  if (!owner->startup_complete())
    return CudaResult(owner->Take(bytes, pointer));
  const auto allowed = owner->RequireRuntime();
  if (!allowed.ok()) {
    if (pointer) *pointer = nullptr;
    return CudaResult(allowed);
  }
  const cudaError_t rc = __real_cudaMalloc(pointer, bytes);
  if (rc != cudaSuccess) return rc;
  return CudaResult(
      owner->ObserveExternalAllocation(pointer ? *pointer : nullptr));
}

extern "C" cudaError_t __wrap_cudaFree(void* pointer) {
  const Guard guard;
  if (!registry.active) return __real_cudaFree(pointer);
  const auto result = registry.active->Release(pointer);
  return result.error == reserve::Error::kNotOwned ? __real_cudaFree(pointer)
                                                   : CudaResult(result);
}

extern "C" cudaError_t __wrap_cudaHostAlloc(void** pointer, size_t bytes,
                                            unsigned int flags) {
  const Guard guard;
  if (registry.active) {
    const auto allowed = registry.active->RequirePrepared();
    if (!allowed.ok()) {
      if (pointer) *pointer = nullptr;
      return CudaResult(allowed);
    }
  }
  return __real_cudaHostAlloc(pointer, bytes, flags);
}

extern "C" cudaError_t __wrap_cudaMallocHost(void** pointer, size_t bytes) {
  const Guard guard;
  if (registry.active) {
    const auto allowed = registry.active->RequirePrepared();
    if (!allowed.ok()) {
      if (pointer) *pointer = nullptr;
      return CudaResult(allowed);
    }
  }
  return __real_cudaMallocHost(pointer, bytes);
}

extern "C" cudaError_t __wrap_cudaMallocAsync(void** pointer, size_t bytes,
                                              cudaStream_t stream) {
  const Guard guard;
  Owner* owner = registry.active;
  if (!owner) return __real_cudaMallocAsync(pointer, bytes, stream);
  const auto allowed = owner->RequireRuntime();
  if (!allowed.ok()) {
    if (pointer) *pointer = nullptr;
    return CudaResult(allowed);
  }
  const cudaError_t rc = __real_cudaMallocAsync(pointer, bytes, stream);
  if (rc != cudaSuccess) return rc;
  return CudaResult(
      owner->ObserveExternalAllocation(pointer ? *pointer : nullptr));
}

extern "C" cudaError_t __wrap_cudaFreeAsync(void* pointer,
                                            cudaStream_t stream) {
  const Guard guard;
  if (registry.active && registry.active->Contains(pointer)) {
    return CudaResult(registry.active->Reject(reserve::Error::kWrongPhase));
  }
  return __real_cudaFreeAsync(pointer, stream);
}
