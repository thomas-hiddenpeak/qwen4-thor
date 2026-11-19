#include "q4t/audio/tts_speaker_model.h"

#include <math_constants.h>

#include <cmath>
#include <limits>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using tts_detail::Complete;
using tts_detail::kThreads;
using tts_detail::Reduce;

__global__ void Columns(const float* input, float* output, int channels,
                        int frames, int kernel, int dilation) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= channels * kernel * frames) return;
  const int row = index / frames, time = index % frames;
  int at = time + (row % kernel - kernel / 2) * dilation;
  if (at < 0) at = -at;
  if (at >= frames) at = 2 * frames - 2 - at;
  output[index] = input[(row / kernel) * frames + at];
}
__global__ void Affine(float* output, const float* bias, int channels,
                       int frames, int activation) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= channels * frames) return;
  float value = __fadd_rn(output[index], bias[index / frames]);
  if (activation == 1) value = fmaxf(value, 0.f);
  if (activation == 2) value = 1.f / (1.f + expf(-value));
  output[index] = value;
}
__global__ void Add(const float* a, const float* b, float* out, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) out[index] = __fadd_rn(a[index], b[index]);
}
__global__ void Squeeze(const float* input, float* output, int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    sum += input[channel * frames + t];
  const float mean = Reduce<false>(sum, scratch) / frames;
  if (threadIdx.x == 0) output[channel] = mean;
}
__global__ void ScaleResidual(const float* update, const float* gate,
                              float* hidden, int frames) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < 512 * frames)
    hidden[index] = __fadd_rn(__fmul_rn(update[index], gate[index / frames]),
                              hidden[index]);
}
__global__ void Context(const float* input, float* context, int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    sum += input[channel * frames + t];
  const float mean = Reduce<false>(sum, scratch) / frames;
  __syncthreads();
  float squares = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const float delta = input[channel * frames + t] - mean;
    squares += __fmul_rn(delta, delta) / frames;
  }
  const float deviation = sqrtf(fmaxf(Reduce<false>(squares, scratch), 1e-12f));
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    context[channel * frames + t] = input[channel * frames + t];
    context[(1536 + channel) * frames + t] = mean;
    context[(3072 + channel) * frames + t] = deviation;
  }
}
__global__ void Tanh(float* values, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) values[index] = tanhf(values[index]);
}
__global__ void Softmax(float* scores, int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float maximum = -CUDART_INF_F;
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    maximum = fmaxf(maximum, scores[channel * frames + t]);
  maximum = Reduce<true>(maximum, scratch);
  __syncthreads();
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const int index = channel * frames + t;
    const float value = expf(scores[index] - maximum);
    scores[index] = value;
    sum += value;
  }
  const float total = Reduce<false>(sum, scratch);
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    scores[channel * frames + t] /= total;
}
__global__ void Pool(const float* input, const float* scores, float* output,
                     int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const int index = channel * frames + t;
    sum += __fmul_rn(input[index], scores[index]);
  }
  const float mean = Reduce<false>(sum, scratch);
  __syncthreads();
  float squares = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const int index = channel * frames + t;
    const float delta = input[index] - mean;
    squares += __fmul_rn(__fmul_rn(delta, delta), scores[index]);
  }
  const float deviation = sqrtf(fmaxf(Reduce<false>(squares, scratch), 1e-12f));
  if (threadIdx.x == 0) {
    output[channel] = mean;
    output[1536 + channel] = deviation;
  }
}
__global__ void Finite(const float* values, int count, int* failed) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count && !isfinite(values[index])) atomicExch(failed, 1);
}

struct MatmulPlan {
  cublasLtMatmulDesc_t operation = nullptr;
  cublasLtMatrixLayout_t a = nullptr, b = nullptr, c = nullptr;
  cublasLtMatmulPreference_t preference = nullptr;
  ~MatmulPlan() {
    if (preference) cublasLtMatmulPreferenceDestroy(preference);
    if (c) cublasLtMatrixLayoutDestroy(c);
    if (b) cublasLtMatrixLayoutDestroy(b);
    if (a) cublasLtMatrixLayoutDestroy(a);
    if (operation) cublasLtMatmulDescDestroy(operation);
  }
};

