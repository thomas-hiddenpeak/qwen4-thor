#pragma once

#include "q4t/server/file_document.h"

namespace q4t::server {
struct SkillPackage {
  std::string archive;
  std::vector<FilePackageEntry> files;
  std::string name;
  std::string description;
};
// Supply either a ZIP or multipart directory members. On success the archive
// is downloadable, and the caller can request the checked members for mounting.
ObjectReply ProcessSkillPackage(SkillPackage input, bool unpack,
                                const BinaryAccess& access,
                                SkillPackage* output);
int MaybeRunSkillWorker(int argc, char** argv);
}  // namespace q4t::server
