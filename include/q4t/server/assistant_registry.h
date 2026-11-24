#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
enum class AssistantShape {
  kCreate,
  kUpdate,
  kResource,
  kQuery,
  kThreadCreate,
  kThreadUpdate,
  kThreadResource,
  kMessageCreate,
  kMessageUpdate,
  kMessageResource,
  kMessageQuery
};
Status ValidateAssistant(const io::Json& value, AssistantShape shape,
                         ChatPlanError* error);
ObjectReply HandleAssistantRegistry(
    ObjectStore& objects, std::string_view method, std::string_view target,
    const std::string& body, const ChatPlanConfig& config,
    const StateAccess& access, bool ingestion_configured);
ObjectReply HandleAssistantThreads(ObjectStore& objects,
                                   std::string_view method,
                                   std::string_view target,
                                   const std::string& body,
                                   const StateAccess& access,
                                   bool ingestion_configured);
}  // namespace q4t::server