Status Multiply(const float* input, const float* weight, float* output,
                int frames, int in, int out, void* workspace,
                cudaStream_t stream) {
  std::lock_guard<std::mutex> lock(quant::LtCacheMutex());
  auto handle = quant::GlobalLtHandle();
  if (!handle) return Status::Fail("TTS speaker cuBLAS handle unavailable");
  MatmulPlan plan;
  const auto ok = [](cublasStatus_t status) {
    return status == CUBLAS_STATUS_SUCCESS;
  };
  // Channel-major [in, time] is column-major [time, in]. Original OIK
  // checkpoint rows become [in, out]. All operands/output remain FP32.
  if (!ok(cublasLtMatmulDescCreate(&plan.operation, CUBLAS_COMPUTE_32F_PEDANTIC,
                                   CUDA_R_32F)) ||
      !ok(cublasLtMatrixLayoutCreate(&plan.a, CUDA_R_32F, frames, in,
                                     frames)) ||
      !ok(cublasLtMatrixLayoutCreate(&plan.b, CUDA_R_32F, in, out, in)) ||
      !ok(cublasLtMatrixLayoutCreate(&plan.c, CUDA_R_32F, frames, out,
                                     frames)) ||
      !ok(cublasLtMatmulPreferenceCreate(&plan.preference)))
    return Status::Fail("TTS speaker FP32 matrix descriptor failed");
  const size_t bytes = TtsSpeakerWorkspace::kGemmBytes;
  const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
  if (!ok(cublasLtMatmulPreferenceSetAttribute(
          plan.preference, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &bytes,
          sizeof(bytes))) ||
      !ok(cublasLtMatmulPreferenceSetAttribute(
          plan.preference, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK,
          &reduction, sizeof(reduction))))
    return Status::Fail("TTS speaker matrix workspace descriptor failed");
  const auto alignment = [](const void* ptr) {
    const auto address = reinterpret_cast<uintptr_t>(ptr);
    return static_cast<uint32_t>(std::min(uintptr_t{256}, address & -address));
  };
  const std::pair<cublasLtMatmulPreferenceAttributes_t, uint32_t> aligned[] = {
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_A_BYTES, alignment(input)},
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_B_BYTES, alignment(weight)},
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_C_BYTES, alignment(output)},
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_D_BYTES, alignment(output)}};
  for (const auto& [field, value] : aligned)
    if (!ok(cublasLtMatmulPreferenceSetAttribute(plan.preference, field, &value,
                                                 sizeof(value))))
      return Status::Fail("TTS speaker matrix alignment descriptor failed");
  cublasLtMatmulHeuristicResult_t algorithm{};
  int count = 0;
  if (!ok(cublasLtMatmulAlgoGetHeuristic(handle, plan.operation, plan.a, plan.b,
                                         plan.c, plan.c, plan.preference, 1,
                                         &algorithm, &count)) ||
      count != 1 || algorithm.state != CUBLAS_STATUS_SUCCESS)
    return Status::Fail("TTS speaker FP32 matrix algorithm unavailable");
  const float alpha = 1, beta = 0;
  const auto result =
      cublasLtMatmul(handle, plan.operation, &alpha, input, plan.a, weight,
                     plan.b, &beta, output, plan.c, output, plan.c,
                     &algorithm.algo, workspace, bytes, stream);
  return ok(result) ? Status{}
                    : Status::Fail("TTS speaker matrix failed: " +
                                   std::to_string(static_cast<int>(result)));
}

Status Conv(const float* input, TtsAffineWeights weights, float* output,
            int frames, int in, int out, int kernel, int dilation,
            int activation, float* columns, void* gemm, cudaStream_t stream) {
  const float* matrix = input;
  if (kernel > 1) {
    const int count = in * kernel * frames;
    Columns<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        input, columns, in, frames, kernel, dilation);
    matrix = columns;
  }
  auto status = Multiply(matrix, weights.weight, output, frames, in * kernel,
                         out, gemm, stream);
  if (!status) return status;
  const int count = frames * out;
  Affine<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      output, weights.bias, out, frames, activation);
  return {};
}
bool Valid(const TtsSpeakerDeviceWeights& weights) {
  const auto affine = [](TtsAffineWeights w) {
    return w.weight && w.bias &&
           reinterpret_cast<uintptr_t>(w.weight) % alignof(float) == 0 &&
           reinterpret_cast<uintptr_t>(w.bias) % alignof(float) == 0;
  };
  if (!affine(weights.input) || !affine(weights.aggregate) ||
      !affine(weights.attention) || !affine(weights.attention_output) ||
      !affine(weights.output))
    return false;
  for (const auto& layer : weights.layers) {
    if (!affine(layer.before) || !affine(layer.after) ||
        !affine(layer.squeeze_first) || !affine(layer.squeeze_second))
      return false;
    for (const auto& branch : layer.branches)
      if (!affine(branch)) return false;
  }
  return true;
}
}  // namespace

