#include "q4t/audio/tts_model.h"

#include <math_constants.h>

#include <limits>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

__global__ void Norm(const uint16_t* input, const uint16_t* weight,
                     uint16_t* output, int hidden) {
  __shared__ float reduction[kThreads];
  const size_t base = static_cast<size_t>(blockIdx.x) * hidden;
  float sum = 0;
  for (int d = threadIdx.x; d < hidden; d += blockDim.x) {
    const float x = Read(input, base + d);
    sum += __fmul_rn(x, x);
  }
  const float rms = rsqrtf(Reduce<false>(sum, reduction) / hidden + 1e-6f);
  for (int d = threadIdx.x; d < hidden; d += blockDim.x)
    output[base + d] = Write(__fmul_rn(
        Round(__fmul_rn(Read(input, base + d), rms)), Read(weight, d)));
}

__global__ void RopeTable(uint16_t* rope, int rows, int past) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= rows * 64) return;
  const int row = i / 64, d = i % 64;
  const float inverse = 1.0f / powf(1000000.0f, 2.0f * d / 128);
  const float angle = __fmul_rn(static_cast<float>(past + row), inverse);
  rope[row * 128 + d] = Write(cosf(angle));
  rope[row * 128 + d + 64] = Write(sinf(angle));
}

__global__ void NormRope(const uint16_t* input, const uint16_t* weight,
                         const uint16_t* rope, uint16_t* output, int heads,
                         int stride, int past) {
  __shared__ float reduction[128], normalized[128];
  const int row = blockIdx.x, h = blockIdx.y, d = threadIdx.x;
  const float x = Read(input, (row * heads + h) * 128 + d);
  const float rms = rsqrtf(Reduce<false>(__fmul_rn(x, x), reduction) / 128 +
                            1e-6f);
  normalized[d] = Round(__fmul_rn(Round(__fmul_rn(x, rms)), Read(weight, d)));
  __syncthreads();
  const int pair = (d + 64) % 128;
  const float rotated = d < 64 ? -normalized[pair] : normalized[pair];
  const float cosine = Read(rope, row * 128 + d % 64);
  const float sine = Read(rope, row * 128 + 64 + d % 64);
  output[(static_cast<size_t>(h) * stride + past + row) * 128 + d] =
      Write(__fadd_rn(Round(__fmul_rn(normalized[d], cosine)),
                      Round(__fmul_rn(rotated, sine))));
}

__global__ void CacheValues(const uint16_t* values, uint16_t* cache,
                            int rows, int context, int past) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= rows * 1024) return;
  const int row = i / 1024, h = i % 1024 / 128, d = i % 128;
  cache[(h * 128 + d) * context + past + row] = values[i];
}

__global__ void Softmax(uint16_t* scores, int stride, int past) {
  __shared__ float reduction[kThreads];
  const int row = blockIdx.x, last = past + row;
  const size_t base = static_cast<size_t>(row) * stride;
  float maximum = -CUDART_INF_F;
  for (int j = threadIdx.x; j < stride; j += blockDim.x) {
    const float score = j <= last
        ? Round(__fmul_rn(Read(scores, base + j), 0.08838834764831845f))
        : -CUDART_INF_F;
    scores[base + j] = Write(score);
    maximum = fmaxf(maximum, score);
  }
  maximum = Reduce<true>(maximum, reduction);
  __syncthreads();
  float sum = 0;
  for (int j = threadIdx.x; j <= last; j += blockDim.x)
    sum += expf(Read(scores, base + j) - maximum);
  sum = Reduce<false>(sum, reduction);
  for (int j = threadIdx.x; j < stride; j += blockDim.x)
    scores[base + j] = Write(j <= last
        ? expf(Read(scores, base + j) - maximum) / sum : 0.0f);
}

__global__ void Merge(const uint16_t* head, uint16_t* output, int rows, int h) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < rows * 128) output[(i / 128 * 16 + h) * 128 + i % 128] = head[i];
}

