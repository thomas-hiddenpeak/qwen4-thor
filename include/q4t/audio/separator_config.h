// Fixed native SepFormer graph and tensor contract; no GPU initialization.
// SpeechBrain attribution: third_party/licenses/speechbrain/README.md.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "q4t/io/json.h"

namespace q4t::audio {
struct SeparatorConfig {
  static constexpr char kProfile[] = "q4t-native-sepformer-v1";
  static constexpr char kModelId[] = "speechbrain-sepformer-wsj02mix";
  static constexpr char kRevision[] =
      "3a2826343a10e2d2e8a75f79aeab5ff3a2473531";
  static constexpr char kSourceRevision[] =
      "fc2eabb7416b0ae68b3baaadb39972ea1d153985";
  static constexpr char kWeightSha256[] =
      "5f5cbbd10eae7be4e56b752cbc0d27f02305dd0e5cdc3d9c115b51881ca52299";
  static constexpr size_t kFileBytes = 113011580;
  static constexpr size_t kWeightBytes = 112957444;
  static constexpr size_t kHeaderBytes = 54128;
  static constexpr size_t kTensors = 417;
  static constexpr int kSampleRate = 8000;
  static constexpr int kSources = 2;
  static constexpr int kChannels = 256;
  static constexpr int kKernel = 16;
  static constexpr int kStride = 8;
  static constexpr int kChunk = 250;
  static constexpr int kHeads = 8;
  static constexpr int kHeadDim = 32;
  static constexpr int kFfn = 1024;
  static constexpr int kBlocks = 2;
  static constexpr int kLayers = 8;
  static constexpr int kPositions = 2500;
};

struct SeparatorTensorContract {
  std::string name;
  std::vector<int64_t> shape;
  size_t bytes = 0;
};
struct SeparatorTensorLayout {
  std::array<size_t, SeparatorConfig::kTensors> offsets{};
};
std::vector<SeparatorTensorContract> SeparatorTensorContracts();
Status ValidateSeparatorConfig(const io::Json& config);
// Every name, F32 shape and canonical interval must match. Output is unchanged
// on failure/cancellation. The enclosing asset owner verifies the full digest.
Status ParseSeparatorTensorLayout(const io::Json& header, uint64_t data_bytes,
                                  SeparatorTensorLayout* out,
                                  std::function<bool()> cancelled = {});
}  // namespace q4t::audio
