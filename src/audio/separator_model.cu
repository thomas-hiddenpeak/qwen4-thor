#include "q4t/audio/separator_model.h"

#include <cublasLt.h>
#include <math_constants.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <mutex>
#include <string>
#include <vector>

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
__global__ void Encode(const float* pcm, const float* weights, float* output,
                       int frames) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= frames * 256) return;
  const int frame = index / 256, channel = index % 256;
  float sum = 0;
  for (int k = 0; k < 16; ++k)
    sum = fmaf(pcm[frame * 8 + k], weights[channel * 16 + k], sum);
  output[index] = fmaxf(sum, 0.f);
}
__global__ void PartialStatistics(const float* input, float* partial,
                                  const float* statistics, int count,
                                  bool variance) {
  __shared__ float scratch[kThreads];
  float sum = 0;
  for (int i = blockIdx.x * 1024 + threadIdx.x;
       i < count && i < (blockIdx.x + 1) * 1024; i += blockDim.x) {
    const float value = input[i];
    const float delta = variance ? value - statistics[0] : value;
    sum += variance ? __fmul_rn(delta, delta) : value;
  }
  sum = Sum(sum, scratch);
  if (threadIdx.x == 0) partial[blockIdx.x] = sum;
}
__global__ void ReduceStatistics(const float* partial, float* statistics,
                                 int blocks, int count, bool variance) {
  __shared__ float scratch[kThreads];
  float sum = 0;
  for (int i = threadIdx.x; i < blocks; i += blockDim.x) sum += partial[i];
  sum = Sum(sum, scratch) / count;
  if (threadIdx.x == 0) statistics[variance ? 1 : 0] = sum;
}
void GroupStatistics(const float* input, float* partial, float* statistics,
                     int count, cudaStream_t stream) {
  const int blocks = (count + 1023) / 1024;
  for (bool variance : {false, true}) {
    PartialStatistics<<<blocks, kThreads, 0, stream>>>(
        input, partial, statistics, count, variance);
    ReduceStatistics<<<1, kThreads, 0, stream>>>(partial, statistics, blocks,
                                                 count, variance);
  }
}
__global__ void GroupNormalize(const float* input, float* output,
                               SeparatorAffineWeights weights,
                               const float* stats, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= count) return;
  const int c = index % 256;
  const float value = (input[index] - stats[0]) * rsqrtf(stats[1] + 1e-8f);
  output[index] =
      __fadd_rn(__fmul_rn(value, weights.weight[c]), weights.bias[c]);
}
__global__ void Split(const float* input, float* output, int frames,
                      int segments) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= 250 * segments * 256) return;
  const int c = index % 256, s = index / 256 % segments;
  const int k = index / (256 * segments), t = s * 125 + k - 125;
  output[index] = t >= 0 && t < frames ? input[t * 256 + c] : 0.f;
}
__global__ void PreparePath(const float* input, const float* position,
                            float* output, int segments, bool intra) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= 250 * segments * 256) return;
  const int c = index % 256, token = index / 256;
  const int time = intra ? token % 250 : token % segments;
  const int k = intra ? time : token / segments;
  const int s = intra ? token / 250 : time;
  output[index] =
      __fadd_rn(input[(k * segments + s) * 256 + c], position[time * 256 + c]);
}
__global__ void LayerNormalize(const float* input, float* output,
                               SeparatorAffineWeights weights) {
  __shared__ float scratch[kThreads];
  const int c = threadIdx.x, index = blockIdx.x * 256 + c;
  const float value = input[index];
  const float mean = Sum(value, scratch) / 256;
  __syncthreads();
  const float delta = value - mean;
  const float variance = Sum(__fmul_rn(delta, delta), scratch) / 256;
  output[index] =
      __fadd_rn(__fmul_rn(delta * rsqrtf(variance + 1e-6f), weights.weight[c]),
                weights.bias[c]);
}
__global__ void Affine(float* output, const float* bias, int count,
                       int channels, int activation) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= count) return;
  float value = output[index];
  if (bias) value = __fadd_rn(value, bias[index % channels]);
  if (activation == 1) value = fmaxf(value, 0.f);
  if (activation == 2) value = tanhf(value);
  if (activation == 3) value = 1.f / (1.f + expf(-value));
  output[index] = value;
}
__global__ void Scores(const float* qkv, float* scores, int length,
                       int batches) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= batches * 8 * length * length) return;
  const int key = index % length, query = index / length % length;
  const int head = index / (length * length) % 8;
  const int batch = index / (length * length * 8);
  const auto* q = qkv + (batch * length + query) * 768 + head * 32;
  const auto* k = qkv + (batch * length + key) * 768 + 256 + head * 32;
  float sum = 0;
  // Match upstream scaling Q before the dot product, including FP32 rounding.
  for (int c = 0; c < 32; ++c)
    sum = fmaf(__fmul_rn(q[c], 0.1767766952966369f), k[c], sum);
  scores[index] = sum;
}
__global__ void Softmax(float* scores, int length) {
  __shared__ float scratch[kThreads];
  const int row = blockIdx.x;
  float maximum = -CUDART_INF_F;
  for (int i = threadIdx.x; i < length; i += blockDim.x)
    maximum = fmaxf(maximum, scores[row * length + i]);
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
  for (int i = threadIdx.x; i < length; i += blockDim.x) {
    const float value = expf(scores[row * length + i] - maximum);
    scores[row * length + i] = value;
    sum += value;
  }
  const float total = Sum(sum, scratch);
  for (int i = threadIdx.x; i < length; i += blockDim.x)
    scores[row * length + i] /= total;
}
__global__ void Attend(const float* qkv, const float* scores, float* output,
                       int length, int batches) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= batches * length * 256) return;
  const int c = index % 256, query = index / 256 % length;
  const int batch = index / (256 * length), head = c / 32;
  const auto* row = scores + ((batch * 8 + head) * length + query) * length;
  float sum = 0;
  for (int key = 0; key < length; ++key)
    sum = fmaf(row[key], qkv[(batch * length + key) * 768 + 512 + c], sum);
  output[index] = sum;
}
__global__ void Residual(float* hidden, const float* update, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) hidden[index] = __fadd_rn(hidden[index], update[index]);
}
__global__ void MergePath(const float* input, float* hidden,
                          SeparatorAffineWeights weights, const float* stats,
                          int segments, bool intra) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= 250 * segments * 256) return;
  const int c = index % 256, s = index / 256 % segments;
  const int k = index / (256 * segments);
  const int at = intra ? (s * 250 + k) * 256 + c : index;
  const float value = (input[at] - stats[0]) * rsqrtf(stats[1] + 1e-8f);
  const float normalized =
      __fadd_rn(__fmul_rn(value, weights.weight[c]), weights.bias[c]);
  hidden[index] = __fadd_rn(hidden[index], normalized);
}
__global__ void Prelu(float* values, const float* weight, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count && values[index] < 0)
    values[index] = __fmul_rn(values[index], weight[0]);
}
__global__ void Join(const float* input, float* output, int frames,
                     int segments) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= 2 * frames * 256) return;
  const int c = index % 256, t = index / 256 % frames;
  const int source = index / (frames * 256), s = t / 125, k = t % 125;
  output[index] =
      __fadd_rn(input[((k + 125) * segments + s) * 512 + source * 256 + c],
                input[(k * segments + s + 1) * 512 + source * 256 + c]);
}
__global__ void Gate(float* output, const float* gate, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) output[index] = __fmul_rn(output[index], gate[index]);
}
__global__ void Mask(const float* mask, const float* encoded, float* output,
                     int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < 2 * count)
    output[index] = __fmul_rn(mask[index], encoded[index % count]);
}
__global__ void Decode(const float* input, const float* weight, float* output,
                       int frames, int samples, int decoded) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= 2 * samples) return;
  const int source = index / samples, t = index % samples;
  if (t >= decoded) {
    output[index] = 0;
    return;
  }
  float sum = 0;
  const int last = t / 8;
  for (int c = 0; c < 256; ++c) {
    for (int frame = last - 1; frame <= last; ++frame) {
      if (frame < 0 || frame >= frames) continue;
      const int k = t - frame * 8;
      sum = fmaf(input[(source * frames + frame) * 256 + c], weight[c * 16 + k],
                 sum);
    }
  }
  output[index] = sum;
}
__global__ void CheckFinite(const float* input, int count, int* failed) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count && !isfinite(input[index])) atomicExch(failed, 1);
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
                int rows, int in, int out, void* workspace,
                cudaStream_t stream) {
  std::lock_guard<std::mutex> lock(quant::LtCacheMutex());
  auto handle = quant::GlobalLtHandle();
  if (!handle) return Status::Fail("Separator cuBLAS handle unavailable");
  MatmulPlan plan;
  const auto ok = [](cublasStatus_t s) { return s == CUBLAS_STATUS_SUCCESS; };
  if (!ok(cublasLtMatmulDescCreate(&plan.operation, CUBLAS_COMPUTE_32F_PEDANTIC,
                                   CUDA_R_32F)) ||
      !ok(cublasLtMatrixLayoutCreate(&plan.a, CUDA_R_32F, rows, in, in)) ||
      !ok(cublasLtMatrixLayoutCreate(&plan.b, CUDA_R_32F, out, in, in)) ||
      !ok(cublasLtMatrixLayoutCreate(&plan.c, CUDA_R_32F, rows, out, out)) ||
      !ok(cublasLtMatmulPreferenceCreate(&plan.preference)))
    return Status::Fail("Separator FP32 matrix descriptor failed");
  const cublasLtOrder_t order = CUBLASLT_ORDER_ROW;
  for (auto matrix : {plan.a, plan.b, plan.c})
    if (!ok(cublasLtMatrixLayoutSetAttribute(
            matrix, CUBLASLT_MATRIX_LAYOUT_ORDER, &order, sizeof(order))))
      return Status::Fail("Separator matrix order failed");
  const cublasOperation_t transpose = CUBLAS_OP_T;
  const size_t bytes = SeparatorWorkspaceLayout::kGemmBytes;
  const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
  if (!ok(cublasLtMatmulDescSetAttribute(plan.operation,
                                         CUBLASLT_MATMUL_DESC_TRANSB,
                                         &transpose, sizeof(transpose))) ||
      !ok(cublasLtMatmulPreferenceSetAttribute(
          plan.preference, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &bytes,
          sizeof(bytes))) ||
      !ok(cublasLtMatmulPreferenceSetAttribute(
          plan.preference, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK,
          &reduction, sizeof(reduction))))
    return Status::Fail("Separator matrix operation failed");
  const auto alignment = [](const void* p) {
    const auto a = reinterpret_cast<uintptr_t>(p);
    return static_cast<uint32_t>(std::min(uintptr_t{256}, a & -a));
  };
  const std::pair<cublasLtMatmulPreferenceAttributes_t, uint32_t> aligned[] = {
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_A_BYTES, alignment(input)},
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_B_BYTES, alignment(weight)},
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_C_BYTES, alignment(output)},
      {CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_D_BYTES, alignment(output)}};
  for (const auto& [field, value] : aligned)
    if (!ok(cublasLtMatmulPreferenceSetAttribute(plan.preference, field, &value,
                                                 sizeof(value))))
      return Status::Fail("Separator matrix alignment failed");
  cublasLtMatmulHeuristicResult_t algorithm{};
  int count = 0;
  if (!ok(cublasLtMatmulAlgoGetHeuristic(handle, plan.operation, plan.a, plan.b,
                                         plan.c, plan.c, plan.preference, 1,
                                         &algorithm, &count)) ||
      count != 1 || algorithm.state != CUBLAS_STATUS_SUCCESS)
    return Status::Fail("Separator FP32 matrix algorithm unavailable");
  const float alpha = 1, beta = 0;
  const auto result =
      cublasLtMatmul(handle, plan.operation, &alpha, input, plan.a, weight,
                     plan.b, &beta, output, plan.c, output, plan.c,
                     &algorithm.algo, workspace, bytes, stream);
  return ok(result) ? Status{}
                    : Status::Fail("Separator matrix failed: " +
                                   std::to_string(static_cast<int>(result)));
}
Status Linear(const float* input, SeparatorAffineWeights weights, float* output,
              int rows, int in, int out, int activation, void* workspace,
              cudaStream_t stream) {
  auto status =
      Multiply(input, weights.weight, output, rows, in, out, workspace, stream);
  if (!status) return status;
  Affine<<<(rows * out + 255) / 256, kThreads, 0, stream>>>(
      output, weights.bias, rows * out, out, activation);
  return {};
}
SeparatorComputeResult Complete(Status status, cudaStream_t stream,
                                cudaError_t submitted = cudaSuccess) {
  const auto launch = cudaGetLastError();
  const auto synced = cudaStreamSynchronize(stream);
  const auto error = submitted != cudaSuccess ? submitted
                     : launch != cudaSuccess  ? launch
                                              : synced;
  return {std::move(status), error};
}
bool Valid(const SeparatorDeviceWeights& weights) {
  const auto affine = [](const auto& w) { return w.weight && w.bias; };
  if (!weights.encoder || !weights.decoder || !weights.project ||
      !weights.prelu || !weights.mask_project || !affine(weights.norm) ||
      !affine(weights.sources) || !affine(weights.output) ||
      !affine(weights.gate))
    return false;
  for (const auto& path : weights.paths) {
    if (!path.position || !affine(path.norm) || !affine(path.group_norm))
      return false;
    for (const auto& layer : path.layers)
      if (!affine(layer.norm1) || !affine(layer.norm2) || !affine(layer.qkv) ||
          !affine(layer.attention) || !affine(layer.ffn1) ||
          !affine(layer.ffn2))
        return false;
  }
  return true;
}
}  // namespace