__global__ void Residual(uint16_t* hidden, const uint16_t* update, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) hidden[i] = Write(__fadd_rn(Read(hidden, i), Read(update, i)));
}

__global__ void SwiGlu(uint16_t* gate, const uint16_t* up, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const float x = Read(gate, i);
  gate[i] = Write(__fmul_rn(Round(x / (1.0f + expf(-x))), Read(up, i)));
}

bool Present(const TtsLayerWeights& w) {
  return w.input_norm && w.post_norm && w.q_norm && w.k_norm &&
         w.q && w.k && w.v && w.o && w.gate && w.up && w.down;
}
}  // namespace

TtsComputeResult TtsDenseRun::Init(const TtsDenseWeights& weights,
    TtsVariant variant, bool predictor, size_t max_rows, size_t context,
    void* workspace, size_t bytes, cudaStream_t stream) {
  if (initialized_ || failed_ || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256 || !weights.norm)
    return Invalid("TTS dense initialization invalid");
  auto status = PlanTtsDenseWorkspace(variant, predictor, max_rows,
                                      context, bytes, &layout_);
  if (!status) return {status, cudaSuccess};
  for (int i = 0; i < layout_.layers; ++i)
    if (!Present(weights.layers[i]))
      return Invalid("TTS dense layer weights incomplete");
  const auto base = reinterpret_cast<uintptr_t>(workspace);
  if (base > std::numeric_limits<uintptr_t>::max() - layout_.bytes)
    return Invalid("TTS dense workspace address overflow");
  workspace_ = static_cast<uint8_t*>(workspace);
  weights_ = &weights;
  initialized_ = true;
  return Reset(stream);
}

TtsComputeResult TtsDenseRun::Reset(cudaStream_t stream) {
  if (!initialized_ || active_ || failed_)
    return Invalid("TTS dense reset out of order");
  const auto copied = cudaMemsetAsync(workspace_ + layout_.cache_k, 0,
      layout_.gemm - layout_.cache_k, stream);
  auto result = Complete({}, stream, copied);
  failed_ = !result.ok();
  if (result.ok()) past_ = rows_ = layer_ = 0;
  return result;
}

TtsComputeResult TtsDenseRun::Begin(std::span<const uint16_t> input,
                                    cudaStream_t stream) {
  if (!initialized_ || active_ || failed_ || input.empty() ||
      input.size() % layout_.hidden ||
      input.size() / layout_.hidden > static_cast<size_t>(layout_.rows) ||
      input.size() / layout_.hidden > static_cast<size_t>(layout_.context - past_))
    return Invalid("TTS dense prefix or cache capacity invalid");
  const auto source = reinterpret_cast<uintptr_t>(input.data());
  const auto base = reinterpret_cast<uintptr_t>(workspace_);
  if (!source || source % 2 || source + input.size_bytes() < source ||
      (source < base + layout_.bytes && base < source + input.size_bytes()))
    return Invalid("TTS dense input overlaps execution workspace");
  rows_ = static_cast<int>(input.size() / layout_.hidden);
  const auto copied = cudaMemcpyAsync(workspace_ + layout_.hidden_state,
      input.data(), input.size_bytes(), cudaMemcpyDeviceToDevice, stream);
  if (copied == cudaSuccess)
    RopeTable<<<(rows_ * 64 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        reinterpret_cast<uint16_t*>(workspace_ + layout_.rope), rows_, past_);
  auto result = Complete({}, stream, copied);
  failed_ = !result.ok();
  if (result.ok()) { active_ = true; layer_ = 0; }
  return result;
}

