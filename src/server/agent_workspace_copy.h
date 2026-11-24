#pragma once

#include <map>

#include "q4t/server/input_snapshot.h"
#include "q4t/server/responses_state.h"

namespace q4t::server::environment_detail {

// Own preparation holds until the workspace which references the copies
// commits.
class WorkspaceCopies {
 public:
  WorkspaceCopies(ObjectStore& objects, StateAccess access,
                  std::string workspace)
      : objects_(objects),
        access_(std::move(access)),
        workspace_(std::move(workspace)) {}
  WorkspaceCopies(const WorkspaceCopies&) = delete;
  WorkspaceCopies& operator=(const WorkspaceCopies&) = delete;
  ~WorkspaceCopies();
  // The receipts in the pending environment own these pins after commit.
  ObjectReply PinSources(const io::Json& files, io::Json* receipts,
                         int64_t* deadline);
  ObjectReply ResumeSources(const io::Json& receipts);
  void ReleaseSources(const io::Json* receipts = nullptr);
  void Settle(const ObjectReply& result);
  ObjectReply CopyFile(const io::Json& source, const std::string& installed_id,
                       std::string param, uint64_t* size, io::Json* content);
  // Replace references only for the installed path. Other installed versions
  // remain private to this workspace, including after a source Files deletion.
  void Bind(io::Json* record, std::string_view replaced = {}) const;

 private:
  ObjectStore& objects_;
  StateAccess access_;
  std::string workspace_;
  std::map<std::string, BinaryRead> sources_;
  bool release_sources_ = true;
  std::vector<InputSnapshotHold> holds_;
  std::vector<io::Json> references_;
};

ObjectReply ReadWorkspaceCopy(
    ObjectStore& objects, const StateAccess& access,
    const std::string& workspace, const io::Json& record, const io::Json& file,
    const std::function<ObjectReply(std::string_view)>& consume);
}  // namespace q4t::server::environment_detail
