// Request-owned file/media input for a Realtime generation.
#pragma once

#include "q4t/server/file_expansion.h"
#include "q4t/server/realtime_chat_plan.h"

namespace q4t::server {
class RealtimeFileInputs {
 public:
  // Whole-envelope validation has already happened in PlanRealtimeChat.
  // The ordinary authenticated owner supplies access, never the Prompt ID.
  ObjectReply Prepare(const RealtimeChatPlan& plan,
                      const ChatPlanConfig& config, ObjectStore* objects,
                      BinaryAccess access);
  FileExpansion* Files() { return files_.get(); }
  const FileExpansionUsage* Usage() const {
    return files_ ? &files_->Usage() : nullptr;
  }
  // Pending sheet steps must be executed and accounted by the model owner.
  // No fabricated summary or partial file may become an executable plan.
  ObjectReply Finish(const ChatPlanConfig& config, RealtimeChatPlan* plan);

 private:
  std::unique_ptr<FileExpansion> files_;
  io::Json private_items_;
  BinaryAccess access_;
  std::string request_sha256_;
  size_t prompt_items_ = 0;
  bool attempted_ = false, completed_ = false;
};
}  // namespace q4t::server
