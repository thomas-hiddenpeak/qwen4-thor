#include "q4t/audio/speaker_model.h"

#include <cublasLt.h>
#include <math_constants.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <mutex>
#include <string>

#include "q4t/quant/lt_cache.h"

namespace q4t::audio {
namespace {
constexpr int kThreads = 256;
__device__ float Sum(float value, float* scratch) {
  scratch[threadIdx.x] = value;
  __syncthreads();
  for (int n = blockDim.x / 2; n; n /= 2) {
    if (threadIdx.x < n) scratch[threadIdx.x] += scratch[threadIdx.x + n];
    __syncthreads();
  }
  return scratch[0];
}
__global__ void Columns(const float* input, float* output, int channels,
                        int frames, int kernel, int dilation) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= channels * kernel * frames) return;
  const int row = index / frames, time = index % frames;
  int at = time + (row % kernel - kernel / 2) * dilation;
  // The planned minimum length is greater than every reflect pad. No sample
  // outside this utterance can enter the convolution.
  if (at < 0) at = -at;
  if (at >= frames) at = 2 * frames - 2 - at;
  output[index] = input[(row / kernel) * frames + at];
}
__global__ void AffineOutput(float* output, const float* bias, int channels,
                             int frames, int activation) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= channels * frames) return;
  float value = __fadd_rn(output[index], bias[index / frames]);
  if (activation == 1) value = fmaxf(value, 0.f);
  if (activation == 2) value = 1.f / (1.f + expf(-value));
  output[index] = value;
}
__global__ void Normalize(float* output, SpeakerNormWeights weights,
                          int channels, int frames) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= channels * frames) return;
  const int channel = index / frames;
  const float centered = output[index] - weights.mean[channel];
  const float normalized = centered * rsqrtf(weights.variance[channel] + 1e-5f);
  output[index] = __fadd_rn(__fmul_rn(normalized, weights.weight[channel]),
                            weights.bias[channel]);
}
__global__ void Add(const float* a, const float* b, float* output, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) output[index] = __fadd_rn(a[index], b[index]);
}
__global__ void Squeeze(const float* input, float* output, int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    sum += input[channel * frames + t];
  const float mean = Sum(sum, scratch) / frames;
  if (threadIdx.x == 0) output[channel] = mean;
}
__global__ void ScaleResidual(const float* update, const float* gate,
                              float* hidden, int frames) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < 1024 * frames)
    hidden[index] = __fadd_rn(__fmul_rn(update[index], gate[index / frames]),
                              hidden[index]);
}
__global__ void GlobalContext(const float* input, float* context, int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    sum += input[channel * frames + t];
  const float mean = Sum(sum, scratch) / frames;
  __syncthreads();
  float squares = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const float delta = input[channel * frames + t] - mean;
    squares += __fmul_rn(delta, delta) / frames;
  }
  const float deviation = sqrtf(fmaxf(Sum(squares, scratch), 1e-12f));
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    context[channel * frames + t] = input[channel * frames + t];
    context[(3072 + channel) * frames + t] = mean;
    context[(6144 + channel) * frames + t] = deviation;
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
  scratch[threadIdx.x] = maximum;
  __syncthreads();
  for (int n = blockDim.x / 2; n; n /= 2) {
    if (threadIdx.x < n)
      scratch[threadIdx.x] =
          fmaxf(scratch[threadIdx.x], scratch[threadIdx.x + n]);
    __syncthreads();
  }
  maximum = scratch[0];
  __syncthreads();
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const int index = channel * frames + t;
    const float value = expf(scores[index] - maximum);
    scores[index] = value;
    sum += value;
  }
  const float total = Sum(sum, scratch);
  for (int t = threadIdx.x; t < frames; t += blockDim.x)
    scores[channel * frames + t] /= total;
}
__global__ void Pool(const float* input, const float* probability,
                     float* output, int frames) {
  __shared__ float scratch[kThreads];
  const int channel = blockIdx.x;
  float sum = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const int index = channel * frames + t;
    sum += __fmul_rn(input[index], probability[index]);
  }
  const float mean = Sum(sum, scratch);
  __syncthreads();
  float squares = 0;
  for (int t = threadIdx.x; t < frames; t += blockDim.x) {
    const int index = channel * frames + t;
    const float delta = input[index] - mean;
    squares += __fmul_rn(__fmul_rn(delta, delta), probability[index]);
  }
  const float deviation = sqrtf(fmaxf(Sum(squares, scratch), 1e-12f));
  if (threadIdx.x == 0) {
    output[channel] = mean;
    output[3072 + channel] = deviation;
  }
}
__global__ void CheckFinite(const float* values, int count, int* failed) {
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
  if (!handle) return Status::Fail("Speaker cuBLAS handle unavailable");
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
    return Status::Fail("Speaker FP32 matrix descriptor failed");
  const size_t bytes = SpeakerWorkspaceLayout::kGemmBytes;
  const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
  if (!ok(cublasLtMatmulPreferenceSetAttribute(
          plan.preference, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &bytes,
          sizeof(bytes))) ||
      !ok(cublasLtMatmulPreferenceSetAttribute(
          plan.preference, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK,
          &reduction, sizeof(reduction))))
    return Status::Fail("Speaker matrix workspace descriptor failed");
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
      return Status::Fail("Speaker matrix alignment descriptor failed");
  cublasLtMatmulHeuristicResult_t algorithm{};
  int count = 0;
  if (!ok(cublasLtMatmulAlgoGetHeuristic(handle, plan.operation, plan.a, plan.b,
                                         plan.c, plan.c, plan.preference, 1,
                                         &algorithm, &count)) ||
      count != 1 || algorithm.state != CUBLAS_STATUS_SUCCESS)
    return Status::Fail("Speaker FP32 matrix algorithm unavailable");
  const float alpha = 1, beta = 0;
  const auto result =
      cublasLtMatmul(handle, plan.operation, &alpha, input, plan.a, weight,
                     plan.b, &beta, output, plan.c, output, plan.c,
                     &algorithm.algo, workspace, bytes, stream);
  return ok(result) ? Status{}
                    : Status::Fail("Speaker matrix failed: " +
                                   std::to_string(static_cast<int>(result)));
}