TtsComputeResult TtsDenseRun::NextLayer(cudaStream_t stream) {
  if (!initialized_ || !active_ || failed_ || layer_ >= layout_.layers)
    return Invalid("TTS dense layer out of order");
  const auto& w = weights_->layers[layer_];
  const int t = rows_, h = layout_.hidden, f = layout_.intermediate;
  const int context = layout_.padded_context;
  const int used = (past_ + rows_ + 15) / 16 * 16;
  const auto at = [&](size_t offset) {
    return reinterpret_cast<uint16_t*>(workspace_ + offset);
  };
  auto* hidden = at(layout_.hidden_state);
  auto* normalized = at(layout_.normalized);
  auto* q = at(layout_.q); auto* k = at(layout_.k); auto* v = at(layout_.v);
  auto* hq = at(layout_.head_q);
  const size_t layer_offset = static_cast<size_t>(layer_) * context * 1024;
  auto* ck = at(layout_.cache_k) + layer_offset;
  auto* cv = at(layout_.cache_v) + layer_offset;
  auto* scores = at(layout_.scores);
  auto* head = at(layout_.head_output);
  auto* attention = at(layout_.attention);
  auto* projected = at(layout_.projected);
  auto* gate = at(layout_.gate); auto* up = at(layout_.up);
  Status status;
  const auto gemm = [&](const uint16_t* x, const uint16_t* weight,
                         uint16_t* y, int n, int inner, int stride = 0) {
    if (status)
      status = Matmul(x, weight, y, t, n, inner, stride ? stride : inner,
          CUDA_R_16BF, CUDA_R_16BF, workspace_ + layout_.gemm,
          TtsDenseLayout::kGemmBytes, stream);
  };
  Norm<<<t, kThreads, 0, stream>>>(hidden, w.input_norm, normalized, h);
  gemm(normalized, w.q, q, 2048, h);
  gemm(normalized, w.k, k, 1024, h);
  gemm(normalized, w.v, v, 1024, h);
  if (status) {
    NormRope<<<dim3(t, 16), 128, 0, stream>>>(
        q, w.q_norm, at(layout_.rope), hq, 16, layout_.padded_rows, 0);
    NormRope<<<dim3(t, 8), 128, 0, stream>>>(
        k, w.k_norm, at(layout_.rope), ck, 8, context, past_);
    CacheValues<<<(t * 1024 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        v, cv, t, context, past_);
  }
  for (int i = 0; status && i < 16; ++i) {
    const int kv = i / 2;
    gemm(hq + static_cast<size_t>(i) * layout_.padded_rows * 128,
         ck + static_cast<size_t>(kv) * context * 128, scores, used, 128);
    if (!status) break;
    Softmax<<<t, kThreads, 0, stream>>>(scores, used, past_);
    gemm(scores, cv + static_cast<size_t>(kv) * 128 * context,
         head, 128, used, context);
    if (status)
      Merge<<<(t * 128 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          head, attention, t, i);
  }
  gemm(attention, w.o, projected, h, 2048);
  if (status) {
    Residual<<<(t * h + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        hidden, projected, t * h);
    Norm<<<t, kThreads, 0, stream>>>(hidden, w.post_norm, normalized, h);
  }
  gemm(normalized, w.gate, gate, f, h);
  gemm(normalized, w.up, up, f, h);
  if (status)
    SwiGlu<<<(t * f + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        gate, up, t * f);
  gemm(gate, w.down, projected, h, f);
  if (status)
    Residual<<<(t * h + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        hidden, projected, t * h);
  auto result = Complete(status, stream);
  if (result.ok()) ++layer_;
  else failed_ = true;
  return result;
}

TtsComputeResult TtsDenseRun::Finish(std::span<const uint16_t>* last_hidden,
                                     cudaStream_t stream) {
  if (!initialized_ || !active_ || failed_ || layer_ != layout_.layers ||
      !last_hidden)
    return Invalid("TTS dense finish out of order");
  const auto* last = reinterpret_cast<uint16_t*>(
      workspace_ + layout_.hidden_state) + (rows_ - 1) * layout_.hidden;
  auto* out = reinterpret_cast<uint16_t*>(workspace_ + layout_.normalized);
  Norm<<<1, kThreads, 0, stream>>>(last, weights_->norm, out, layout_.hidden);
  auto result = Complete({}, stream);
  failed_ = !result.ok();
  if (result.ok()) {
    *last_hidden = {out, static_cast<size_t>(layout_.hidden)};
    past_ += rows_;
    active_ = false;
  }
  return result;
}

}  // namespace q4t::audio
