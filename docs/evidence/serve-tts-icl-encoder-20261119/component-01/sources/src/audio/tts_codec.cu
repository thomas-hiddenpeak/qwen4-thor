#include "q4t/audio/tts_codec.h"

#include <algorithm>
#include <limits>

#include "tts_codec_internal.h"
#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

struct Books {
  const float* usage[16];
  const float* embeddings[16];
};

__global__ void Quantize(Books books, const int32_t* codes, float* first,
                         float* rest, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int row = i / 256, d = i % 256;
  float sum = 0;
  for (int group = 0; group < 16; ++group) {
    const int code = codes[row * 16 + group];
    const float value = books.embeddings[group][code * 256 + d] /
                        fmaxf(books.usage[group][code], 1e-5f);
    if (group == 0)
      first[i] = value;
    else
      sum = __fadd_rn(sum, value);
  }
  rest[i] = sum;
}

__global__ void Add(float* values, const float* residual, const float* gamma,
                    size_t count, int width) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i < count)
    values[i] =
        __fadd_rn(residual[i],
                  gamma ? __fmul_rn(values[i], gamma[i % width]) : values[i]);
}

__global__ void Snake(float* values, const float* alpha, const float* beta,
                      size_t count, int width) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int channel = i % width;
  const float x = values[i];
  const float wave = sinf(__fmul_rn(x, expf(alpha[channel])));
  const float reciprocal = 1.0f / (expf(beta[channel]) + 1e-9f);
  values[i] = __fadd_rn(x, __fmul_rn(reciprocal, __fmul_rn(wave, wave)));
}

__global__ void Clamp(float* values, size_t count, int* invalid) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const float value = values[i];
  if (!isfinite(value)) {
    atomicExch(invalid, 1);
    return;  // Do not turn a failed model output into apparently valid silence.
  }
  values[i] = fminf(1.0f, fmaxf(-1.0f, value));
}

bool Present(TtsAffineWeights w) { return w.weight && w.bias; }
bool Present(TtsCodecConvWeights w) { return w.weight && w.bias; }
bool Present(TtsCodecSnakeWeights w) { return w.alpha && w.beta; }
bool Present(const TtsCodecDeviceWeights& w) {
  for (int i = 0; i < 16; ++i)
    if (!w.usage[i] || !w.embedding_sum[i]) return false;
  if (!w.rvq_projection[0] || !w.rvq_projection[1] || !w.final_norm ||
      !Present(w.pre_conv) || !Present(w.input_projection) ||
      !Present(w.output_projection) || !Present(w.vocoder_input) ||
      !Present(w.vocoder_output) || !Present(w.final_act))
    return false;
  for (const auto& v : w.layers)
    if (!v.input_norm || !v.post_norm || !v.q || !v.k || !v.v || !v.o ||
        !v.gate || !v.up || !v.down || !v.attention_scale || !v.mlp_scale)
      return false;
  for (const auto& v : w.upsample)
    if (!Present(v.transpose) || !Present(v.depthwise) || !Present(v.norm) ||
        !Present(v.fc1) || !Present(v.fc2) || !v.gamma)
      return false;
  for (const auto& v : w.vocoder) {
    if (!Present(v.act) || !Present(v.transpose)) return false;
    for (const auto& r : v.residuals)
      if (!Present(r.act1) || !Present(r.act2) || !Present(r.conv1) ||
          !Present(r.conv2))
        return false;
  }
  return true;
}
}  // namespace

float* TtsCodecRun::At(size_t offset) const {
  return reinterpret_cast<float*>(workspace_ + offset);
}
float* TtsCodecRun::Current() const { return At(layout_.wave_buffers[wave_]); }
float* TtsCodecRun::Other() const {
  return At(layout_.wave_buffers[1 - wave_]);
}

std::span<const float> TtsCodecRun::pcm() const {
  return done() && !failed_ ? std::span<const float>(Current(), layout_.samples)
                            : std::span<const float>{};
}

void TtsCodecRun::Trace(const char* name, size_t index) const {
#ifdef Q4T_TTS_TRACE
  // Bound intermediate captures independently of waveform length. PCM is
  // checked in full; these head/tail rows help locate a concrete discrepancy.
  const size_t count = std::min(rows_, size_t{8}) * width_;
  TraceTtsOperand(name, index, 0, {Current(), count});
  TraceTtsOperand(
      name, index, 1,
      {Current() + (rows_ - std::min(rows_, size_t{8})) * width_, count});
#else
  (void)name;
  (void)index;
#endif
}