TtsComputeResult TtsSpeakerRun::Begin(const TtsSpeakerDeviceWeights& weights,
                                      std::span<const float> mel, size_t frames,
                                      void* workspace, size_t bytes,
                                      cudaStream_t stream) {
  if (started_ || failed_ || finished_ || !Valid(weights) || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256)
    return {Status::Fail("TTS speaker input or state invalid")};
  TtsSpeakerWorkspace layout;
  auto status = PlanTtsSpeakerWorkspace(frames, TtsSpeakerConfig::kMaxFrames,
                                        bytes, &layout);
  if (!status) return {status};
  if (mel.size() != 128 * frames ||
      reinterpret_cast<uintptr_t>(workspace) >
          std::numeric_limits<uintptr_t>::max() - layout.bytes ||
      !std::all_of(mel.begin(), mel.end(),
                   [](float x) { return std::isfinite(x); }))
    return {Status::Fail("TTS speaker input or workspace bounds invalid")};
  auto* base = static_cast<uint8_t*>(workspace);
  auto result =
      Complete({}, stream,
               cudaMemcpyAsync(base + layout.mel, mel.data(), mel.size_bytes(),
                               cudaMemcpyHostToDevice, stream));
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  weights_ = &weights;
  workspace_ = base;
  layout_ = layout;
  started_ = true;
  return {};
}

