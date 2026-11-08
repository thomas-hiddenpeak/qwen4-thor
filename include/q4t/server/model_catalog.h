#pragma once

#include <algorithm>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/status.h"

namespace q4t::server {

// Identifiers are labels, never filesystem paths. Keep exact UTF-8 bytes.
inline bool ValidModelIdentifier(std::string_view id) {
  if (id.empty() || id.size() > 256) return false;
  uint32_t value = 0, minimum = 0;
  int remaining = 0;
  for (const unsigned char c : id) {
    if (remaining) {
      if ((c & 0xc0) != 0x80) return false;
      value = (value << 6) | (c & 0x3f);
      if (--remaining == 0 &&
          (value < minimum || value > 0x10ffff ||
           (value >= 0xd800 && value <= 0xdfff)))
        return false;
    } else if (c < 0x80) {
      if (c <= 0x20 || c == 0x7f) return false;
    } else if (c >= 0xc2 && c <= 0xdf) {
      value = c & 0x1f;
      minimum = 0x80;
      remaining = 1;
    } else if (c >= 0xe0 && c <= 0xef) {
      value = c & 0x0f;
      minimum = 0x800;
      remaining = 2;
    } else if (c >= 0xf0 && c <= 0xf4) {
      value = c & 0x07;
      minimum = 0x10000;
      remaining = 3;
    } else {
      return false;
    }
  }
  return remaining == 0;
}

inline bool ValidServedModels(std::string_view name,
                              std::span<const std::string> aliases) {
  if (!ValidModelIdentifier(name) || aliases.size() > 32) return false;
  for (size_t i = 0; i < aliases.size(); ++i) {
    if (!ValidModelIdentifier(aliases[i]) || aliases[i] == name ||
        std::find(aliases.begin(), aliases.begin() + i, aliases[i]) !=
            aliases.begin() + i)
      return false;
  }
  return true;
}

inline bool IsServedModel(std::string_view id, std::string_view name,
                          std::span<const std::string> aliases) {
  return id == name || std::find(aliases.begin(), aliases.end(), id) !=
                           aliases.end();
}

inline bool IsModelDetailPath(std::string_view path) {
  return path.starts_with("/v1/models/");
}

// Decode one URL component exactly once; a decoded slash stays in the label.
Status DecodeModelPath(std::string_view path, std::string* id);

class ModelCatalog {
 public:
  Status Configure(std::string name, std::vector<std::string> aliases,
                    int64_t created);
  bool Contains(std::string_view id) const {
    return IsServedModel(id, name_, aliases_);
  }
  std::string ListJson() const;
  // Call only for an actual registered model. Unknown IDs yield no object.
  std::string RetrieveJson(std::string_view id) const;
  const std::vector<std::string>& Aliases() const { return aliases_; }

 private:
  std::string name_;
  std::vector<std::string> aliases_;
  int64_t created_ = 0;
};

}  // namespace q4t::server
