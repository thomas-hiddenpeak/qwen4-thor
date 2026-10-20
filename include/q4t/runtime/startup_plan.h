// Metadata-only startup allocation contract. No CUDA calls or payload reads.
#pragma once

#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <string>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/status.h"

namespace q4t::model {
struct ModelConfig;
}
namespace q4t::mtp {
struct MtpConfig;
}
namespace q4t::server {
struct ServerOptions;
}

namespace q4t::runtime {
struct ResidencyConfig;

struct StartupAllocation {
  std::string name;
  size_t bytes = 0;
};

class StartupPlan {
 public:
  Status AddBytes(const std::string& name, size_t bytes);
  Status Add(const std::string& name, std::initializer_list<int64_t> shape,
             size_t element_bytes);
  Status CheckTensor(const io::WeightLoader& loader, const std::string& name,
                     io::Dtype dtype,
                     std::initializer_list<int64_t> shape) const;
  Status AddTensor(const io::WeightLoader& loader, const std::string& name,
                   io::Dtype dtype, std::initializer_list<int64_t> shape);
  const std::vector<StartupAllocation>& entries() const { return entries_; }
  size_t device_bytes() const { return device_bytes_; }

 private:
  std::vector<StartupAllocation> entries_;
  size_t device_bytes_ = 0;
};

Status PlanMainResources(const model::ModelConfig& cfg,
                         const ResidencyConfig& residency, StartupPlan* plan);
Status PlanMtpResources(const mtp::MtpConfig& cfg, StartupPlan* plan);
Status PlanVerifyResources(const model::ModelConfig& cfg, int k,
                           StartupPlan* plan);
// cfg is the effective, post-budget configuration, not the CLI request.
// Pure support selection: no file reads, device allocation or fallback after
// a checkpoint/plan error. Auto mode may use the legacy path only if this
// configuration is unsupported; BuildStartupPlan failures remain failures.
Status CheckStartupPlanSupport(const model::ModelConfig& cfg,
                               const server::ServerOptions& opts);
Status BuildStartupPlan(const model::ModelConfig& cfg,
                        const server::ServerOptions& opts, StartupPlan* plan);

// Implemented only by the explicitly enabled research executable.
Status PrepareStartupReservation(const model::ModelConfig& cfg,
                                 const server::ServerOptions& opts);
Status CompleteStartupReservation();
}  // namespace q4t::runtime
