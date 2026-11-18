// Verified local speech assets; all filesystem I/O stays in the owned worker.
#pragma once

#include <memory>
#include <span>
#include <vector>

#include "q4t/audio/tts_speaker_layout.h"
#include "q4t/audio/tts_encoder_layout.h"
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
inline constexpr char kNativeTtsBaseModel[] = "qwen3-tts-12hz-1.7b-base";
inline constexpr char kNativeTtsBaseRevision[] =
    "fd4b254389122332181a7c3db7f27e918eec64e3";
inline constexpr char kNativeTtsBaseWeightSha256[] =
    "38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c";
inline constexpr size_t kNativeTtsBaseFileBytes = 3857413744;
inline constexpr size_t kNativeTtsFileBytes = 3833402552;
inline constexpr size_t kNativeTtsCodecFileBytes = 682293092;
inline constexpr size_t kNativeTtsWeightBytes = 7666705408;
inline constexpr size_t kTtsAssetStagingBytes = 5ULL << 30;
inline constexpr size_t kNativeTtsVoiceEncoderOffset =
    (audio::TtsSpeakerConfig::kDeviceBytes + 255) / 256 * 256;
inline constexpr size_t kNativeTtsVoiceWeightBytes =
    kNativeTtsVoiceEncoderOffset + audio::TtsEncoderConfig::kWeightBytes;

struct TtsHostWeightFile {
  std::vector<uint8_t> bytes;
  audio::TtsWeightLayout layout;
  size_t data_offset = 0;
  std::span<const uint8_t> WeightBytes() const {
    return data_offset <= bytes.size() ? std::span(bytes).subspan(data_offset)
                                       : std::span<const uint8_t>{};
  }
};
struct TtsAssetSelection {
  audio::TtsVariant variant = audio::TtsVariant::kCustomVoice17B;
  // Speaker-only component qualification can omit the codec. Registration
  // additionally validates its encoder; synthesis consumes the decoder only.
  bool include_codec = true;
  bool include_encoder = false;
  bool Valid() const {
    return (variant == audio::TtsVariant::kBase17B ||
            (variant == audio::TtsVariant::kCustomVoice17B && include_codec)) &&
           (!include_encoder ||
            (variant == audio::TtsVariant::kBase17B && include_codec));
  }
};
struct TtsHostAssets {
  audio::TtsVariant variant = audio::TtsVariant::kCustomVoice17B;
  audio::TtsWeightLayout speaker_layout, encoder_layout;
  std::unique_ptr<text::Tokenizer> tokenizer;
  TtsHostWeightFile talker, codec;
};

// Atomically publishes the selected actual trained profile and optional codec.
// Base source includes speaker + talker, even for speaker-only registration.
// Both subgraph extents are parsed; device allocation is the caller's choice.
// Metadata-only loading opens neither checkpoint. Both stages share one
// deadline. max_staging_bytes covers their combined raw bytes; parsed objects,
// device weights and expansion scratch need separate RAM admission. Payloads
// use verified aligned direct I/O; metadata file pages fit the host margin.
// No remote code/downloads, CUDA calls or full FP32 host copy are performed.
Status LoadTtsAssets(const std::string& directory, TtsHostAssets* out,
                     GrammarControl control = {},
                     GrammarFailure* failure = nullptr,
                     bool include_weights = true,
                     size_t max_staging_bytes = kTtsAssetStagingBytes,
                     TtsAssetSelection selection = {});
}  // namespace q4t::server
