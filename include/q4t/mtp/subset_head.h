#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "q4t/status.h"

namespace q4t::mtp {
inline constexpr int kSubsetRows = 65536;
inline constexpr size_t kSubsetWeightBytes = size_t{kSubsetRows} * 2560 * 2;
inline constexpr size_t kSubsetIdBytes = size_t{kSubsetRows} * sizeof(int32_t);

// Conservative allowance for files read while validating the artifact.
inline constexpr size_t kSubsetReadCacheBudget = size_t{4} << 30;

struct SubsetHeadData {
  std::vector<uint16_t> weights;
  std::vector<int32_t> ids;
};
// Strict host validation. Empty path is handled by the caller, never here.
// Model files and derived files are read-only. Output is transactional.
Status ReadSubsetHead(const std::string& directory,
                      const std::string& model_directory, int vocab, int hs,
                      SubsetHeadData* output);
}  // namespace q4t::mtp
