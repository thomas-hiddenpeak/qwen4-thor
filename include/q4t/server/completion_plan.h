#pragma once

#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "q4t/server/chat_plan.h"

namespace q4t::server {

inline constexpr size_t kCompletionCandidates = 128;
inline constexpr io::JsonParseLimits kCompletionJsonLimits{
    .max_depth = 32,
    .max_values = 1024 * 1024,
    .max_string_bytes = 16 * 1024 * 1024,
    .reject_duplicate_keys = true,
    .allow_exact_number_text = true};

struct CompletionPrompt {
  bool tokenized = false;
  std::string text;
  std::vector<uint32_t> input_tokens;
};

struct CompletionRequestPlan {
  // Common generator controls, with actual sampling/choice semantics; never
  // reparsed as a Chat request (zero budgets and raw token inputs are legal).
  io::Json generation_request;
  SamplingOptions sampling;
  std::vector<std::string> stops;
  std::vector<CompletionPrompt> prompts;
  std::optional<std::string> suffix;
  std::string response_model;
  int max_tokens = 16;
  int n = 1;
  int best_of = 1;
  int logprobs = -1;
  bool echo = false;
  bool stream = false;
  bool include_usage = false;
  bool include_obfuscation = true;
  bool ScoresPrompt() const { return echo && logprobs >= 0; }
  int CandidateCount() const {
    return static_cast<int>(prompts.size()) * best_of;
  }
};

Status PlanCompletionRequest(const io::Json& request,
                             const ChatPlanConfig& config,
                             CompletionRequestPlan* plan,
                             ChatPlanError* error);

struct PreparedCompletionPrompt {
  std::vector<uint32_t> tokens;
  std::string echo_text;
  // Only this interval is echoed/scored, not FIM markers/suffix or an
  // implicit empty-document seed. Positions refer to the actual model input.
  size_t echo_begin = 0;
  size_t echo_end = 0;
  std::vector<size_t> echo_offsets;
  ChatPromptBudget budget;
};

using CompletionDecoder =
    std::function<Status(const std::vector<uint32_t>&, std::string*)>;
Status PrepareCompletionPrompts(
    const CompletionRequestPlan& plan, const ChatPlanConfig& config,
    const PromptEncoder& encode, const CompletionDecoder& decode,
    std::vector<PreparedCompletionPrompt>* prompts, uint64_t* prompt_tokens,
    uint64_t* output_tokens, ChatPlanError* error);

}  // namespace q4t::server
