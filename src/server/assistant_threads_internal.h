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
ObjectReply LoadThreadDraft(ObjectStore& objects, const StateAccess& access,
                            std::string_view id, ObjectRequest* write);
ObjectReply ReadThread(const io::Json& record, io::Json* data);
ObjectReply SaveThread(ObjectStore& objects, const BinaryAccess& access,
                       bool creating, bool resources_changed, bool ingestion,
                       const Attachments& files, ObjectRequest* write,
                       std::vector<ObjectRequest>* prepared = nullptr);
ObjectReply CreateMessage(ObjectStore& objects, const BinaryAccess& access,
                          const std::string& thread, const io::Json& input,
                          Attachments* attachments, io::Json* message);
ObjectReply PageAssistantRows(const io::Json& rows, const io::Json& page,
                              bool allow_empty_page = false);
ObjectReply ListMessages(const io::Json& messages, std::string_view query);
}  // namespace q4t::server::assistant_detail
