#pragma once

#include "q4t/server/application_routes.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/containers.h"

namespace q4t::server::container_detail {
struct Stored {
  ObjectRequest metadata;
  ObjectRequest workspace;
  int64_t expires = 0;
};

ObjectReply Invalid(std::string message, std::string field);
ObjectReply Corrupt();
ObjectReply Read(ObjectStore& objects, const StateAccess& access,
                 std::string_view id, Stored* state);
ObjectReply OpenWorkspace(ObjectStore& objects, const StateAccess& access,
                          Stored* state);
ObjectReply Commit(ObjectStore& objects, const StateAccess& access,
                   Stored state, std::vector<ObjectRequest> extra = {});
ObjectReply Public(const io::Json& record, int64_t now);
ObjectReply ContainerFiles(ObjectStore& objects, const StateAccess& access,
                           const ContainerRoute& route, std::string_view method,
                           const io::Json& query, const std::string& body,
                           Stored state, BinaryRead* download,
                           BinaryStage* upload, std::string_view filename);
ObjectReply PrepareFiles(ObjectStore& objects, const StateAccess& access,
                         const io::Json& ids, Stored* state,
                         std::vector<BinaryRead>* pins,
                         std::vector<ObjectRequest>* writes);
void CloseFiles(ObjectStore& objects, const StateAccess& access,
                std::vector<BinaryRead>* pins);

}  // namespace q4t::server::container_detail
