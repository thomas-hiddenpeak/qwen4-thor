#pragma once

#include <string>
#include <vector>

#include "q4t/server/responses_state.h"

namespace q4t::server {

struct ItemViewOptions {
  int limit = 20;
  bool descending = true;
  std::string after;
  std::vector<std::string> include;
};

ObjectReply NormalizeConversationMetadata(const io::Json& metadata);

// Query values have already been decoded from HTTP (limit is a JSON number).
// Used by Conversations and Responses input-item views; does no storage work.
Status ParseItemViewOptions(const io::Json& query, ItemViewOptions* options,
                            ChatPlanError* error, bool pagination = true);
io::Json ProjectStoredItem(const io::Json& item,
                           const ItemViewOptions& options);
ObjectReply PageStoredItems(const io::Json& items,
                            const ItemViewOptions& options);

// Stores canonical, resolved items, not generation requests. The input adapter
// resolves references and supplies IDs/status before these methods are called.
// Shape validation deliberately does not demand a completed call/result pair.
class ConversationState {
 public:
  explicit ConversationState(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Create(const io::Json& metadata, const io::Json& items,
                     const StateAccess& access);
  ObjectReply Retrieve(const std::string& id, const StateAccess& access);
  ObjectReply Update(const std::string& id, const io::Json& metadata,
                     const StateAccess& access);
  // Deleting a conversation leaves its independently stored items available.
  ObjectReply Erase(const std::string& id, const StateAccess& access);
  ObjectReply Append(const std::string& id, const io::Json& items,
                     const ItemViewOptions& options, const StateAccess& access);
  ObjectReply ListItems(const std::string& id, const ItemViewOptions& options,
                        const StateAccess& access);
  ObjectReply RetrieveItem(const std::string& id, const std::string& item_id,
                           const ItemViewOptions& options,
                           const StateAccess& access);
  // Removes membership from this conversation. Other histories and references
  // to the same item are independent and are not retroactively rewritten.
  ObjectReply EraseItem(const std::string& id, const std::string& item_id,
                        const StateAccess& access);

 private:
  ObjectReply Read(const std::string& id, const StateAccess& access);
  ObjectReply Publish(const std::string& id, int64_t revision, io::Json data,
                      const io::Json& new_items, const StateAccess& access);
  ObjectStore& objects_;
};

}  // namespace q4t::server
