// Standalone standard moderation input/result contract, without model I/O.
#pragma once

#include <vector>

#include "q4t/server/moderation.h"

namespace q4t::server {
inline constexpr size_t kModerationsInputs = 32;
inline constexpr size_t kModerationsParts = 256;
inline constexpr size_t kModerationsTextBytes = 1024 * 1024;

struct ModerationsRequestPlan {
  std::string model;
  std::vector<ModerationPlan> inputs;
  uint64_t OutputBudget() const {
    return inputs.size() * kMaxModerationStepTokens;
  }
};

// Each string in a string array has its own result. A flat multimodal array
// is one content group, classified together. Failure publishes no partial plan.
Status PlanModerationsRequest(const io::Json& request,
                              const ChatPlanConfig& config,
                              ModerationsRequestPlan* plan,
                              ChatPlanError* error);
// Require one complete real report per input and the actual served identity.
// Internal report metadata is omitted from the standard public result shape.
Status BuildModerationsResponse(std::string_view id,
                                const ModerationsRequestPlan& plan,
                                const std::vector<io::Json>& reports,
                                io::Json* response);
}  // namespace q4t::server
