// Durable native retrieval ownership and jobs. No model or network IO here.
#pragma once

#include "q4t/server/vector_index.h"

namespace q4t::server {
inline constexpr size_t kVectorStoreFiles = 10000;

struct VectorIngestionClaim {
  std::string store_id, file_id, member_id, token, owner;
  int64_t deadline_at = 0;
};

class VectorStores {
 public:
  explicit VectorStores(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Create(const BinaryAccess& access, const io::Json& request);
  ObjectReply Retrieve(const BinaryAccess& access, const std::string& id);
  ObjectReply Update(const BinaryAccess& access, const std::string& id,
                     const io::Json& request);
  ObjectReply Erase(const BinaryAccess& access, const std::string& id);
  ObjectReply List(const BinaryAccess& access, ObjectRequest page);
  ObjectReply Attach(const BinaryAccess& access, const std::string& id,
                     const io::Json& request);
  ObjectReply RetrieveFile(const BinaryAccess& access, const std::string& id,
                           const std::string& file_id);
  ObjectReply UpdateFile(const BinaryAccess& access, const std::string& id,
                         const std::string& file_id, const io::Json& request);
  ObjectReply Detach(const BinaryAccess& access, const std::string& id,
                     const std::string& file_id);
  // Page is an already decoded query (limit is numeric). No private IDs leak.
  ObjectReply ListFiles(const BinaryAccess& access, const std::string& id,
                        const io::Json& page);
  // Actual search/tool use advances last_active_at; management reads do not.
  ObjectReply Touch(const BinaryAccess& access, const std::string& id);

  // The durable claim survives receipt loss. Expiry never grants permission
  // to race a live owner. Only queued work can be claimed; recovery must prove
  // the previous execution drained/exited before calling RequeueAfterExit.
  ObjectReply Claim(const BinaryAccess& access, const std::string& id,
                    const std::string& file_id, std::string owner,
                    int lifetime_seconds, VectorIngestionClaim* claim);
  ObjectReply LoadClaim(const BinaryAccess& access,
                        const VectorIngestionClaim& claim);
  ObjectReply RequeueAfterExit(const BinaryAccess& access,
                               const VectorIngestionClaim& claim);
  ObjectReply Finish(const BinaryAccess& access,
                     const VectorIngestionClaim& claim,
                     const VectorIndexIdentity& identity,
                     const InputSnapshotRef& reference);
  ObjectReply Fail(const BinaryAccess& access,
                   const VectorIngestionClaim& claim, std::string code,
                   std::string message);
  // A trusted executor uses this to cancel after its model/IO cleanup. It is
  // not an HTTP cancellation acknowledgement while work is still running.
  ObjectReply Cancel(const BinaryAccess& access,
                     const VectorIngestionClaim& claim);
  // Durable intent only; running work remains in_progress until drained.
  ObjectReply RequestCancel(const BinaryAccess& access, const std::string& id,
                            const std::string& file_id);
  // Private membership state, including a deleted source, for terminal
  // reconciliation. Execution/search must use LoadFile/LoadClaim instead.
  ObjectReply LoadRecord(const BinaryAccess& access, const std::string& id,
                         const std::string& file_id);
  // Private, scoped, current membership + source-file check for the executor.
  ObjectReply LoadFile(const BinaryAccess& access, const std::string& id,
                       const std::string& file_id);

 private:
  ObjectReply Read(const BinaryAccess& access, const std::string& id,
                   bool require_live = false);
  ObjectReply ReadMember(const BinaryAccess& access, const io::Json& store,
                         const std::string& file_id);
  ObjectReply Publish(const BinaryAccess& access, const io::Json& before,
                      io::Json data,
                      std::span<const ObjectRequest> children = {});
  ObjectReply ChangeJob(const BinaryAccess& access,
                        const VectorIngestionClaim& claim,
                        std::string_view action, const io::Json& completion);
  ObjectStore& objects_;
};
}  // namespace q4t::server
