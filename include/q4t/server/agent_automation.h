#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
class ResponsesJobs;
enum class AutomationShape {
  kCreate,
  kUpdate,
  kResource,
  kDeleted,
  kQuery,
  kInvocation,
  kInvocationQuery
};
Status ValidateAutomation(const io::Json& value, AutomationShape shape,
                          ChatPlanError* error);
ObjectReply HandleAgentAutomation(ObjectStore& objects, ResponsesJobs* jobs,
                                  const ChatPlanConfig& config,
                                  const StateAccess& access,
                                  std::string_view method,
                                  std::string_view target,
                                  const std::string& body);
}  // namespace q4t::server