TtsComputeResult TtsCodecRun::Begin(const TtsCodecDeviceWeights& weights,
                                    std::span<const TtsCodes> codes,
                                    void* workspace, size_t bytes,
                                    cudaStream_t stream) {
  if (initialized_ || failed_ || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256 || !Present(weights))
    return Invalid("TTS codec initialization invalid");
  if (auto s = ValidateTtsCodecCodes(codes); !s) return {s, cudaSuccess};
  if (auto s = PlanTtsCodecWorkspace(codes.size(), bytes, &layout_); !s)
    return {s, cudaSuccess};
  if (reinterpret_cast<uintptr_t>(workspace) >
      std::numeric_limits<uintptr_t>::max() - layout_.bytes)
    return Invalid("TTS codec workspace address overflow");
  workspace_ = static_cast<uint8_t*>(workspace);
  weights_ = &weights;
  rows_ = codes.size();
  width_ = 512;
  const auto copied =
      cudaMemcpyAsync(workspace_ + layout_.codes, codes.data(),
                      codes.size_bytes(), cudaMemcpyHostToDevice, stream);
  auto result = Complete({}, stream, copied);
  if (result.ok())
    initialized_ = true;
  else
    failed_ = true;
  return result;
}

TtsComputeResult TtsCodecRun::Convolution(const TtsCodecConvWeights& weights,
                                          Stage next, cudaStream_t stream) {
  if (!conv_active_) {
    const size_t output_rows =
        rows_ * (weights.transposed ? weights.stride : 1);
    if (weights.input != width_ || weights.output <= 0 ||
        output_rows > layout_.samples ||
        output_rows * weights.output > layout_.buffer_elements)
      return Invalid("TTS codec stage shape exceeds allocated waveform");
    conv_ = TtsCodecConvRun{};
    auto result = conv_.Begin(
        weights, rows_, {Current(), rows_ * width_},
        {Other(), output_rows * weights.output},
        {At(layout_.columns), TtsCodecConfig::kColumnBytes / sizeof(float)},
        {At(layout_.transpose_weights),
         TtsCodecConfig::kTransposeWeightBytes / sizeof(float)},
        {workspace_ + layout_.gemm, TtsDenseLayout::kGemmBytes}, stream);
    if (!result.ok()) return result;
    conv_active_ = true;
  }
  auto result = conv_.Advance(stream);
  if (result.ok() && conv_.done()) {
    rows_ = conv_.output_rows();
    width_ = weights.output;
    wave_ = 1 - wave_;
    conv_active_ = false;
    stage_ = next;
  }
  return result;
}

TtsComputeResult TtsCodecRun::SaveResidual(cudaStream_t stream) {
  return Complete({}, stream,
                  cudaMemcpyAsync(At(layout_.wave_buffers[2]), Current(),
                                  rows_ * width_ * sizeof(float),
                                  cudaMemcpyDeviceToDevice, stream));
}

TtsComputeResult TtsCodecRun::Activate(TtsCodecSnakeWeights weights,
                                       cudaStream_t stream) {
  const size_t count = rows_ * width_;
  Snake<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      Current(), weights.alpha, weights.beta, count, width_);
  return Complete({}, stream);
}

