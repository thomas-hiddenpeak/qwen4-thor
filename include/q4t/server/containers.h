#pragma once

#include "q4t/server/binary_store.h"
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
ObjectReply HandleContainers(ObjectStore& objects, const StateAccess& access,
                             std::string_view method, std::string_view target,
                             const std::string& body,
                             BinaryRead* download = nullptr,
                             BinaryStage* upload = nullptr,
                             std::string_view filename = {});
}  // namespace q4t::server
