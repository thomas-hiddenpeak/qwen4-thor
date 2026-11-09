#pragma once

#include <array>
#include <cstdint>
#include <string_view>

#include "q4t/server/chat_plan.h"

namespace q4t::server {

enum class ModerationMode { kScore, kBlock };
enum class ModerationDecision { kAllow, kBlocked, kUnavailable };

struct ModerationUsage {
  int64_t input_tokens = 0;
  int64_t output_tokens = 0;
  int64_t model_steps = 0;
};

struct ModerationOptions {
  bool enabled = false;
  std::string model;
  ModerationMode input = ModerationMode::kScore;
  ModerationMode output = ModerationMode::kScore;
};

struct ModerationContent {
  io::Json records;
  io::Json images;
  bool text = false;
};

struct ModerationPlan {
  io::Json request;
  ChatRequestPlan chat;
  bool text = false;
  bool image = false;
  std::string model;
};

inline constexpr std::array<std::string_view, 13> kModerationCategories{
    "harassment",
    "harassment/threatening",
    "hate",
    "hate/threatening",
    "illicit",
    "illicit/violent",
    "self-harm",
    "self-harm/instructions",
    "self-harm/intent",
    "sexual",
    "sexual/minors",
    "violence",
    "violence/graphic"};
inline constexpr int kMaxModerationStepTokens = 512;

// Select the actual served checkpoint. Cloud model names are never aliases for
// an unrelated local classifier. No model execution occurs in these planners.
Status ParseModerationOptions(const io::Json& request,
                              const ChatPlanConfig& config,
                              ModerationOptions* options, ChatPlanError* error);
// These consume already validated actual message/output items. Tool argument
// and result content is included, tool names/definitions and schemas are not.
Status CollectModerationInput(const io::Json& chat_messages,
                              ModerationContent* content, ChatPlanError* error);
Status CollectModerationOutput(const io::Json& response_items,
                               ModerationContent* content,
                               ChatPlanError* error);
Status PlanModeration(const ModerationOptions& options,
                      const ModerationContent& content,
                      const ChatPlanConfig& config, int output_limit,
                      ModerationPlan* plan, ChatPlanError* error);
// Scores come from the actual classifier output. Missing/invalid values never
// become zero or a safe result. The local threshold is not cloud calibration.
Status ParseModerationResult(std::string_view generated,
                             const ModerationPlan& plan, double threshold,
                             io::Json* result);
io::Json ModerationError(std::string code, std::string message);
bool ValidModerationReport(const io::Json& result);
ModerationDecision DecideModeration(ModerationMode mode,
                                    const io::Json& result);

}  // namespace q4t::server
