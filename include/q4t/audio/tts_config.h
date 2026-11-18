// Fixed native Qwen3-TTS profiles. Metadata validation never initializes CUDA.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::audio {

enum class TtsVariant { kCustomVoice06B, kCustomVoice17B };

struct TtsTensorContract {
  std::string name;
  std::vector<int64_t> shape;
  size_t bytes = 0;
};

struct TtsConfig {
  static constexpr int kTextHidden = 2048;
  static constexpr int kTextVocab = 151936;
  static constexpr int kTalkerLayers = 28;
  static constexpr int kPredictorLayers = 5;
  static constexpr int kPredictorHidden = 1024;
  static constexpr int kHeadDim = 128;
  static constexpr int kQueryHeads = 16;
  static constexpr int kKvHeads = 8;
  static constexpr int kCodeGroups = 16;
  static constexpr int kCodebook = 2048;
  static constexpr int kCodecVocab = 3072;
  static constexpr int kCodecEos = 2150;
  static constexpr int kSampleRate = 24000;
  static constexpr int kSamplesPerFrame = 1920;
  static constexpr uint64_t kCodecFileDataBytes = 682229764;
  static constexpr uint64_t kCodecDecoderBytes = 457292548;
};

Status ValidateTtsConfig(const io::Json& config, TtsVariant variant);
Status ValidateTtsGenerationConfig(const io::Json& config);
Status ValidateTtsCodecConfig(const io::Json& config);
std::vector<TtsTensorContract> TtsTensorContracts(TtsVariant variant);
std::vector<TtsTensorContract> TtsCodecDecoderTensorContracts();

// Requires the complete trained BF16 talker/predictor graph and disjoint,
// hole-free extents. Caller must also verify the entire asset's pinned SHA256.
Status ValidateTtsTensorHeader(const io::Json& header, uint64_t data_bytes,
                               TtsVariant variant);

// Base uses a trained 2048-dimensional speaker condition. These independent
// metadata contracts do not imply that a registered voice backend is loaded.
Status ValidateTtsBaseConfig(const io::Json& config);
std::vector<TtsTensorContract> TtsBaseTensorContracts();
Status ValidateTtsBaseTensorHeader(const io::Json& header, uint64_t data_bytes);

// Validates every F32 storage extent and all 271 synthesis-decoder tensors.
// The other 225 encoder tensors are retained, not executed or loaded as the
// decoder. Their semantics are not certified by this function; the complete
// pinned file hash is mandatory before any model use.
Status ValidateTtsCodecTensorHeader(const io::Json& header,
                                    uint64_t data_bytes);

}  // namespace q4t::audio
