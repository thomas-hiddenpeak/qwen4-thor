#pragma once

#include "file_package.h"
#include "skill_manifest.h"

namespace q4t::server::skill_detail {
constexpr size_t kSkillMaxFiles = 500;
constexpr size_t kSkillFileBytes = 25'000'000;
constexpr size_t kSkillArchiveBytes = 50'000'000;

struct Bundle {
  Manifest manifest;
  std::string archive;
  file_detail::Package members;
};

// Child-only API. The caller enters the native document sandbox after loading
// the required codecs and before reading any customer-provided payload.
ObjectReply LoadSkillRuntime(bool write_zip);
ObjectReply ReadSkillArchive(std::string archive, const BinaryAccess& access,
                             Bundle* output);
ObjectReply MakeSkillArchive(file_detail::Package files,
                             const BinaryAccess& access, Bundle* output);
}  // namespace q4t::server::skill_detail
