#pragma once

#include "q4t/server/file_document.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {
enum class ContainerShape {
  kCreate,
  kCreated,
  kResource,
  kListItem,
  kQuery,
  kFileCreated,
  kFileResource,
  kFileListItem,
  kFileQuery
};
Status ValidateContainer(const io::Json& value, ContainerShape shape,
                         ChatPlanError* error);
// Read checked workspace-relative paths and exact bytes for the hosted tool.
ObjectReply ReadContainerSkillFiles(ObjectStore& objects,
                                    const StateAccess& access,
                                    std::string_view container,
                                    std::vector<FilePackageEntry>* files);
// Run one tool call against a scoped snapshot. Publish its file delta
// atomically.
ObjectReply ExecuteContainerPython(ObjectStore& objects,
                                   const StateAccess& access,
                                   std::string_view container,
                                   std::string_view code);
std::string AssistantOutputFileId(const std::string& message,
                                  const std::string& container_file);
ObjectReply ExportAssistantOutputFile(ObjectStore& objects,
                                      const StateAccess& access,
                                      const std::string& message,
                                      const io::Json& file);
ObjectReply HandleContainers(ObjectStore& objects, const StateAccess& access,
                             std::string_view method, std::string_view target,
                             const std::string& body,
                             BinaryRead* download = nullptr,
                             BinaryStage* upload = nullptr,
                             std::string_view filename = {});
}  // namespace q4t::server
