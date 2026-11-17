// Native Silero VAD (fixed 16 kHz model). No runtime/library or GPU dependency.
// Structure derived from Silero's MIT-licensed tinygrad model; see
// third_party/licenses/silero-vad/README.md.
#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <span>
#include <vector>

#include "q4t/io/json.h"

namespace q4t::audio {
inline constexpr size_t kVadFrameSamples = 512;
inline constexpr size_t kVadContextSamples = 64;
inline constexpr size_t kVadMaxSamples = 600ULL * 16000;
inline constexpr size_t kVadWeightBytes = 1238532;
inline constexpr size_t kVadFileBytes = 1239748;
inline constexpr char kVadWeightSha256[] =
    "c59271c284ae9c8335d795d60e0bfdb71aaaceec578d9bd9ffc1b8153c319ea1";
inline constexpr char kVadRevision[] =
    "066409f3d9db45a30d21dfcbf9147826ffdd7052";

struct VadTensorLayout {
  std::array<size_t, 15> offsets{};
};
Status ParseVadTensorLayout(const io::Json& header, size_t data_bytes,
                            VadTensorLayout* out);

struct VadState {
  std::array<float, kVadContextSamples> context{};
  std::array<float, 128> hidden{}, cell{};
};
struct VadFrame {
  size_t begin_sample = 0, end_sample = 0;
  float speech_probability = 0;
};

// Immutable weights after Load. Request-local state; concurrent requests do
// not share recurrent state. All failed operations preserve caller outputs.
class SileroVad {
 public:
  using Cancelled = std::function<bool()>;
  Status Load(std::span<const uint8_t> file, Cancelled cancelled = {});
  bool Ready() const {
    return weights_.size() * sizeof(float) == kVadWeightBytes;
  }
  Status Step(std::span<const float> samples, VadState* state,
              float* probability, Cancelled cancelled = {}) const;
  // Only the final short frame is zero-padded. Returned sample boundaries
  // exclude padding. Frame probabilities are not a whole-clip probability.
  Status Score(std::span<const float> pcm, size_t max_samples,
               std::vector<VadFrame>* out, Cancelled cancelled = {}) const;

 private:
  VadTensorLayout layout_;
  std::vector<float> weights_;
};
}  // namespace q4t::audio
