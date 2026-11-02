#pragma once

#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/output_constraints.h"
#include "q4t/server/server_options.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kPreflightProfile = "q4t-preflight-v1";
inline constexpr size_t kPreflightResponseBytes = 8 * 1024 * 1024;

// A snapshot of initialized runtime values; never used to configure a model.
struct ChatPlanConfig {
  std::string model;
  int max_len = 8192;
  int max_seq = 1;
  int max_prefill = 2048;
  int default_max_tokens = 256;
  int request_deadline_ms = 1200000;
  AdmissionLimits admission;
  bool allow_media = false;
  bool structured = false;
  bool session_cache = false;
  bool prefix_cache = false;
  std::vector<int> capacities;
  std::vector<int> checkpoints;
};

struct ChatPlanError {
  std::string code = "invalid_request";
  std::string param;
};

struct ChatRequestPlan {
  OutputConstraints constraints;
  std::vector<std::string> stops;
};

Status PlanChatRequest(const io::Json& request, const ChatPlanConfig& config,
                       ChatRequestPlan* plan, ChatPlanError* error);

struct ChatPromptBudget {
  int requested_output = 0;
  int capped_output = 0;
  int available_output = 0;
  size_t required_capacity = 0;
  std::string output_source;
  std::vector<int> eligible_slots;
};

// Preserves both existing admission policies: uniform slots truncate output
// at the context boundary; explicit slots must fit the entire capped budget.
int ChatOutputLimit(const io::Json& request, const ChatPlanConfig& config);
Status PlanChatBudget(const io::Json& request, const ChatPlanConfig& config,
                      size_t tokens, ChatPromptBudget* budget,
                      ChatPlanError* error);

using PromptEncoder =
    std::function<Status(std::string_view, std::vector<uint32_t>*)>;
Status EncodeChatPrompt(const io::Json& request, const ChatPlanConfig& config,
                        std::string_view prompt, const PromptEncoder& encode,
                        std::vector<uint32_t>* tokens, ChatPromptBudget* budget,
                        ChatPlanError* error);

Status ValidatePreflightEnvelope(const io::Json& body, ChatPlanError* error);
Status ChatPreflightJson(const io::Json& envelope, const ChatPlanConfig& config,
                         const ChatRequestPlan& plan,
                         const ChatPromptBudget& budget,
                         const std::string& prompt,
                         const std::vector<uint32_t>& tokens,
                         std::string* output, ChatPlanError* error);

std::string EffectiveServeConfigJson(const ServerOptions& requested,
                                     const ChatPlanConfig& effective,
                                     bool budget_enabled, bool mtp_enabled,
                                     int business_connections);

}  // namespace q4t::server
