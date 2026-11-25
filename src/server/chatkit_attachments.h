#pragma once

#include "chatkit_credentials.h"
#include "q4t/server/binary_store.h"

namespace q4t::server::chatkit_detail {
struct ReservedFile {
  std::string name, mime;
  uint64_t bytes = 0;
};

ObjectReply ReserveAttachment(ObjectStore& objects, const StateAccess& access,
                              const Grant& grant, const io::Json& params,
                              ChatKitIssuer issuer, std::string_view base);
ObjectReply ReadReservation(ObjectStore& objects, const StateAccess& access,
                            Grant* grant, std::string_view id,
                            std::string_view token, ReservedFile* file);
ObjectReply AttachmentLimit(const Grant& grant, uint64_t* limit);
ObjectReply PublishAttachment(ObjectStore& objects, const BinaryAccess& access,
                              const Grant& grant, BinaryStage* stage,
                              std::string_view name, std::string_view mime,
                              std::string_view reserved_id = {},
                              std::string_view token = {});
ObjectReply ReadAttachmentPreview(ObjectStore& objects,
                                  const StateAccess& access,
                                  std::string_view session,
                                  std::string_view file, std::string_view token,
                                  std::string* png);
ObjectReply RemoveAttachment(ObjectStore& objects, const StateAccess& access,
                             const Grant& grant, const std::string& id);
io::Json AttachmentModelInput(const io::Json& resource);
ObjectReply ResolveAttachments(ObjectStore& objects, const StateAccess& access,
                               const Grant& grant, const io::Json& ids,
                               io::Json* published, io::Json* model);
}  // namespace q4t::server::chatkit_detail
