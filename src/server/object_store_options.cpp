#include "q4t/server/object_store_options.h"

namespace q4t::server {
namespace {
bool ValidDirectory(std::string_view path) {
  if (path.empty() || path.front() != '/' || path.size() > 4096 ||
      path.find('\0') != path.npos)
    return false;
  bool component = false;
  while (!path.empty()) {
    const auto end = path.find('/');
    const auto name = path.substr(0, end);
    if (name == "." || name == "..") return false;
    component |= !name.empty();
    if (end == path.npos) break;
    path.remove_prefix(end + 1);
  }
  return component;
}
}  // namespace

Status ValidateObjectStoreOptions(const ServerOptions& options) {
  constexpr uint64_t maximum = (1ULL << 53) - 1;
  if (!options.batch_model_queue_tokens ||
      options.batch_model_queue_tokens > maximum ||
      !options.batch_client_model_queue_tokens ||
      options.batch_client_model_queue_tokens >
          options.batch_model_queue_tokens)
    return Status::Fail("invalid Batch model/client queued input token limits");
  if (!options.object_store_dir.empty() &&
      !ValidDirectory(options.object_store_dir))
    return Status::Fail(
        "object-store-dir requires an absolute directory without dot "
        "components");
  if (options.object_max_object_mib < 1 || options.object_max_object_mib > 32 ||
      options.object_max_client_mib < options.object_max_object_mib ||
      options.object_max_total_mib < options.object_max_client_mib ||
      options.object_max_total_mib > 1048576 ||
      options.object_max_client_objects < 1 ||
      options.object_max_total_objects < options.object_max_client_objects ||
      options.object_max_total_objects > 100000 ||
      options.object_io_timeout_ms < 1 || options.object_io_timeout_ms > 30000)
    return Status::Fail("invalid object-store byte/object/timeout limits");
  return {};
}

ObjectStoreOptions ResolveObjectStoreOptions(const ServerOptions& options,
                                             std::string_view xdg_state_home,
                                             std::string_view home_directory) {
  ObjectStoreOptions result;
  result.directory = options.object_store_dir;
  if (result.directory.empty()) {
    if (ValidDirectory(xdg_state_home))
      result.directory = std::string(xdg_state_home) + "/q4t/objects";
    else if (ValidDirectory(home_directory))
      result.directory =
          std::string(home_directory) + "/.local/state/q4t/objects";
  }
  result.max_object_bytes = static_cast<uint64_t>(options.object_max_object_mib)
                            << 20;
  result.max_client_bytes = static_cast<uint64_t>(options.object_max_client_mib)
                            << 20;
  result.max_total_bytes = static_cast<uint64_t>(options.object_max_total_mib)
                           << 20;
  result.max_client_objects = options.object_max_client_objects;
  result.max_total_objects = options.object_max_total_objects;
  result.io_timeout_ms = options.object_io_timeout_ms;
  result.batch_model_queue_tokens = options.batch_model_queue_tokens;
  result.batch_client_model_queue_tokens =
      options.batch_client_model_queue_tokens;
  return result;
}
}  // namespace q4t::server
