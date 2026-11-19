#include "q4t/audio/tts_codec.h"

#include <algorithm>
#include <array>
#include <limits>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

__global__ void Columns(const float* input, float* columns, int count,
                        int first, int channels, int kernel, int dilation) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int k = i % kernel, channel = i / kernel % channels;
  const int row = first + i / (channels * kernel);
  const int source = row - (kernel - 1 - k) * dilation;
  columns[i] =
      source < 0 ? 0 : input[static_cast<size_t>(source) * channels + channel];
}

__global__ void Bias(float* output, const float* bias, size_t count,
                     int channels) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i < count) output[i] = __fadd_rn(output[i], bias[i % channels]);
}

__global__ void Depthwise(const float* input, const float* weights,
                          const float* bias, float* output, int count,
                          int first, int channels, int kernel, int dilation) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int row = first + i / channels, channel = i % channels;
  float sum = 0;
  for (int k = 0; k < kernel; ++k) {
    const int source = row - (kernel - 1 - k) * dilation;
    if (source >= 0)
      sum = fmaf(input[static_cast<size_t>(source) * channels + channel],
                 weights[channel * kernel + k], sum);
  }
  output[static_cast<size_t>(row) * channels + channel] =
      __fadd_rn(sum, bias[channel]);
}

__global__ void TransposeWeights(const float* source, float* packed, int input,
                                 int output, int kernel, int k) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < input * output)
    packed[i] = source[(i % input * output + i / input) * kernel + k];
}

__global__ void ScatterTranspose(const float* tile, float* output, int count,
                                 int first, int channels, int stride,
                                 int kernel, int output_rows) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int row = (first + i / channels) * stride + kernel;
  if (row < output_rows) {
    const size_t index = static_cast<size_t>(row) * channels + i % channels;
    // For a fixed kernel position every thread writes a unique output. The
    // kernel positions execute sequentially on the caller's stream.
    output[index] = __fadd_rn(output[index], tile[i]);
  }
}

bool Disjoint(std::span<const float> input, std::span<float> output,
              std::span<float> columns, std::span<float> transpose,
              std::span<uint8_t> gemm) {
  const std::array<std::pair<uintptr_t, size_t>, 5> buffers{
      {{reinterpret_cast<uintptr_t>(input.data()), input.size_bytes()},
       {reinterpret_cast<uintptr_t>(output.data()), output.size_bytes()},
       {reinterpret_cast<uintptr_t>(columns.data()), columns.size_bytes()},
       {reinterpret_cast<uintptr_t>(transpose.data()), transpose.size_bytes()},
       {reinterpret_cast<uintptr_t>(gemm.data()), gemm.size_bytes()}}};
  for (size_t i = 0; i < buffers.size(); ++i) {
    const auto [address, bytes] = buffers[i];
    if (!address || address % alignof(float) ||
        address > std::numeric_limits<uintptr_t>::max() - bytes)
      return false;
    for (size_t j = 0; j < i; ++j)
      if (address < buffers[j].first + buffers[j].second &&
          buffers[j].first < address + bytes)
        return false;
  }
  return true;
}
}  // namespace