SeparatorComputeResult SeparatorRun::Begin(
    const SeparatorDeviceWeights& weights, std::span<const float> pcm,
    void* workspace, size_t bytes, cudaStream_t stream) {
  if (started_ || failed_ || finished_ || !Valid(weights) || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256)
    return {Status::Fail("Separator input or execution state invalid")};
  SeparatorWorkspaceLayout layout;
  auto status = PlanSeparatorWorkspace(
      pcm.size(), SeparatorWorkspaceLayout::kMaxSamples, bytes, &layout);
  if (!status) return {status};
  if (reinterpret_cast<uintptr_t>(workspace) >
          std::numeric_limits<uintptr_t>::max() - layout.bytes ||
      !std::all_of(pcm.begin(), pcm.end(),
                   [](float x) { return std::isfinite(x); }))
    return {Status::Fail("Separator nonfinite input or workspace extent")};
  auto* base = static_cast<uint8_t*>(workspace);
  auto result =
      Complete({}, stream,
               cudaMemcpyAsync(base + layout.pcm, pcm.data(), pcm.size_bytes(),
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

SeparatorComputeResult SeparatorRun::NextStage(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_ >= kStages)
    return {Status::Fail("Separator stage state invalid")};
  const int frames = layout_.frames, rows = layout_.rows;
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace_ + offset);
  };
  auto* gemm = workspace_ + layout_.gemm;
  auto* encoded = at(layout_.encoded);
  auto* hidden = at(layout_.hidden);
  auto* partial = at(layout_.partial);
  auto* statistics = at(layout_.statistics);
  const float* checked = nullptr;
  int count = 0;
  Status status;
  if (completed_ == 0) {
    Encode<<<(frames * 256 + 255) / 256, kThreads, 0, stream>>>(
        at(layout_.pcm), weights_->encoder, encoded, frames);
    checked = encoded;
    count = frames * 256;
  } else if (completed_ == 1) {
    GroupStatistics(encoded, partial, statistics, frames * 256, stream);
    GroupNormalize<<<(frames * 256 + 255) / 256, kThreads, 0, stream>>>(
        encoded, at(layout_.normalized), weights_->norm, statistics,
        frames * 256);
    status = Multiply(at(layout_.normalized), weights_->project,
                      at(layout_.projected), frames, 256, 256, gemm, stream);
    if (status)
      Split<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(
          at(layout_.projected), hidden, frames, layout_.segments);
    checked = hidden;
    count = rows * 256;
  } else if (completed_ < 34) {
    const int path_index = (completed_ - 2) / 8;
    const int layer_index = (completed_ - 2) % 8;
    const auto& path_weights = weights_->paths[path_index];
    const auto& layer = path_weights.layers[layer_index];
    const bool intra = path_index % 2 == 0;
    const int length = intra ? 250 : layout_.segments;
    const int batches = intra ? layout_.segments : 250;
    auto* path = at(layout_.path);
    auto* norm = at(layout_.norm);
    auto* qkv = at(layout_.qkv);
    auto* scores = at(layout_.scores);
    auto* update = at(layout_.update);
    if (layer_index == 0)
      PreparePath<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(
          hidden, path_weights.position, path, layout_.segments, intra);
    LayerNormalize<<<rows, kThreads, 0, stream>>>(path, norm, layer.norm1);
    status = Linear(norm, layer.qkv, qkv, rows, 256, 768, 0, gemm, stream);
    if (status) {
      const int elements = batches * 8 * length * length;
      Scores<<<(elements + 255) / 256, kThreads, 0, stream>>>(qkv, scores,
                                                              length, batches);
      Softmax<<<batches * 8 * length, kThreads, 0, stream>>>(scores, length);
      Attend<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(
          qkv, scores, at(layout_.attention), length, batches);
      status = Linear(at(layout_.attention), layer.attention, update, rows, 256,
                      256, 0, gemm, stream);
    }
    if (status) {
      Residual<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(path, update,
                                                                  rows * 256);
      LayerNormalize<<<rows, kThreads, 0, stream>>>(path, norm, layer.norm2);
      status = Linear(norm, layer.ffn1, at(layout_.ffn), rows, 256, 1024, 1,
                      gemm, stream);
    }
    if (status)
      status = Linear(at(layout_.ffn), layer.ffn2, update, rows, 1024, 256, 0,
                      gemm, stream);
    if (status) {
      Residual<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(path, update,
                                                                  rows * 256);
      if (layer_index == 7) {
        LayerNormalize<<<rows, kThreads, 0, stream>>>(path, norm,
                                                      path_weights.norm);
        GroupStatistics(norm, partial, statistics, rows * 256, stream);
        MergePath<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(
            norm, hidden, path_weights.group_norm, statistics, layout_.segments,
            intra);
      }
    }
    checked = layer_index == 7 ? hidden : path;
    count = rows * 256;
  } else if (completed_ == 34) {
    Prelu<<<(rows * 256 + 255) / 256, kThreads, 0, stream>>>(
        hidden, weights_->prelu, rows * 256);
    status = Linear(hidden, weights_->sources, at(layout_.source_chunks), rows,
                    256, 512, 0, gemm, stream);
    if (status) {
      Join<<<(2 * frames * 256 + 255) / 256, kThreads, 0, stream>>>(
          at(layout_.source_chunks), at(layout_.joined), frames,
          layout_.segments);
      status = Linear(at(layout_.joined), weights_->output,
                      at(layout_.output_hidden), 2 * frames, 256, 256, 2, gemm,
                      stream);
    }
    if (status)
      status = Linear(at(layout_.joined), weights_->gate, at(layout_.gate),
                      2 * frames, 256, 256, 3, gemm, stream);
    if (status) {
      Gate<<<(2 * frames * 256 + 255) / 256, kThreads, 0, stream>>>(
          at(layout_.output_hidden), at(layout_.gate), 2 * frames * 256);
      status =
          Linear(at(layout_.output_hidden), {weights_->mask_project, nullptr},
                 at(layout_.masks), 2 * frames, 256, 256, 1, gemm, stream);
    }
    checked = at(layout_.masks);
    count = 2 * frames * 256;
  } else {
    Mask<<<(2 * frames * 256 + 255) / 256, kThreads, 0, stream>>>(
        at(layout_.masks), encoded, at(layout_.joined), frames * 256);
    Decode<<<(2 * layout_.samples + 255) / 256, kThreads, 0, stream>>>(
        at(layout_.joined), weights_->decoder, at(layout_.separated), frames,
        layout_.samples, layout_.decoded_samples);
    checked = at(layout_.separated);
    count = 2 * layout_.samples;
  }
  int invalid = 0;
  cudaError_t submitted = cudaSuccess;
  if (status) {
    auto* flag = reinterpret_cast<int*>(workspace_ + layout_.finite_flag);
    submitted = cudaMemsetAsync(flag, 0, sizeof(int), stream);
    if (submitted == cudaSuccess) {
      CheckFinite<<<(count + 255) / 256, kThreads, 0, stream>>>(checked, count,
                                                                flag);
      submitted = cudaMemcpyAsync(&invalid, flag, sizeof(int),
                                  cudaMemcpyDeviceToHost, stream);
    }
  }
  auto result = Complete(std::move(status), stream, submitted);
  if (result.ok() && invalid)
    result.status = Status::Fail("Separator stage produced nonfinite values");
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  ++completed_;
  return {};
}

SeparatorComputeResult SeparatorRun::Finish(std::span<float> sources,
                                            cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_ != kStages ||
      sources.size() != size_t(2 * layout_.samples))
    return {Status::Fail("Separator output state or shape invalid")};
  std::vector<float> result(sources.size());
  auto status = Complete(
      {}, stream,
      cudaMemcpyAsync(result.data(), workspace_ + layout_.separated,
                      sources.size_bytes(), cudaMemcpyDeviceToHost, stream));
  if (status.ok() && !std::all_of(result.begin(), result.end(),
                                  [](float x) { return std::isfinite(x); }))
    status.status = Status::Fail("Separator output is nonfinite");
  if (!status.ok()) {
    failed_ = true;
    return status;
  }
  std::copy(result.begin(), result.end(), sources.begin());
  finished_ = true;
  return {};
}
}  // namespace q4t::audio
