#pragma once

#include "q4t/server/responses_web_search.h"

namespace q4t::server {
// Pre-answer retrieval for Chat. The original public request is unchanged;
// only the private model request carries the actual source passages. Queued
// work retains those passages, bound to its original request and scope.
class ChatWebSearch {
 public:
  ObjectReply Prepare(ObjectStore* objects, const io::Json& request,
                      const ChatPlanConfig& config, const BinaryAccess& access,
                      ChatRequestPlan* plan, const io::Json* frozen = nullptr);
  const io::Json& Freeze() const { return frozen_; }
  bool Active() const { return !frozen_.IsNull(); }
  Status Annotate(std::string_view message, std::string* output,
                  std::string* delta) const;

 private:
  io::Json frozen_, context_;
  bool attempted_ = false;
};
}  // namespace q4t::server