TtsComputeResult TtsSpeakerRun::NextStage(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_ >= kStages)
    return {Status::Fail("TTS speaker stage state invalid")};
  const int frames = layout_.frames;
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace_ + offset);
  };
  auto* hidden = at(layout_.hidden);
  auto* columns = at(layout_.columns);
  auto* gemm = workspace_ + layout_.gemm;
  auto* aggregate = at(layout_.aggregate);
  auto* pool = at(layout_.pool);
  const float* checked = nullptr;
  int count = 0;
  Status status;
  cudaError_t submitted = cudaSuccess;
  const auto copy = [&](float* to, const float* from, int elements) {
    const auto error = cudaMemcpyAsync(to, from, elements * sizeof(float),
                                       cudaMemcpyDeviceToDevice, stream);
    if (error != cudaSuccess) {
      if (submitted == cudaSuccess) submitted = error;
      status = Status::Fail("TTS speaker stage copy failed");
    }
  };
  if (completed_ == 0) {
    status = Conv(at(layout_.mel), weights_->input, hidden, frames, 128, 512, 5,
                  1, 1, columns, gemm, stream);
    checked = hidden;
    count = 512 * frames;
  } else if (completed_ <= 39) {
    const int block = (completed_ - 1) / 13;
    const int step = (completed_ - 1) % 13;
    const auto& layer = weights_->layers[block];
    auto* temporary = at(layout_.temporary);
    auto* res2 = at(layout_.res2);
    auto* update = at(layout_.update);
    if (step == 0) {
      status = Conv(hidden, layer.before, temporary, frames, 512, 512, 1, 1, 1,
                    columns, gemm, stream);
      if (status) copy(res2, temporary, 64 * frames);
      checked = temporary;
      count = 512 * frames;
    } else if (step <= 7) {
      const int branch = step - 1, elements = 64 * frames;
      const float* input = temporary + (branch + 1) * elements;
      if (branch > 0) {
        Add<<<(elements + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
            input, res2 + branch * elements, update, elements);
        input = update;
      }
      auto* output = res2 + (branch + 1) * elements;
      status = Conv(input, layer.branches[branch], output, frames, 64, 64, 3,
                    block + 2, 1, columns, gemm, stream);
      checked = output;
      count = elements;
    } else if (step == 8) {
      status = Conv(res2, layer.after, update, frames, 512, 512, 1, 1, 1,
                    columns, gemm, stream);
      checked = update;
      count = 512 * frames;
    } else if (step == 9) {
      Squeeze<<<512, kThreads, 0, stream>>>(update, at(layout_.squeeze),
                                            frames);
      checked = at(layout_.squeeze);
      count = 512;
    } else if (step == 10) {
      status = Conv(at(layout_.squeeze), layer.squeeze_first,
                    at(layout_.squeeze_hidden), 1, 512, 128, 1, 1, 1, columns,
                    gemm, stream);
      checked = at(layout_.squeeze_hidden);
      count = 128;
    } else if (step == 11) {
      status = Conv(at(layout_.squeeze_hidden), layer.squeeze_second,
                    at(layout_.squeeze_gate), 1, 128, 512, 1, 1, 2, columns,
                    gemm, stream);
      checked = at(layout_.squeeze_gate);
      count = 512;
    } else {
      const int elements = 512 * frames;
      ScaleResidual<<<(elements + kThreads - 1) / kThreads, kThreads, 0,
                      stream>>>(update, at(layout_.squeeze_gate), hidden,
                                frames);
      copy(at(layout_.history) + block * elements, hidden, elements);
      checked = hidden;
      count = elements;
    }
  } else if (completed_ == 40) {
    status = Conv(at(layout_.history), weights_->aggregate, aggregate, frames,
                  1536, 1536, 1, 1, 1, columns, gemm, stream);
    checked = aggregate;
    count = 1536 * frames;
  } else if (completed_ == 41) {
    Context<<<1536, kThreads, 0, stream>>>(aggregate, at(layout_.context),
                                           frames);
    checked = at(layout_.context);
    count = 4608 * frames;
  } else if (completed_ == 42) {
    status = Conv(at(layout_.context), weights_->attention,
                  at(layout_.attention_hidden), frames, 4608, 128, 1, 1, 1,
                  columns, gemm, stream);
    checked = at(layout_.attention_hidden);
    count = 128 * frames;
  } else if (completed_ == 43) {
    const int elements = 128 * frames;
    Tanh<<<(elements + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        at(layout_.attention_hidden), elements);
    status = Conv(at(layout_.attention_hidden), weights_->attention_output,
                  at(layout_.attention), frames, 128, 1536, 1, 1, 0, columns,
                  gemm, stream);
    checked = at(layout_.attention);
    count = 1536 * frames;
  } else if (completed_ == 44) {
    Softmax<<<1536, kThreads, 0, stream>>>(at(layout_.attention), frames);
    checked = at(layout_.attention);
    count = 1536 * frames;
  } else if (completed_ == 45) {
    Pool<<<1536, kThreads, 0, stream>>>(aggregate, at(layout_.attention), pool,
                                        frames);
    checked = pool;
    count = 3072;
  } else {
    status = Conv(pool, weights_->output, at(layout_.output), 1, 3072, 2048, 1,
                  1, 0, columns, gemm, stream);
    checked = at(layout_.output);
    count = 2048;
  }
  int invalid = 0;
  if (status) {
    auto* flag = reinterpret_cast<int*>(workspace_ + layout_.finite_flag);
    submitted = cudaMemsetAsync(flag, 0, sizeof(int), stream);
    if (submitted == cudaSuccess) {
      Finite<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          checked, count, flag);
      submitted = cudaMemcpyAsync(&invalid, flag, sizeof(int),
                                  cudaMemcpyDeviceToHost, stream);
    }
  }
  auto result = Complete(std::move(status), stream, submitted);
  if (result.ok() && invalid)
    result.status = Status::Fail("TTS speaker stage produced nonfinite values");
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  ++completed_;
  return {};
}

TtsComputeResult TtsSpeakerRun::Finish(std::span<float> embedding,
                                       cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_ != kStages ||
      embedding.size() != 2048)
    return {Status::Fail("TTS speaker output state or shape invalid")};
  std::array<float, 2048> result;
  auto status =
      Complete({}, stream,
               cudaMemcpyAsync(result.data(), workspace_ + layout_.output,
                               sizeof(result), cudaMemcpyDeviceToHost, stream));
  if (status.ok() && !std::all_of(result.begin(), result.end(),
                                  [](float x) { return std::isfinite(x); }))
    status.status = Status::Fail("TTS speaker embedding is nonfinite");
  if (!status.ok()) {
    failed_ = true;
    return status;
  }
  std::copy(result.begin(), result.end(), embedding.begin());
  finished_ = true;
  return {};
}
}  // namespace q4t::audio
