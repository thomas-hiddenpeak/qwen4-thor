#pragma once

#include "q4t/server/application_routes.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/responses_state.h"

namespace q4t::server::agent_detail {

ObjectReply HandleArtifacts(ObjectStore& objects, const StateAccess& access,
                            const AgentSessionRoute& route,
                            std::string_view method, const io::Json& query,
                            int fd, bool* sent);

// Own staged output chunks until the terminal Response and all artifacts
// commit together. An uncertain outcome is reconciled by durable retention.
class TurnArtifacts {
 public:
  TurnArtifacts(ObjectStore& objects, const StateAccess& access)
      : objects_(objects), binary_(objects), access_(access) {}
  ~TurnArtifacts();
  TurnArtifacts(const TurnArtifacts&) = delete;
  TurnArtifacts& operator=(const TurnArtifacts&) = delete;

  ObjectReply Prepare(const std::string& session, const std::string& response,
                      std::vector<ObjectRequest>* writes);
  void Settle(const ObjectReply& outcome);

 private:
  ObjectReply Stage(const io::Json& file, const io::Json& installed,
                    const io::Json& workspace, const io::Json& resource,
                    std::vector<ObjectRequest>* writes);
  ObjectStore& objects_;
  BinaryStore binary_;
  StateAccess access_;
  std::vector<BinaryStage> stages_;
};

}  // namespace q4t::server::agent_detail
