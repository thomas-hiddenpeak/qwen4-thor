// Pinned native Qwen3-Embedding-0.6B architecture; no CUDA initialization.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::model {

inline bool ValidEmbeddingAssetDirectory(std::string_view path) {
  if (path.size() < 2 || path.size() > 4096 || path.front() != '/' ||
      path.back() == '/' || path.find('\0') != path.npos)
    return false;
  for (size_t start = 1; start < path.size();) {
    const auto end = path.find('/', start);
    const auto part = path.substr(start, end == path.npos ? end : end - start);
    if (part.empty() || part == "." || part == "..") return false;
    if (end == path.npos) break;
    start = end + 1;
  }
  return true;
}

struct EmbeddingConfig {
  static constexpr const char* kModelId = "qwen3-embedding-0.6b";
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
  static constexpr int kMinimumDimensions = 32;
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
