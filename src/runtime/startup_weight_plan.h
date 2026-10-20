// Shared metadata-only weight descriptions for main and MTP planning.
#pragma once

#include "q4t/model/full_attention.h"
#include "q4t/runtime/startup_plan.h"

namespace q4t::runtime::startup_detail {
Status PlanHC(const io::WeightLoader& loader, const std::string& prefix, int hc,
              int hs, int lowrank, bool combine, StartupPlan* plan);
Status PlanFull(const io::WeightLoader& loader, const std::string& prefix,
                const model::FullAttentionWeights& dims, StartupPlan* plan);
Status PlanMoEExtra(const io::WeightLoader& loader, const std::string& prefix,
                    int experts, int hs, int shared_is, StartupPlan* plan);
}  // namespace q4t::runtime::startup_detail
