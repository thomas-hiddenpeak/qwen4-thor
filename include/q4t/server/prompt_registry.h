#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {

// Local immutable prompt versions. HTTP credentials supply StateAccess; a
// prompt ID/variable/metadata value is never an authority or filesystem path.
class PromptRegistry {
 public:
  explicit PromptRegistry(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Create(const io::Json& definition, const ChatPlanConfig& config,
                     const StateAccess& access);
  ObjectReply Append(const std::string& id, int64_t expected_revision,
                     const io::Json& definition, const ChatPlanConfig& config,
                     const StateAccess& access);
  ObjectReply Select(const std::string& id, int64_t expected_revision,
                     const std::string& version, const StateAccess& access);
  ObjectReply Retrieve(const std::string& id, const std::string& version,
                       const StateAccess& access);
  ObjectReply Erase(const std::string& id, const StateAccess& access);
  ObjectReply List(ObjectRequest request, const StateAccess& access);
  // Returns an effective Responses request with a pinned public prompt
  // reference. This executes no model/tool. Restore uses the saved effective
  // request, never expands it again or depends on the live registry.
  ObjectReply Expand(const io::Json& request, const ChatPlanConfig& config,
                     const StateAccess& access);

 private:
  ObjectReply Read(const std::string& id, const StateAccess& access);
  ObjectReply Write(const std::string& id, int64_t expected_revision,
                    io::Json data, const StateAccess& access);
  ObjectStore& objects_;
};

// q4t-native management extension; Responses.prompt keeps the SDK shape.
ObjectReply HandlePromptRequest(ObjectStore& objects, std::string_view method,
                                std::string_view target,
                                const std::string& body,
                                const ChatPlanConfig& config,
                                const StateAccess& access);

}  // namespace q4t::server