Status Conv(const float* input, SpeakerAffineWeights weights, float* output,
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
  const int count = out * frames;
  AffineOutput<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      output, weights.bias, out, frames, activation);
  return {};
}
Status Tdnn(const float* input, SpeakerTdnnWeights weights, float* output,
            int frames, int in, int out, int kernel, int dilation,
            float* columns, void* gemm, cudaStream_t stream) {
  auto status = Conv(input, weights.affine, output, frames, in, out, kernel,
                     dilation, 1, columns, gemm, stream);
  if (!status) return status;
  const int count = out * frames;
  Normalize<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      output, weights.norm, out, frames);
  return {};
}
SpeakerComputeResult Complete(Status status, cudaStream_t stream,
                              cudaError_t submitted = cudaSuccess) {
  const auto launch = cudaPeekAtLastError();
  const auto completion = cudaStreamSynchronize(stream);
  const auto error = completion != cudaSuccess ? completion
                     : launch != cudaSuccess   ? launch
                                               : submitted;
  if (error != cudaSuccess)
    return {Status::Fail(std::string("Speaker CUDA failure: ") +
                         cudaGetErrorString(error)),
            error};
  return {std::move(status), cudaSuccess};
}
bool Valid(const SpeakerDeviceWeights& weights) {
  const auto affine = [](SpeakerAffineWeights w) { return w.weight && w.bias; };
  const auto norm = [](SpeakerNormWeights w) {
    return w.weight && w.bias && w.mean && w.variance;
  };
  const auto tdnn = [&](SpeakerTdnnWeights w) {
    return affine(w.affine) && norm(w.norm);
  };
  if (!tdnn(weights.input) || !tdnn(weights.aggregate) ||
      !tdnn(weights.attention) || !affine(weights.attention_output) ||
      !affine(weights.output) || !norm(weights.pool_norm))
    return false;
  for (const auto& layer : weights.layers) {
    if (!tdnn(layer.before) || !tdnn(layer.after) || !affine(layer.se_first) ||
        !affine(layer.se_second))
      return false;
    for (const auto& branch : layer.branches)
      if (!tdnn(branch)) return false;
  }
  return true;
}
}  // namespace

