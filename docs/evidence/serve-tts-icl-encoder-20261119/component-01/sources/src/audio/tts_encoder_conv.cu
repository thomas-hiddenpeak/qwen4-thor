#include "q4t/audio/tts_encoder.h"

#include <algorithm>
#include <limits>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

__global__ void Columns(const float* input, float* columns, int count,
                        int first, int rows, int channels, int kernel,
                        int stride, int left) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int k = i % kernel, channel = i / kernel % channels;
  const int row = first + i / (channels * kernel);
  const int source = row * stride - left + k;
  columns[i] = source < 0 || source >= rows
                   ? 0
                   : input[static_cast<size_t>(source) * channels + channel];
}

__global__ void Bias(float* output, const float* bias, int count,
                     int channels) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) output[i] = __fadd_rn(output[i], bias[i % channels]);
}

bool Disjoint(std::span<const float> input, std::span<float> output,
              std::span<float> columns, std::span<uint8_t> gemm) {
  const std::array<std::pair<uintptr_t, size_t>, 4> buffers{
      {{reinterpret_cast<uintptr_t>(input.data()), input.size_bytes()},
       {reinterpret_cast<uintptr_t>(output.data()), output.size_bytes()},
       {reinterpret_cast<uintptr_t>(columns.data()), columns.size_bytes()},
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

TtsComputeResult TtsEncoderConvRun::Begin(const TtsCodecConvWeights& weights,
                                          size_t rows,
                                          std::span<const float> input,
                                          std::span<float> output,
                                          std::span<float> columns,
                                          std::span<uint8_t> gemm) {
  size_t output_rows = 0, left = 0;
  if (initialized_ || failed_ || !weights.weight ||
      !PlanTtsEncoderConvolution(weights, rows, &output_rows, &left) ||
      input.size() != rows * weights.input ||
      output.size() != output_rows * weights.output ||
      columns.size_bytes() != TtsCodecConfig::kColumnBytes ||
      gemm.size_bytes() != TtsDenseLayout::kGemmBytes ||
      !Disjoint(input, output, columns, gemm))
    return Invalid("TTS encoder convolution state or buffers invalid");
  weights_ = weights;
  rows_ = rows;
  output_rows_ = output_rows;
  left_ = left;
  input_ = input.data();
  output_ = output.data();
  columns_ = columns.data();
  gemm_ = gemm.data();
  initialized_ = true;
  return {};
}

TtsComputeResult TtsEncoderConvRun::Advance(cudaStream_t stream) {
  if (!initialized_ || failed_ || done_)
    return Invalid("TTS encoder convolution advance out of order");
  const auto& w = weights_;
  const int inner = w.input * w.kernel;
  const size_t limit = std::min(
      size_t{4096}, TtsCodecConfig::kColumnBytes / sizeof(float) / inner);
  const int count = static_cast<int>(std::min(limit, output_rows_ - row_));
  Columns<<<(count * inner + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      input_, columns_, count * inner, static_cast<int>(row_),
      static_cast<int>(rows_), w.input, w.kernel, w.stride,
      static_cast<int>(left_));
  auto status = Matmul(columns_, w.weight, output_ + row_ * w.output, count,
                       w.output, inner, inner, CUDA_R_32F, CUDA_R_32F, gemm_,
                       TtsDenseLayout::kGemmBytes, stream);
  if (status && w.bias)
    Bias<<<(count * w.output + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        output_ + row_ * w.output, w.bias, count * w.output, w.output);
  auto result = Complete(status, stream);
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  row_ += count;
  done_ = row_ == output_rows_;
  return {};
}

}  // namespace q4t::audio
