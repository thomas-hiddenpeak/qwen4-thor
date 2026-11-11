// Pinned native Qwen3-Embedding-0.6B architecture; no CUDA initialization.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::model {

struct EmbeddingConfig {
  static constexpr int kLayers = 28;
  static constexpr int kHidden = 1024;
  static constexpr int kIntermediate = 3072;
  static constexpr int kQueryHeads = 16;
  static constexpr int kKvHeads = 8;
  static constexpr int kHeadDim = 128;
  static constexpr int kVocab = 151669;
  static constexpr int kEos = 151643;
  static constexpr int kContext = 32768;
  // The standard embeddings endpoint permits <=8192 tokens per input.
  static constexpr int kInputTokens = 8192;
  static constexpr float kNormEpsilon = 1e-6f;
  static constexpr float kRopeTheta = 1000000.0f;
};

struct EmbeddingTensorContract {
  std::string name;
  std::vector<int64_t> shape;
  size_t bytes = 0;  // Exactly BF16, no dtype reinterpretation.
};

Status ValidateEmbeddingConfig(const io::Json& config);
std::vector<EmbeddingTensorContract> EmbeddingTensorContracts();
size_t EmbeddingWeightBytes();

}  // namespace q4t::model
