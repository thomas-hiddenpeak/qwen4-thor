#pragma once

#include "agent_workspace_copy.h"
#include "q4t/server/agent_environment.h"
#include "q4t/server/file_input.h"

namespace q4t::server::environment_detail {
// Called before publishing a hosted workspace. Only the caller-owned template
// is opened. Secret configuration stays in the private definition.
ObjectReply PrepareAttachment(ObjectStore& objects, const StateAccess& access,
                              const io::Json& input,
                              const std::string& session_id,
                              const io::Json& vaults, ObjectRequest* write,
                              std::shared_ptr<WorkspaceCopies>* copies);
ObjectReply ResolveDefinition(ObjectStore& objects, const StateAccess& access,
                              const io::Json& environment,
                              io::Json* definition);
ObjectReply PublicTemplate(const io::Json& config, const std::string& id,
                           int64_t created, int64_t updated);
bool ValidWorkspaceFile(std::string_view path);
ObjectReply WorkspaceFiles(ObjectStore& objects, const StateAccess& access,
                           std::string_view method, std::string_view target,
                           const io::Json& input, const io::Json& stored,
                           const io::Json& resource);
ObjectReply SnapshotFile(const io::Json& source, WorkspaceCopies& copies,
                         std::string param, io::Json* installed,
                         io::Json* content);
ObjectReply PrepareWorkspace(ObjectStore& objects, const StateAccess& access,
                             const std::string& id, const io::Json& input,
                             io::Json* private_definition,
                             WorkspaceCopies& copies);
ObjectReply SealDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const io::Json& definition,
                           io::Json* sealed);
ObjectReply OpenDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const io::Json& sealed,
                           io::Json* definition);
}  // namespace q4t::server::environment_detail
