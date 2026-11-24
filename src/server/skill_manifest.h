#pragma once

#include <string>
#include <string_view>

#include "q4t/server/object_store.h"

namespace q4t::server::skill_detail {
struct Manifest {
  std::string name;
  std::string description;
};

// For the isolated archive worker. Initialize system libraries and the UTF-8
// locale before entering its syscall sandbox, then parse customer bytes.
ObjectReply LoadManifestRuntime();
ObjectReply ReadManifest(std::string_view folder, std::string_view markdown,
                         Manifest* output);
}  // namespace q4t::server::skill_detail
