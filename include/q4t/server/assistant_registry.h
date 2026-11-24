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
  kMessageQuery,
  kRunCreate, kThreadRunCreate, kRunSubmit, kRunUpdate, kRunListQuery,
  kRunResource, kStepResource, kStepListQuery, kStepQuery
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
class ResponsesJobs;
void ServeAssistantRun(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                       std::string_view method, std::string_view path,
                       const std::string& body, const StateAccess& access,
                       const ChatPlanConfig& config, bool ingestion);
}  // namespace q4t::server
