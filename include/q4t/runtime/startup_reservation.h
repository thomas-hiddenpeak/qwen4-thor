// Scoped production startup reservation. No CUDA dependency in this interface.
#pragma once

#include <array>
#include <memory>

#include "q4t/runtime/reservation_owner.h"
#include "q4t/status.h"

namespace q4t::model {
struct ModelConfig;
}
namespace q4t::server {
struct ServerOptions;
}

namespace q4t::runtime {
class StartupPlan;

struct StartupReservationSummary {
  reservation::Summary ownership;
  size_t device_bytes = 0;
  std::array<char, 65> plan_sha256{};
  bool active = false;
  bool startup_complete = false;
};

// Declare before all model/CUDA-owning members: their destructors must run
// before this scope finalizes. Only one prepared scope is allowed per process.
// An unused scope is inert. A selected plan failure must stop model startup;
// callers must not turn a Prepare/Complete error into a legacy-path retry.
class StartupReservation {
 public:
  StartupReservation() noexcept;
  ~StartupReservation() noexcept;
  StartupReservation(const StartupReservation&) = delete;
  StartupReservation& operator=(const StartupReservation&) = delete;

  Status Prepare(const model::ModelConfig& cfg,
                 const server::ServerOptions& opts);
  Status Prepare(const StartupPlan& plan);
  Status Complete();
  bool active() const noexcept;
  bool prepared() const noexcept;
  StartupReservationSummary Snapshot() const noexcept;
  // After application destructors; frees only unhanded allocations. Repeated
  // calls are inert. A failed CUDA free has unknown completion, never retried.
  StartupReservationSummary Finalize() noexcept;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  StartupReservationSummary summary_;
  bool attempted_ = false;
  bool finalized_ = false;
};
}  // namespace q4t::runtime
