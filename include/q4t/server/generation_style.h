// Shared model-native adaptation of standard generation style controls.
#pragma once

#include <string_view>

namespace q4t::server {

inline bool IsReasoningEffort(std::string_view effort) {
  return effort == "none" || effort == "minimal" || effort == "low" ||
         effort == "medium" || effort == "high" || effort == "xhigh" ||
         effort == "max";
}

// These are model instructions, not a numeric compute or quality guarantee.
// none additionally selects the model's native closed-thinking prefix.
inline std::string_view ReasoningGuidance(std::string_view effort) {
  if (effort == "low")
    return "Reasoning effort is set to low. Keep your thinking brief "
           "and focused, moving directly to the conclusion without unnecessary "
           "elaboration.";
  if (effort == "xhigh")
    return "Reasoning effort is set to xhigh. Please think carefully "
           "through the task, validate key assumptions, consider plausible "
           "alternatives, and prioritize correctness, consistency, and clarity "
           "in the final answer.";
  if (effort == "minimal")
    return "Use minimal reasoning: only the essential steps needed for an "
           "accurate answer.";
  if (effort == "high")
    return "Use high reasoning effort: check assumptions and verify the "
           "important steps before answering.";
  if (effort == "max")
    return "Use maximum reasoning effort within the output budget: examine "
           "alternatives and verify the result carefully.";
  return {};
}

inline bool IsVerbosity(std::string_view verbosity) {
  return verbosity == "low" || verbosity == "medium" || verbosity == "high";
}

inline std::string_view VerbosityGuidance(std::string_view verbosity) {
  if (verbosity == "low")
    return "Keep the final answer concise and directly relevant.";
  if (verbosity == "high")
    return "Give a detailed final answer with the relevant explanation and "
           "evidence.";
  return {};
}

}  // namespace q4t::server
