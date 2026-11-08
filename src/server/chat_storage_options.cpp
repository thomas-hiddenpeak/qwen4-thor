#include "q4t/server/chat_storage.h"

#include <algorithm>
#include <unordered_set>

#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using io::Json;
size_t Characters(std::string_view value) {
  return std::count_if(value.begin(), value.end(), [](unsigned char byte) {
    return (byte & 0xc0) != 0x80;
  });
}
}  // namespace

Status ValidateChatMetadata(const Json& metadata, std::string* param) {
  if (metadata.IsNull()) return {};
  if (!metadata.IsObject() || metadata.object.size() > 16) {
    if (param) *param = "metadata";
    return Status::Fail("metadata requires an object of at most 16 entries");
  }
  std::unordered_set<std::string> seen;
  for (const auto& [key, value] : metadata.object) {
    if (Characters(key) > 64 || !value.IsString() ||
        Characters(value.str) > 512 || !seen.insert(key).second) {
      if (param) *param = InputParamPath("metadata", key);
      return Status::Fail("metadata keys/values require 64/512 characters");
    }
  }
  return {};
}
Status ParseChatStorageOptions(const Json& request, ChatStorageOptions* options,
                               std::string* error_param) {
  *options = {};
  if (const auto* store = request.Find("store")) {
    if (!store->IsNull() && !store->IsBool()) {
      if (error_param) *error_param = "store";
      return Status::Fail("store must be boolean or null");
    }
    options->store = store->IsBool() && store->boolean;
  }
  if (const auto* messages = request.GetArray("messages");
      options->store && messages && messages->array.size() > 4096) {
    if (error_param) *error_param = "messages";
    return Status::Fail("Stored Chat input exceeds 4096 messages");
  }
  if (const auto* metadata = request.Find("metadata")) {
    auto status = ValidateChatMetadata(*metadata, error_param);
    if (!status) return status;
    options->has_metadata = true;
    options->metadata = *metadata;
  }
  return {};
}

}  // namespace q4t::server
