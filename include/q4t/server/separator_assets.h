// Fixed optional SepFormer assets, read by the existing isolated model worker.
#pragma once

#include "q4t/audio/separator_config.h"
#include "q4t/server/model_assets.h"

namespace q4t::server {
inline constexpr size_t kNativeSeparatorConfigBytes = 952;
inline constexpr char kNativeSeparatorConfigSha256[] =
    "bf0654f8f375b96d8ff4bc33de333695f61bfe36b6b3cf8f02656f690de779a6";
struct SeparatorHostAssets {
  std::vector<uint8_t> file_bytes;
  audio::SeparatorTensorLayout layout;
  size_t data_offset = 0;
  std::span<const uint8_t> WeightBytes() const {
    return data_offset <= file_bytes.size()
               ? std::span(file_bytes).subspan(data_offset)
               : std::span<const uint8_t>{};
  }
};
Status LoadSeparatorAssets(const std::string& directory,
                           SeparatorHostAssets* out,
                           GrammarControl control = {},
                           GrammarFailure* failure = nullptr,
                           bool include_weights = true,
                           size_t max_staging_bytes = 128ULL << 20);
}  // namespace q4t::server
