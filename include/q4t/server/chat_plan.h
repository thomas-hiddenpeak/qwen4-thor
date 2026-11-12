#pragma once

#include <functional>
#include <memory>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/access_program.h"
#include "q4t/server/output_constraints.h"
#include "q4t/server/sampling.h"
#include "q4t/server/prediction.h"
#include "q4t/server/prompt_cache_policy.h"
#include "q4t/server/client_policy.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/server_options.h"
#include "q4t/status.h"

namespace q4t::vision {
struct MediaPlan;
}

namespace q4t::server {
struct FileExpansionOptions;
struct FileExpansionUsage;

inline constexpr std::string_view kPreflightProfile = "q4t-preflight-v1";
inline constexpr size_t kPreflightResponseBytes = 8 * 1024 * 1024;

// A snapshot of initialized runtime values; never used to configure a model.
struct ChatPlanConfig {
  std::string model;
  int max_len = 8192;
  int max_seq = 1;
  int max_prefill = 2048;
  int default_max_tokens = 256;
  int token_vocab = 0;
  int embedding_dimensions = 0;
  int embedding_min_dimensions = 1;
  // Independently configured trained backend. Never adds a Chat model alias.
  bool native_embedding_enabled = false;
  int request_deadline_ms = 1200000;
  AdmissionLimits admission;
  bool allow_media = false;
  uint64_t media_attention_shared_bytes = 0;
  bool structured = false;
  bool session_cache = false;
  bool prefix_cache = false;
  // Internal Responses prefill-only execution, never a caller Chat field.
  bool prewarm_only = false;
  // Internal CPU input counting: no generation slot/output reservation.
  // Never set from an untrusted Chat field.
  bool count_input_only = false;
  // Pure validation before request-owned file acquisition. The renderer must
  // receive the expanded request, with this flag cleared.
  bool preparing_file_inputs = false;
  // Validated Responses history only. Public Chat cannot set this policy.
  std::vector<std::string> async_tool_call_ids = {};
  bool responses_tool_media = false;
  // Responses discovery keeps later definitions at their historical position.
  // This affects rendering only; the actual tools still constrain generation.
  std::optional<io::Json> header_tools;
  std::vector<ChatToolCallFormat> tool_call_formats;
  std::vector<int> capacities;
  std::vector<int> checkpoints;
  uint64_t cuda_quota_limit_bytes = 0;
  std::string client_policy_sha256;
  std::string client_policy_content_sha256;
  uint64_t client_policy_revision = 0;
  size_t client_count = 0;
  uint32_t client_policy_version = 1;
  bool client_fair_queue = false;
  const ClientLimits* client_limits = nullptr;
  std::vector<std::string> model_aliases;
  std::shared_ptr<const AccessProgramPolicy> access_program_policy;
  // Deployment-owned file policy; never read from request JSON.
  std::shared_ptr<const FileExpansionOptions> file_inputs;
  std::string file_input_policy_sha256;
};

struct ChatPlanError {
  std::string code = "invalid_request";
  std::string param;
  int http_status = 400;
};

struct ChatRequestPlan {
  std::vector<std::string> async_tool_call_ids = {};
  bool responses_tool_media = false;
  std::optional<io::Json> header_tools;
  std::vector<ChatToolCallFormat> tool_call_formats;
  std::optional<io::Json> normalized_request;
  bool legacy_output = false;
  OutputConstraints constraints;
  SamplingOptions sampling;
  PredictionOptions prediction;
  PromptCacheOptions cache;
  std::vector<std::string> stops;
  const io::Json& Request(const io::Json& original) const {
    return normalized_request ? *normalized_request : original;
  }
  const io::Json* HeaderTools() const {
    return header_tools ? &*header_tools : nullptr;
  }
};

Status PlanChatRequest(const io::Json& request, const ChatPlanConfig& config,
                       ChatRequestPlan* plan, ChatPlanError* error);

struct ChatPromptBudget {
  int requested_output = 0;
  int capped_output = 0;
  int available_output = 0;
  uint64_t total_output = 0;
  std::vector<int32_t> prediction_tokens;
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
                         std::string* output, ChatPlanError* error,
                         const vision::MediaPlan* media = nullptr,
                         const FileExpansionUsage* files = nullptr);

struct StartupMemoryChecks;
std::string EffectiveServeConfigJson(
    const ServerOptions& requested, const ChatPlanConfig& effective,
    bool budget_enabled, bool mtp_enabled, int business_connections,
    const StartupMemoryChecks* startup = nullptr);

}  // namespace q4t::server
