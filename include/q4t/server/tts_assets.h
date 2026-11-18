// Verified local speech assets; all filesystem I/O stays in the owned worker.
#pragma once

#include <memory>
#include <span>
#include <vector>

#include "q4t/audio/tts_layout.h"
#include "q4t/server/model_assets.h"
#include "q4t/text/tokenizer.h"

namespace q4t::server {
inline constexpr char kNativeTtsModel[] = "qwen3-tts-12hz-1.7b-customvoice";
inline constexpr char kNativeTtsProfile[] = "q4t-native-tts-v1";
inline constexpr char kNativeTtsRevision[] =
    "0c0e3051f131929182e2c023b9537f8b1c68adfe";
inline constexpr char kNativeTtsWeightSha256[] =
    "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137";
inline constexpr char kNativeTtsCodecWeightSha256[] =
    "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258";
inline constexpr size_t kNativeTtsFileBytes = 3833402552;
inline constexpr size_t kNativeTtsCodecFileBytes = 682293092;
inline constexpr size_t kNativeTtsWeightBytes = 7666705408;
inline constexpr size_t kTtsAssetStagingBytes = 5ULL << 30;

struct TtsHostWeightFile {
  std::vector<uint8_t> bytes;
  audio::TtsWeightLayout layout;
  size_t data_offset = 0;
  std::span<const uint8_t> WeightBytes() const {
    return data_offset <= bytes.size() ? std::span(bytes).subspan(data_offset)
                                       : std::span<const uint8_t>{};
  }
};
struct TtsHostAssets {
  std::unique_ptr<text::Tokenizer> tokenizer;
  TtsHostWeightFile talker, codec;
};

// Atomically publishes both trained profiles, including speech_tokenizer/.
// Metadata-only loading opens neither checkpoint. Both stages share one
// deadline. max_staging_bytes covers their combined raw bytes; parsed objects,
// device weights and expansion scratch need separate RAM admission. Payloads
// use verified aligned direct I/O; metadata file pages fit the host margin.
// No remote code/downloads, CUDA calls or full FP32 host copy are performed.
Status LoadTtsAssets(const std::string& directory, TtsHostAssets* out,
                     GrammarControl control = {},
                     GrammarFailure* failure = nullptr,
                     bool include_weights = true,
                     size_t max_staging_bytes = kTtsAssetStagingBytes);
}  // namespace q4t::server
