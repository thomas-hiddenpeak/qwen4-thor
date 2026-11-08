#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {

struct ChatStorageOptions {
  bool store = false;
  bool has_metadata = false;
  io::Json metadata;
};

Status ValidateChatMetadata(const io::Json& metadata,
                            std::string* error_param = nullptr);

Status ParseChatStorageOptions(const io::Json& request,
                               ChatStorageOptions* options,
                               std::string* error_param = nullptr);

struct ChatListOptions {
  int limit = 20;
  bool descending = false;
  std::string after;
  std::string model;
  io::Json metadata;
};

// Stores only actual complete generation results and the original input
// messages. It never reserves an incomplete completion, executes a model/tool,
// or replays a write after an uncertain receipt. An uncertain final put is
// reconciled by retrieving its original ID, not by generating again.
class StoredChats {
 public:
  explicit StoredChats(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Save(const io::Json& request, const io::Json& response,
                   const StateAccess& access);
  ObjectReply Retrieve(const std::string& id, const StateAccess& access);
  ObjectReply Update(const std::string& id, const io::Json& metadata,
                     const StateAccess& access);
  ObjectReply Erase(const std::string& id, const StateAccess& access);
  ObjectReply List(const ChatListOptions& options, const StateAccess& access);
  ObjectReply Messages(const std::string& id, const ChatListOptions& options,
                       const StateAccess& access);

 private:
  ObjectReply Read(const std::string& id, const StateAccess& access);
  ObjectStore& objects_;
};

// Metadata is returned even when store=false, without touching storage.
ObjectReply FinalizeStoredChat(ObjectStore* objects, const io::Json& request,
                               const std::string& response,
                               const StateAccess& access);
ObjectReply HandleStoredChatRequest(ObjectStore& objects,
                                    std::string_view method,
                                    std::string_view target,
                                    const std::string& body,
                                    const StateAccess& access);

}  // namespace q4t::server
