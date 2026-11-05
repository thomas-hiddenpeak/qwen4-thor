// Experimental full-vocabulary W4A16 draft head, derived from loaded BF16.
#pragma once

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>

#include "q4t/status.h"

namespace q4t::mtp {

inline constexpr int kQuantizedHeadVocab = 248320;
inline constexpr int kQuantizedHeadHidden = 2560;
inline constexpr size_t kQuantizedHeadPackedBytes =
    size_t{kQuantizedHeadVocab} * kQuantizedHeadHidden / 2;
inline constexpr size_t kQuantizedHeadScaleBytes =
    size_t{kQuantizedHeadVocab} * kQuantizedHeadHidden / 16;
inline constexpr size_t kQuantizedHeadBytes =
    kQuantizedHeadPackedBytes + kQuantizedHeadScaleBytes;
inline constexpr size_t kQuantizedHeadTemporaryBytes = 1024 * sizeof(float);

// Same explicit ownership convention as MtpModel. Build drains its stream
// before returning, including errors. Forward borrows all buffers until the
// caller's stream completes. Free is called only after that completion.
struct QuantizedHead {
  uint8_t* packed = nullptr;
  uint8_t* scales = nullptr;
  float global_scale = 0.0f;  // Dequantization multiplier, not its reciprocal.
  void Free();
};

Status BuildQuantizedHead(const uint16_t* source, QuantizedHead* output,
                          cudaStream_t stream);
Status QuantizedHeadForward(const QuantizedHead& head, const uint16_t* input,
                            uint16_t* output, int rows, cudaStream_t stream);

}  // namespace q4t::mtp
