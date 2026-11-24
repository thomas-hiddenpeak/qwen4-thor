#pragma once

#include "chatkit_credentials.h"
#include "q4t/server/binary_store.h"

namespace q4t::server::chatkit_detail {
ObjectReply AttachmentLimit(const Grant& grant, uint64_t* limit);
ObjectReply PublishAttachment(ObjectStore& objects, const BinaryAccess& access,
                              const Grant& grant, BinaryStage* stage,
                              std::string_view name, std::string_view mime);
ObjectReply RemoveAttachment(ObjectStore& objects, const StateAccess& access,
                             const Grant& grant, const std::string& id);
ObjectReply ResolveAttachments(ObjectStore& objects, const StateAccess& access,
                               const Grant& grant, const io::Json& ids,
                               io::Json* published, io::Json* model);
}  // namespace q4t::server::chatkit_detail
