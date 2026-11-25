#pragma once

#include "q4t/server/chatkit.h"

namespace q4t::server::chatkit_detail {
struct Grant;
// A model job and its pending thread revision share a journal transaction.
// Any reader can finish a completed response after a model or server restart.
ObjectReply ReadThread(ObjectStore& objects, const StateAccess& access,
                       const std::string& id, const Grant* grant = nullptr);
ObjectReply BrowserHistory(ObjectStore& objects, const StateAccess& access,
                           const Grant& grant);
ObjectReply BrowserOperation(ObjectStore& objects, const StateAccess& access,
                             const Grant& grant, std::string_view operation,
                             const io::Json& params);
ObjectReply ProjectOutput(const io::Json& response,
                          const std::string& thread_id);
// Preserve rich browser parts while supplying the model with user-visible text.
ObjectReply AppendUserContent(const io::Json& parts, io::Json* content,
                              io::Json* model);
void ProjectManagementItem(io::Json* item);
io::Json ClientThread(const io::Json& data);
io::Json ClientItem(io::Json item);
}  // namespace q4t::server::chatkit_detail
