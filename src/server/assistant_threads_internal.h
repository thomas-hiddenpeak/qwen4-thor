#pragma once

#include <vector>

#include "q4t/server/assistant_registry.h"
#include "q4t/server/binary_store.h"

namespace q4t::server::assistant_detail {
ObjectReply Invalid(std::string message, std::string field);
ObjectReply BindResources(ObjectStore& objects, const BinaryAccess& access,
                          io::Json* resources, bool creating,
                          bool ingestion_configured, io::Json* inline_store,
                          std::string* store_id);
struct Attachments {
  std::vector<std::string> code, search;
};
ObjectReply CreateMessage(ObjectStore& objects, const BinaryAccess& access,
                          const std::string& thread, const io::Json& input,
                          Attachments* attachments, io::Json* message);
ObjectReply ListMessages(const io::Json& messages, std::string_view query);
}  // namespace q4t::server::assistant_detail
