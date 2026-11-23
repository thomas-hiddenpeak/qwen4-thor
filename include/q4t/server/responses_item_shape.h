#pragma once

#include <string_view>

#include "q4t/server/chat_plan.h"

namespace q4t::server {
enum class ResponsesItemShape { kInput, kOutput, kConversation };
enum class MultiAgentShape {
  kConfiguration, kInputCall, kInputResult, kOutputCall, kOutputResult
};
Status ValidateMultiAgentShape(const io::Json& value, MultiAgentShape shape,
                               ChatPlanError* error, std::string_view path);

// SDK data shapes, including open maps and partial tool history. This performs
// no model/tool/media execution, and does not replace endpoint limits, resource
// authorization, or the generation planner's call/result consistency checks.
Status ValidateResponsesItemShape(const io::Json& item,
                                  ResponsesItemShape shape,
                                  ChatPlanError* error,
                                  std::string_view path = "input",
                                  bool multi_agent = false);
}  // namespace q4t::server
