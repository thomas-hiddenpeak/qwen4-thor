#pragma once

#include <memory>
#include <span>
#include <vector>

#include "q4t/audio/asr_layout.h"
#include "q4t/server/model_assets.h"
#include "q4t/text/tokenizer.h"

namespace q4t::server {
inline constexpr char kNativeAsrRevision[] =
    "5eb144179a02acc5e5ba31e748d22b0cf3e303b0";
inline constexpr char kNativeAsrWeightSha256[] =
    "79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea";
inline constexpr size_t kNativeAsrFileBytes = 1876091704;
struct AsrHostAssets {
  std::unique_ptr<text::Tokenizer> tokenizer;
  std::vector<uint8_t> file_bytes;
  audio::AsrWeightLayout layout;
  size_t data_offset = 0;
  std::span<const uint8_t> WeightBytes() const {
    return data_offset <= file_bytes.size()
               ? std::span(file_bytes).subspan(data_offset)
               : std::span<const uint8_t>{};
  }
};

// Fixed official assets, exact tensor/config/tokenizer checks; no filesystem
// operations in the parent and no GPU mutex/CUDA context required. Failure
// preserves *out. include_weights=false never opens the checkpoint.
Status LoadAsrAssets(const std::string& directory, AsrHostAssets* out,
                     GrammarControl control = {},
                     GrammarFailure* failure = nullptr,
                     bool include_weights = true,
                     size_t max_staging_bytes = 2ULL * 1024 * 1024 * 1024);
}  // namespace q4t::server
