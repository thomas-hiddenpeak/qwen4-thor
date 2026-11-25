#pragma once

#include <functional>

#include "q4t/server/responses_state.h"

namespace q4t::server {
struct AsrRequest;
struct ChatPlanConfig;
class ResponsesJobs;
struct ChatKitIssuer {
  std::string_view binding;
  std::string_view key_digest;
};
void ServeChatKitClient(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                        const ChatPlanConfig& config, const StateAccess& access,
                        std::string_view token, const std::string& body,
                        ChatKitIssuer issuer, std::string_view origin,
                        const std::function<void(const io::Json&)>& transcribe);
Status ParseChatKitTranscription(const io::Json& params, AsrRequest* request,
                                 ChatPlanError* error);
Status ValidateChatKit(const io::Json& value, const char* root,
                       ChatPlanError* error);
ObjectReply HandleChatKit(ObjectStore& objects, const StateAccess& access,
                          std::string_view method, std::string_view target,
                          const std::string& body, ChatKitIssuer issuer = {});
ObjectReply HandleChatKitSession(ObjectStore& objects,
                                 const StateAccess& access,
                                 std::string_view target,
                                 const std::string& body,
                                 ChatKitIssuer issuer = {});
}  // namespace q4t::server
