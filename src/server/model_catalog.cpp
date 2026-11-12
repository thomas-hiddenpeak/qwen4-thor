#include "q4t/server/model_catalog.h"

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
int Hex(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
}  // namespace

Status DecodeModelPath(std::string_view path, std::string* id) {
  id->clear();
  if (!IsModelDetailPath(path) || path.size() > 11 + 256 * 3 ||
      path.find_first_of("?#") != path.npos)
    return Status::Fail("invalid model path");
  std::string decoded;
  path.remove_prefix(std::string_view("/v1/models/").size());
  for (size_t i = 0; i < path.size(); ++i) {
    char c = path[i];
    if (c == '%') {
      if (i + 2 >= path.size() || Hex(path[i + 1]) < 0 ||
          Hex(path[i + 2]) < 0)
        return Status::Fail("invalid model path escaping");
      c = static_cast<char>((Hex(path[i + 1]) << 4) | Hex(path[i + 2]));
      i += 2;
    }
    decoded += c;
  }
  if (!ValidModelIdentifier(decoded))
    return Status::Fail("invalid model identifier");
  *id = std::move(decoded);
  return Status();
}

Status ModelCatalog::Configure(std::string name,
                               std::vector<std::string> aliases,
                               int64_t created) {
  if (!ValidServedModels(name, aliases) || created < 0)
    return Status::Fail("invalid served model names or creation time");
  name_ = std::move(name);
  aliases_ = std::move(aliases);
  created_ = created;
  return Status();
}

std::string ModelCatalog::RetrieveJson(std::string_view id) const {
  if (name_.empty() || !Contains(id)) return {};
  return "{\"id\":\"" + detail::JsonEscape(std::string(id)) +
         "\",\"object\":\"model\",\"created\":" +
         std::to_string(created_) + ",\"owned_by\":\"q4t\"}";
}

std::string ModelCatalog::ListJson() const {
  std::string body = "{\"object\":\"list\",\"data\":[";
  if (!name_.empty()) body += RetrieveJson(name_);
  for (const auto& alias : aliases_) body += ',' + RetrieveJson(alias);
  return body + "]}";
}

}  // namespace q4t::server