TtsComputeResult TtsCodecConvRun::Begin(
    const TtsCodecConvWeights& weights, size_t rows,
    std::span<const float> input, std::span<float> output,
    std::span<float> columns, std::span<float> transpose_weights,
    std::span<uint8_t> gemm, cudaStream_t stream) {
  if (initialized_ || failed_ || !weights.weight || !weights.bias ||
      weights.input < 1 || weights.input > 1536 || weights.output < 1 ||
      weights.output > 1536 || weights.kernel < 1 || weights.kernel > 16 ||
      weights.dilation < 1 || weights.dilation > 9 || weights.stride < 1 ||
      weights.stride > 8 || rows == 0 ||
      rows > TtsCodecConfig::kMaxInputFrames * TtsConfig::kSamplesPerFrame ||
      (weights.groups != 1 &&
       (weights.groups != weights.input || weights.input != weights.output)) ||
      (weights.transposed && (weights.dilation != 1 || weights.groups != 1 ||
                              weights.kernel < weights.stride)) ||
      (!weights.transposed && weights.stride != 1))
    return Invalid("TTS codec convolution shape or state invalid");
  const size_t output_rows = rows * (weights.transposed ? weights.stride : 1);
  if (output_rows >
          TtsCodecConfig::kMaxInputFrames * TtsConfig::kSamplesPerFrame ||
      input.size() != rows * weights.input ||
      output.size() != output_rows * weights.output ||
      columns.size_bytes() != TtsCodecConfig::kColumnBytes ||
      transpose_weights.size_bytes() != TtsCodecConfig::kTransposeWeightBytes ||
      gemm.size_bytes() != TtsDenseLayout::kGemmBytes ||
      static_cast<size_t>(weights.input) * weights.output * sizeof(float) >
          transpose_weights.size_bytes() ||
      !Disjoint(input, output, columns, transpose_weights, gemm))
    return Invalid("TTS codec convolution buffers invalid");
  weights_ = weights;
  input_ = input.data();
  output_ = output.data();
  columns_ = columns.data();
  transpose_weights_ = transpose_weights.data();
  gemm_ = gemm.data();
  rows_ = rows;
  output_rows_ = output_rows;
  if (weights.transposed) {
    auto result = Complete(
        {}, stream, cudaMemsetAsync(output_, 0, output.size_bytes(), stream));
    if (!result.ok()) {
      failed_ = true;
      return result;
    }
  }
  initialized_ = true;
  return {};
}

TtsComputeResult TtsCodecConvRun::Advance(cudaStream_t stream) {
  if (!initialized_ || failed_ || done_)
    return Invalid("TTS codec convolution advance out of order");
  const auto& w = weights_;
  const int inner = w.input * w.kernel;
  const size_t tile_limit =
      std::min(size_t{4096}, TtsCodecConfig::kColumnBytes / sizeof(float) /
                                 (w.transposed ? w.output : inner));
  const int count = static_cast<int>(std::min(tile_limit, rows_ - row_));
  const int first = static_cast<int>(row_);
  Status status;
  if (w.transposed) {
    if (row_ == 0)
      TransposeWeights<<<(w.input * w.output + kThreads - 1) / kThreads,
                         kThreads, 0, stream>>>(
          w.weight, transpose_weights_, w.input, w.output, w.kernel, kernel_);
    status = Matmul(input_ + row_ * w.input, transpose_weights_, columns_,
                    count, w.output, w.input, w.input, CUDA_R_32F, CUDA_R_32F,
                    gemm_, TtsDenseLayout::kGemmBytes, stream);
    if (status)
      ScatterTranspose<<<(count * w.output + kThreads - 1) / kThreads, kThreads,
                         0, stream>>>(columns_, output_, count * w.output,
                                      first, w.output, w.stride, kernel_,
                                      static_cast<int>(output_rows_));
    if (status && row_ + count == rows_ && kernel_ + 1 == w.kernel) {
      const size_t elements = output_rows_ * w.output;
      Bias<<<(elements + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          output_, w.bias, elements, w.output);
    }
  } else if (w.groups != 1) {
    Depthwise<<<(count * w.output + kThreads - 1) / kThreads, kThreads, 0,
                stream>>>(input_, w.weight, w.bias, output_, count * w.output,
                          first, w.output, w.kernel, w.dilation);
  } else {
    Columns<<<(count * inner + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        input_, columns_, count * inner, first, w.input, w.kernel, w.dilation);
    status = Matmul(columns_, w.weight, output_ + row_ * w.output, count,
                    w.output, inner, inner, CUDA_R_32F, CUDA_R_32F, gemm_,
                    TtsDenseLayout::kGemmBytes, stream);
    if (status)
      Bias<<<(count * w.output + kThreads - 1) / kThreads, kThreads, 0,
             stream>>>(output_ + row_ * w.output, w.bias, count * w.output,
                       w.output);
  }
  auto result = Complete(status, stream);
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  row_ += count;
  if (row_ == rows_) {
    if (w.transposed && ++kernel_ < w.kernel)
      row_ = 0;
    else
      done_ = true;
  }
  return {};
}

}  // namespace q4t::audio