TtsComputeResult TtsCodecRun::Advance(cudaStream_t stream) {
  if (!initialized_ || failed_ || done())
    return Invalid("TTS codec advance out of order");
  const auto& w = *weights_;
  TtsComputeResult result;
  switch (stage_) {
    case Stage::kQuantize: {
      Books books;
      for (int i = 0; i < 16; ++i) {
        books.usage[i] = w.usage[i];
        books.embeddings[i] = w.embedding_sum[i];
      }
      const int count = static_cast<int>(rows_) * 256;
      Quantize<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          books, reinterpret_cast<int32_t*>(workspace_ + layout_.codes),
          At(layout_.rvq_first), At(layout_.rvq_rest), count);
      result = Complete({}, stream);
      if (result.ok()) stage_ = Stage::kProjectFirst;
      break;
    }
    case Stage::kProjectFirst:
    case Stage::kProjectRest: {
      const bool first = stage_ == Stage::kProjectFirst;
      auto status =
          Matmul(At(first ? layout_.rvq_first : layout_.rvq_rest),
                 w.rvq_projection[first ? 0 : 1], first ? Current() : Other(),
                 rows_, 512, 256, 256, CUDA_R_32F, CUDA_R_32F,
                 workspace_ + layout_.gemm, TtsDenseLayout::kGemmBytes, stream);
      if (status && !first) {
        const size_t count = rows_ * 512;
        Add<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
            Current(), Other(), nullptr, count, 512);
      }
      result = Complete(status, stream);
      if (result.ok()) {
        stage_ = first ? Stage::kProjectRest : Stage::kPreConv;
        if (!first) Trace("codec-rvq", 0);
      }
      break;
    }
    case Stage::kPreConv:
      result = Convolution(w.pre_conv, Stage::kTransformerBegin, stream);
      if (result.ok() && !conv_active_) Trace("codec-pre-conv", 0);
      break;
    case Stage::kTransformerBegin:
      result = codec_detail::TransformerBegin(w, layout_, workspace_, Current(),
                                              stream);
      if (result.ok()) stage_ = Stage::kTransformerLayer;
      break;
    case Stage::kTransformerLayer:
      result = codec_detail::TransformerLayer(w, layout_, workspace_, layer_,
                                              stream);
      if (result.ok() && ++layer_ == 8) stage_ = Stage::kTransformerFinish;
      break;
    case Stage::kTransformerFinish:
      result = codec_detail::TransformerFinish(w, layout_, workspace_,
                                               Current(), stream);
      if (result.ok()) {
        Trace("codec-transformer", 0);
        stage_ = Stage::kUpsampleTranspose;
      }
      break;
    case Stage::kUpsampleTranspose:
      result = Convolution(w.upsample[upsample_].transpose,
                           Stage::kConvNextDepthwise, stream);
      break;
    case Stage::kConvNextDepthwise:
      if (!conv_active_) result = SaveResidual(stream);
      if (result.ok())
        result = Convolution(w.upsample[upsample_].depthwise,
                             Stage::kConvNextNorm, stream);
      break;
    case Stage::kConvNextNorm:
      result = codec_detail::LayerNorm(Current(), w.upsample[upsample_].norm,
                                       rows_, 1024, stream);
      if (result.ok()) stage_ = Stage::kConvNextFc1;
      break;
    case Stage::kConvNextFc1:
    case Stage::kConvNextFc2: {
      const bool first = stage_ == Stage::kConvNextFc1;
      const auto weights =
          first ? w.upsample[upsample_].fc1 : w.upsample[upsample_].fc2;
      result = codec_detail::Affine(Current(), weights, Other(), rows_,
                                    first ? 4096 : 1024, width_, first,
                                    workspace_ + layout_.gemm, stream);
      if (result.ok()) {
        wave_ = 1 - wave_;
        width_ = first ? 4096 : 1024;
        stage_ = first ? Stage::kConvNextFc2 : Stage::kConvNextResidual;
      }
      break;
    }
    case Stage::kConvNextResidual: {
      const size_t count = rows_ * width_;
      Add<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          Current(), At(layout_.wave_buffers[2]), w.upsample[upsample_].gamma,
          count, width_);
      result = Complete({}, stream);
      if (result.ok()) {
        Trace("codec-upsample", upsample_);
        stage_ =
            ++upsample_ < 2 ? Stage::kUpsampleTranspose : Stage::kVocoderInput;
      }
      break;
    }
    case Stage::kVocoderInput:
      result = Convolution(w.vocoder_input, Stage::kVocoderSnake, stream);
      break;
    case Stage::kVocoderSnake:
      result = Activate(w.vocoder[vocoder_].act, stream);
      if (result.ok()) stage_ = Stage::kVocoderTranspose;
      break;
    case Stage::kVocoderTranspose:
      result = Convolution(w.vocoder[vocoder_].transpose,
                           Stage::kResidualSnake1, stream);
      break;
    case Stage::kResidualSnake1:
      result = SaveResidual(stream);
      if (result.ok())
        result =
            Activate(w.vocoder[vocoder_].residuals[residual_].act1, stream);
      if (result.ok()) stage_ = Stage::kResidualConv1;
      break;
    case Stage::kResidualConv1:
      result = Convolution(w.vocoder[vocoder_].residuals[residual_].conv1,
                           Stage::kResidualSnake2, stream);
      break;
    case Stage::kResidualSnake2:
      result = Activate(w.vocoder[vocoder_].residuals[residual_].act2, stream);
      if (result.ok()) stage_ = Stage::kResidualConv2;
      break;
    case Stage::kResidualConv2:
      result = Convolution(w.vocoder[vocoder_].residuals[residual_].conv2,
                           Stage::kResidualAdd, stream);
      break;
    case Stage::kResidualAdd: {
      const size_t count = rows_ * width_;
      Add<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          Current(), At(layout_.wave_buffers[2]), nullptr, count, width_);
      result = Complete({}, stream);
      if (result.ok()) {
        if (++residual_ < 3)
          stage_ = Stage::kResidualSnake1;
        else {
          Trace("codec-vocoder", vocoder_);
          residual_ = 0;
          stage_ = ++vocoder_ < 4 ? Stage::kVocoderSnake : Stage::kFinalSnake;
        }
      }
      break;
    }
    case Stage::kFinalSnake:
      result = Activate(w.final_act, stream);
      if (result.ok()) stage_ = Stage::kFinalConv;
      break;
    case Stage::kFinalConv:
      result = Convolution(w.vocoder_output, Stage::kClamp, stream);
      break;
    case Stage::kClamp: {
      if (rows_ != layout_.samples || width_ != 1) {
        result = Invalid("TTS codec final sample count differs");
        break;
      }
      auto* invalid = reinterpret_cast<int*>(workspace_ + layout_.columns);
      auto copied = cudaMemsetAsync(invalid, 0, sizeof(int), stream);
      int host_invalid = 0;
      if (copied == cudaSuccess) {
        Clamp<<<(rows_ + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
            Current(), rows_, invalid);
        copied = cudaMemcpyAsync(&host_invalid, invalid, sizeof(int),
                                 cudaMemcpyDeviceToHost, stream);
      }
      result = Complete({}, stream, copied);
      if (result.ok() && host_invalid)
        result = Invalid("TTS codec returned nonfinite waveform");
      if (result.ok()) stage_ = Stage::kDone;
      break;
    }
    case Stage::kDone:
      return Invalid("TTS codec already complete");
  }
  if (!result.ok()) failed_ = true;
  return result;
}

}  // namespace q4t::audio
