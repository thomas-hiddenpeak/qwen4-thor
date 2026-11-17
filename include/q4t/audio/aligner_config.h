// Native fixed Qwen3-ForcedAligner-0.6B contract; no CUDA initialization.
#pragma once

#include "q4t/audio/asr_config.h"

namespace q4t::audio {

struct AlignerConfig : AsrConfig {
  static constexpr const char* kModelId = "qwen3-forced-aligner-0.6b";
  static constexpr int kEncoderLayers = 24;
  static constexpr int kEncoderHidden = 1024;
  static constexpr int kEncoderHeads = 16;
  static constexpr int kEncoderIntermediate = 4096;
  static constexpr int kVocab = 152064;
  static constexpr int kContext = 8192;
  static constexpr int kClasses = 5000;
  static constexpr int kTimestampToken = 151705;
  static constexpr int kTimestampMilliseconds = 80;
};

Status ValidateAlignerConfig(const io::Json& config);
Status ValidateAlignerPreprocessorConfig(const io::Json& config);
// This metadata is checked for identity only. Alignment classifies timestamp
// slots in the provided transcript; it does not autoregress to an EOS token.
Status ValidateAlignerGenerationConfig(const io::Json& config);
std::vector<AsrTensorContract> AlignerTensorContracts();
size_t AlignerWeightBytes();
Status ValidateAlignerTensorHeader(const io::Json& header, uint64_t data_bytes);

}  // namespace q4t::audio