SpeakerComputeResult SpeakerRun::Begin(const SpeakerDeviceWeights& weights,
                                       std::span<const float> mel, int frames,
                                       void* workspace, size_t bytes,
                                       cudaStream_t stream) {
  if (started_ || failed_ || finished_ || !Valid(weights) || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256 || frames < 5)
    return {Status::Fail("Speaker input or execution state invalid")};
  SpeakerWorkspaceLayout layout;
  auto status = PlanSpeakerWorkspace(frames, SpeakerWorkspaceLayout::kMaxFrames,
                                     bytes, &layout);
  if (!status) return {status};
  if (mel.size() != static_cast<size_t>(80 * frames) ||
      reinterpret_cast<uintptr_t>(workspace) >
          std::numeric_limits<uintptr_t>::max() - layout.bytes)
    return {Status::Fail("Speaker input or workspace extent invalid")};
  if (!std::all_of(mel.begin(), mel.end(),
                   [](float value) { return std::isfinite(value); }))
    return {Status::Fail("Speaker input is nonfinite")};
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

SpeakerComputeResult SpeakerRun::NextStage(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_ >= 7)
    return {Status::Fail("Speaker stage state invalid")};
  const int frames = layout_.frames;
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace_ + offset);
  };
  auto* columns = at(layout_.columns);
  auto* gemm = workspace_ + layout_.gemm;
  auto* hidden = at(layout_.hidden);
  auto* aggregate = at(layout_.aggregate);
  auto* pool = at(layout_.pool);
  const float* checked = nullptr;
  int count = 0;
  Status status;
  cudaError_t submitted = cudaSuccess;
  const auto copy = [&](float* to, const float* from, size_t elements) {
    const auto error = cudaMemcpyAsync(to, from, elements * sizeof(float),
                                       cudaMemcpyDeviceToDevice, stream);
    if (error != cudaSuccess && submitted == cudaSuccess) submitted = error;
    if (error != cudaSuccess)
      status = Status::Fail("Speaker stage copy failed");
  };
  if (completed_ == 0) {
    status = Tdnn(at(layout_.mel), weights_->input, hidden, frames, 80, 1024, 5,
                  1, columns, gemm, stream);
    checked = hidden;
    count = 1024 * frames;
  } else if (completed_ <= 3) {
    const auto& layer = weights_->layers[completed_ - 1];
    auto* temporary = at(layout_.temporary);
    auto* res2 = at(layout_.res2);
    auto* update = at(layout_.update);
    status = Tdnn(hidden, layer.before, temporary, frames, 1024, 1024, 1, 1,
                  columns, gemm, stream);
    if (status) copy(res2, temporary, 128 * frames);
    for (int branch = 0; branch < 7 && status; ++branch) {
      const float* input = temporary + (branch + 1) * 128 * frames;
      if (branch > 0) {
        const int elements = 128 * frames;
        Add<<<(elements + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
            input, res2 + branch * elements, update, elements);
        input = update;
      }
      status = Tdnn(input, layer.branches[branch],
                    res2 + (branch + 1) * 128 * frames, frames, 128, 128, 3,
                    completed_ + 1, columns, gemm, stream);
    }
    if (status)
      status = Tdnn(res2, layer.after, update, frames, 1024, 1024, 1, 1,
                    columns, gemm, stream);
    if (status) {
      Squeeze<<<1024, kThreads, 0, stream>>>(update, at(layout_.squeeze),
                                             frames);
      status =
          Conv(at(layout_.squeeze), layer.se_first, at(layout_.squeeze_hidden),
               1, 1024, 128, 1, 1, 1, columns, gemm, stream);
    }
    if (status)
      status = Conv(at(layout_.squeeze_hidden), layer.se_second,
                    at(layout_.squeeze_gate), 1, 128, 1024, 1, 1, 2, columns,
                    gemm, stream);
    if (status) {
      const int elements = 1024 * frames;
      ScaleResidual<<<(elements + kThreads - 1) / kThreads, kThreads, 0,
                      stream>>>(update, at(layout_.squeeze_gate), hidden,
                                frames);
      copy(at(layout_.history) + (completed_ - 1) * elements, hidden, elements);
    }
    checked = hidden;
    count = 1024 * frames;
  } else if (completed_ == 4) {
    status = Tdnn(at(layout_.history), weights_->aggregate, aggregate, frames,
                  3072, 3072, 1, 1, columns, gemm, stream);
    checked = aggregate;
    count = 3072 * frames;
  } else if (completed_ == 5) {
    auto* context = at(layout_.context);
    auto* attention_hidden = at(layout_.attention_hidden);
    auto* attention = at(layout_.attention);
    GlobalContext<<<3072, kThreads, 0, stream>>>(aggregate, context, frames);
    status = Tdnn(context, weights_->attention, attention_hidden, frames, 9216,
                  128, 1, 1, columns, gemm, stream);
    if (status) {
      const int elements = 128 * frames;
      Tanh<<<(elements + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          attention_hidden, elements);
      status = Conv(attention_hidden, weights_->attention_output, attention,
                    frames, 128, 3072, 1, 1, 0, columns, gemm, stream);
    }
    if (status) {
      Softmax<<<3072, kThreads, 0, stream>>>(attention, frames);
      Pool<<<3072, kThreads, 0, stream>>>(aggregate, attention, pool, frames);
      Normalize<<<(6144 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          pool, weights_->pool_norm, 6144, 1);
    }
    checked = pool;
    count = 6144;
  } else {
    status = Conv(pool, weights_->output, at(layout_.output), 1, 6144, 192, 1,
                  1, 0, columns, gemm, stream);
    checked = at(layout_.output);
    count = 192;
  }
  int invalid = 0;
  if (status) {
    auto* flag = reinterpret_cast<int*>(workspace_ + layout_.finite_flag);
    submitted = cudaMemsetAsync(flag, 0, sizeof(int), stream);
    if (submitted == cudaSuccess) {
      CheckFinite<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          checked, count, flag);
      submitted = cudaMemcpyAsync(&invalid, flag, sizeof(int),
                                  cudaMemcpyDeviceToHost, stream);
    }
  }
  auto result = Complete(std::move(status), stream, submitted);
  if (result.ok() && invalid)
    result.status = Status::Fail("Speaker stage produced nonfinite values");
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  ++completed_;
  return {};
}

SpeakerComputeResult SpeakerRun::Finish(std::span<float> embedding,
                                        cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_ != 7 ||
      embedding.size() != 192)
    return {Status::Fail("Speaker output state or shape invalid")};
  std::array<float, 192> result;
  auto status =
      Complete({}, stream,
               cudaMemcpyAsync(result.data(), workspace_ + layout_.output,
                               sizeof(result), cudaMemcpyDeviceToHost, stream));
  if (status.ok() && !std::all_of(result.begin(), result.end(),
                                  [](float x) { return std::isfinite(x); }))
    status.status = Status::Fail("Speaker embedding is nonfinite");
  if (!status.ok()) {
    failed_ = true;
    return status;
  }
  std::copy(result.begin(), result.end(), embedding.begin());
  finished_ = true;
  return {};
}
}  // namespace q4t::audio
