// Native Qwen3-ASR-0.6B contract; configuration checks never initialize CUDA.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::audio {

struct AsrConfig {
  static constexpr const char* kModelId = "qwen3-asr-0.6b";
  static constexpr int kMelBins = 128;
  static constexpr int kSampleRate = 16000;
  static constexpr int kFft = 400;
  static constexpr int kHop = 160;
  static constexpr int kEncoderLayers = 18;
  static constexpr int kEncoderHidden = 896;
  static constexpr int kEncoderHeads = 14;
  static constexpr int kEncoderHeadDim = 64;
  static constexpr int kEncoderIntermediate = 3584;
  static constexpr int kConvChannels = 480;
  static constexpr int kConvFrequency = 16;
  static constexpr int kMelChunk = 100;
  static constexpr int kEncodedChunk = 13;
  static constexpr int kAttentionWindowMel = 800;
  static constexpr int kSourcePositions = 1500;
  static constexpr float kEncoderNormEpsilon = 1e-5f;
  static constexpr int kDecoderLayers = 28;
  static constexpr int kHidden = 1024;
  static constexpr int kIntermediate = 3072;
  static constexpr int kQueryHeads = 16;
  static constexpr int kKvHeads = 8;
  static constexpr int kHeadDim = 128;
  static constexpr int kVocab = 151936;
  static constexpr int kContext = 65536;
  static constexpr float kNormEpsilon = 1e-6f;
  static constexpr float kRopeTheta = 1000000.0f;
  static constexpr int kAudioStart = 151669;
  static constexpr int kAudioEnd = 151670;
  static constexpr int kAudioPad = 151676;
  static constexpr int kEos = 151643;
  static constexpr int kImEnd = 151645;
};

struct AsrTensorContract {
  std::string name;
  std::vector<int64_t> shape;
  size_t bytes = 0;  // Every stored tensor is BF16, including both heads.
};

Status ValidateAsrConfig(const io::Json& config);
Status ValidateAsrPreprocessorConfig(const io::Json& config);
Status ValidateAsrGenerationConfig(const io::Json& config);
std::vector<AsrTensorContract> AsrTensorContracts();
size_t AsrWeightBytes();

// A full 100-frame chunk produces 13 positions. The final partial chunk is
// convolved separately, so ceil(total_frames / 8) is not the trained layout.
// Defined for the entire size_t range; does not impose an HTTP audio budget.
size_t AsrEncodedFrames(size_t mel_frames);

// Validate the entire checkpoint metadata before allocating/loading weights.
// Requires exactly the trained names, BF16 shapes and disjoint, hole-free
// extents covering data_bytes. The caller verifies the enclosing file bounds.
Status ValidateAsrTensorHeader(const io::Json& header, uint64_t data_bytes);

}  // namespace q4t::audio
