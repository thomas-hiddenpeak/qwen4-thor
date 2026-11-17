// Fixed optional ECAPA assets, read by the existing isolated model worker.
#pragma once

#include "q4t/audio/speaker_config.h"
#include "q4t/server/model_assets.h"

namespace q4t::server {
inline constexpr size_t kNativeSpeakerConfigBytes = 1175;
inline constexpr char kNativeSpeakerConfigSha256[] =
    "a173ae14a11bacabd30a71535bc581e5aab1a5871855ba939e002b3284a398ac";
struct SpeakerHostAssets {
  std::vector<uint8_t> file_bytes;
  audio::SpeakerTensorLayout layout;
  size_t data_offset = 0;
  std::span<const uint8_t> WeightBytes() const {
    return data_offset <= file_bytes.size()
               ? std::span(file_bytes).subspan(data_offset)
               : std::span<const uint8_t>{};
  }
};
Status LoadSpeakerAssets(const std::string& directory, SpeakerHostAssets* out,
                         GrammarControl control = {},
                         GrammarFailure* failure = nullptr,
                         bool include_weights = true,
                         size_t max_staging_bytes = 128ULL << 20);
}  // namespace q4t::server
