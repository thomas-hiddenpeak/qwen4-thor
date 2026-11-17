// Fixed native ECAPA-TDNN embedding contract. No model/GPU initialization.
// SpeechBrain source and Apache-2.0 notice: third_party/licenses/speechbrain.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "q4t/io/json.h"

namespace q4t::audio {
struct SpeakerConfig {
  static constexpr char kProfile[] = "q4t-native-ecapa-speaker-v1";
  static constexpr char kModelId[] = "speechbrain-ecapa-voxceleb";
  static constexpr char kRevision[] =
      "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286";
  static constexpr char kSourceRevision[] =
      "c0241278f93ef02cd5191f03caf4e5f8e1292bf6";
  static constexpr char kWeightSha256[] =
      "901bb7278ba2f4f43076854e1e2757d7ae8c15f429b16b42e35cd729c75754af";
  static constexpr size_t kFileBytes = 83249856;
  static constexpr size_t kWeightBytes = 83224056;
  static constexpr size_t kHeaderBytes = 25792;
  static constexpr size_t kTensors = 231;
  static constexpr int kSampleRate = 16000;
  static constexpr int kMelBins = 80;
  static constexpr int kFft = 400;
  static constexpr int kHop = 160;
  static constexpr int kChannels = 1024;
  static constexpr int kEmbedding = 192;
};

struct SpeakerTensorContract {
  std::string name;
  std::string dtype;
  std::vector<int64_t> shape;
  size_t bytes = 0;
};
struct SpeakerTensorLayout {
  // Byte offsets in canonical name order. Scalar I64 training counters are
  // retained as original data; inference never treats them as float weights.
  std::array<size_t, SpeakerConfig::kTensors> offsets{};
};
std::vector<SpeakerTensorContract> SpeakerTensorContracts();
Status ValidateSpeakerConfig(const io::Json& config);
// Exact names/dtypes/shapes and contiguous canonical extents are required.
// Fails atomically, including cancellation. Enclosing file and weight digest
// checks belong to the asset owner, before any model allocation/execution.
Status ParseSpeakerTensorLayout(const io::Json& header, uint64_t data_bytes,
                                SpeakerTensorLayout* out,
                                std::function<bool()> cancelled = {});
}  // namespace q4t::audio
