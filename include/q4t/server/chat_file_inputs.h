#pragma once

#include "q4t/server/file_expansion.h"

namespace q4t::server {

// Owns the actual PDF pages/image bytes through planning and generation. Pure
// validation of the whole envelope always precedes acquisition. Public input
// is retained verbatim; only the private model request is expanded.
class ChatFileInputs {
 public:
  explicit ChatFileInputs(ObjectStore* objects) : objects_(objects) {}
  ObjectReply Prepare(const io::Json& request, const ChatPlanConfig& config,
                      const BinaryAccess& access, ChatRequestPlan* plan);
  ObjectReply Persist(const std::string& completion_id,
                      const BinaryAccess& access, io::Json* bindings);
  void Published(const BinaryAccess& access, bool uncertain) noexcept;
  const FileExpansionUsage* Usage() const {
    return files_ ? &files_->Usage() : nullptr;
  }

 private:
  ObjectStore* objects_;
  std::unique_ptr<FileExpansion> files_;
  io::Json original_items_;
};

// The completion ID and message position define the stored public message ID.
std::string StoredChatMessageId(const std::string& completion_id, size_t index);

}  // namespace q4t::server
