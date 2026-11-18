#include "q4t/audio/tts_encoder.h"

#include <algorithm>
#include <limits>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

__global__ void Elu(float* values, size_t count) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i < count && values[i] < 0) values[i] = expm1f(values[i]);
}

__global__ void Add(float* values, const float* residual, size_t count) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i < count) values[i] = __fadd_rn(values[i], residual[i]);
}

bool Conv(const TtsCodecConvWeights& w, int input, int output, int kernel,
          int stride = 1, bool bias = true) {
  return w.weight && (bias ? w.bias != nullptr : w.bias == nullptr) &&
         w.input == input && w.output == output && w.kernel == kernel &&
         w.stride == stride && w.dilation == 1 && w.groups == 1 &&
         !w.transposed;
}
bool Present(const TtsEncoderDeviceWeights& w) {
  if (!Conv(w.input, 1, 64, 7) || !Conv(w.output, 1024, 512, 3) ||
      !Conv(w.downsample, 512, 512, 4, 2, false) ||
      !w.quantizer_projection[0] || !w.quantizer_projection[1])
    return false;
  constexpr int ratios[] = {4, 5, 6, 8};
  for (size_t i = 0; i < w.blocks.size(); ++i) {
    const int width = 64 << i;
    const auto& b = w.blocks[i];
    if (!Conv(b.first, width, width / 2, 3) ||
        !Conv(b.second, width / 2, width, 1) ||
        !Conv(b.downsample, width, width * 2, 2 * ratios[i], ratios[i]))
      return false;
  }
  for (const auto& layer : w.layers)
    if (!layer.input_norm.weight || !layer.input_norm.bias ||
        !layer.post_norm.weight || !layer.post_norm.bias || !layer.q ||
        !layer.k || !layer.v || !layer.o || !layer.fc1 || !layer.fc2 ||
        !layer.attention_scale || !layer.mlp_scale)
      return false;
  for (size_t i = 0; i < w.usage.size(); ++i)
    if (!w.usage[i] || !w.embedding_sum[i] || !w.initialized[i]) return false;
  return true;
}
}  // namespace

float* TtsEncoderRun::At(size_t offset) const {
  return reinterpret_cast<float*>(workspace_ + offset);
}
float* TtsEncoderRun::Current() const { return At(layout_.wave[wave_]); }
float* TtsEncoderRun::Other() const { return At(layout_.wave[1 - wave_]); }

void TtsEncoderRun::Trace(const char* name, size_t index, const float* data,
                          size_t rows, size_t width) const {
#ifdef Q4T_TTS_TRACE
  const auto count = std::min(rows, size_t{8});
  TraceTtsOperand(name, index, 0, {data, count * width});
  TraceTtsOperand(name, index, 1,
                  {data + (rows - count) * width, count * width});
#else
  (void)name;
  (void)index;
  (void)data;
  (void)rows;
  (void)width;
#endif
}

TtsComputeResult TtsEncoderRun::Begin(const TtsEncoderDeviceWeights& weights,
                                      std::span<const float> pcm,
                                      int sample_rate, void* workspace,
                                      size_t bytes, cudaStream_t stream) {
  TtsEncoderWorkspace layout;
  const auto address = reinterpret_cast<uintptr_t>(workspace);
  const auto source = reinterpret_cast<uintptr_t>(pcm.data());
  if (initialized_ || failed_ || !address || address % 256 || !source ||
      source > std::numeric_limits<uintptr_t>::max() - pcm.size_bytes() ||
      address > std::numeric_limits<uintptr_t>::max() - bytes ||
      (address < source + pcm.size_bytes() && source < address + bytes) ||
      !Present(weights) || !ValidateTtsEncoderPcm(pcm, sample_rate) ||
      !PlanTtsEncoderWorkspace(pcm.size(), bytes, &layout) ||
      layout.bytes != bytes)
    return Invalid("TTS encoder input, weights or workspace invalid");
  output_.resize(layout.frames);
  weights_ = &weights;
  workspace_ = static_cast<uint8_t*>(workspace);
  layout_ = layout;
  rows_ = pcm.size();
  width_ = 1;
  auto copied = cudaMemcpyAsync(Current(), pcm.data(), pcm.size_bytes(),
                                cudaMemcpyHostToDevice, stream);
  if (copied == cudaSuccess)
    copied = cudaMemsetAsync(workspace_ + layout_.finite_flag, 0,
                             sizeof(int32_t), stream);
  auto result = Complete({}, stream, copied);
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  initialized_ = true;
  return {};
}

TtsComputeResult TtsEncoderRun::Convolution(const TtsCodecConvWeights& w,
                                            Stage next, cudaStream_t stream) {
  if (!conv_active_) {
    size_t rows, left;
    if (width_ != w.input ||
        !PlanTtsEncoderConvolution(w, rows_, &rows, &left) ||
        rows * w.output > layout_.wave_elements)
      return Invalid("TTS encoder convolution workspace shape differs");
    conv_ = TtsEncoderConvRun{};
    auto result = conv_.Begin(
        w, rows_, {Current(), rows_ * width_}, {Other(), rows * w.output},
        {At(layout_.columns), TtsCodecConfig::kColumnBytes / sizeof(float)},
        {workspace_ + layout_.gemm, TtsDenseLayout::kGemmBytes});
    if (!result.ok()) return result;
    conv_active_ = true;
  }
  auto result = conv_.Advance(stream);
  if (result.ok() && conv_.done()) {
    rows_ = conv_.output_rows();
    width_ = w.output;
    wave_ = 1 - wave_;
    conv_active_ = false;
    stage_ = next;
  }
  return result;
}

TtsComputeResult TtsEncoderRun::Activate(cudaStream_t stream) {
  const size_t count = rows_ * width_;
  Elu<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(Current(),
                                                                  count);
  return Complete({}, stream);
}

TtsComputeResult TtsEncoderRun::Advance(cudaStream_t stream) {
  if (!initialized_ || failed_ || done())
    return Invalid("TTS encoder advance out of order");
  const auto& w = *weights_;
  TtsComputeResult result;
  switch (stage_) {
    case Stage::kInput:
      result = Convolution(w.input, Stage::kResidualStart, stream);
      if (result.ok() && !conv_active_)
        Trace("encoder-input", 0, Current(), rows_, width_);
      break;
    case Stage::kResidualStart:
      result = Complete({}, stream,
                        cudaMemcpyAsync(At(layout_.wave[2]), Current(),
                                        rows_ * width_ * sizeof(float),
                                        cudaMemcpyDeviceToDevice, stream));
      if (result.ok()) result = Activate(stream);
      if (result.ok()) stage_ = Stage::kResidualFirst;
      break;
    case Stage::kResidualFirst:
      result = Convolution(w.blocks[block_].first, Stage::kResidualElu, stream);
      break;
    case Stage::kResidualElu:
      result = Activate(stream);
      if (result.ok()) stage_ = Stage::kResidualSecond;
      break;
    case Stage::kResidualSecond:
      result =
          Convolution(w.blocks[block_].second, Stage::kResidualAdd, stream);
      break;
    case Stage::kResidualAdd: {
      const size_t count = rows_ * width_;
      Add<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          Current(), At(layout_.wave[2]), count);
      result = Complete({}, stream);
      if (result.ok()) {
        Trace("encoder-residual", block_, Current(), rows_, width_);
        stage_ = Stage::kDownElu;
      }
      break;
    }
    case Stage::kDownElu:
      result = Activate(stream);
      if (result.ok()) stage_ = Stage::kDownsample;
      break;
    case Stage::kDownsample:
      result =
          Convolution(w.blocks[block_].downsample, Stage::kOutputElu, stream);
      if (result.ok() && !conv_active_) {
        Trace("encoder-downsample", block_, Current(), rows_, width_);
        if (++block_ < 4) stage_ = Stage::kResidualStart;
      }
      break;
    case Stage::kOutputElu:
      result = Activate(stream);
      if (result.ok()) stage_ = Stage::kOutput;
      break;
    case Stage::kOutput:
      result = Convolution(w.output, Stage::kTransformerBegin, stream);
      if (result.ok() && !conv_active_)
        Trace("encoder-seanet", 0, Current(), rows_, width_);
      break;
    case Stage::kTransformerBegin:
      result = TransformerStart(stream);
      if (result.ok()) stage_ = Stage::kAttentionProject;
      break;
    case Stage::kAttentionProject:
      result = AttentionProject(stream);
      if (result.ok()) {
        head_ = 0;
        stage_ = Stage::kAttentionHead;
      }
      break;
    case Stage::kAttentionHead:
      result = AttentionHead(stream);
      if (result.ok() && ++head_ == 8) stage_ = Stage::kAttentionFinish;
      break;
    case Stage::kAttentionFinish:
      result = AttentionFinish(stream);
      if (result.ok()) stage_ = Stage::kFeedForward;
      break;
    case Stage::kFeedForward:
      result = FeedForward(stream);
      if (result.ok()) {
        Trace("encoder-transformer", layer_, At(layout_.hidden), rows_, 512);
        stage_ =
            ++layer_ < 8 ? Stage::kAttentionProject : Stage::kTransformerFinish;
      }
      break;
    case Stage::kTransformerFinish:
      result = Complete({}, stream,
                        cudaMemcpyAsync(Current(), At(layout_.hidden),
                                        rows_ * 512 * sizeof(float),
                                        cudaMemcpyDeviceToDevice, stream));
      if (result.ok()) stage_ = Stage::kFinalDownsample;
      break;
    case Stage::kFinalDownsample:
      result = Convolution(w.downsample, Stage::kQuantizerProjection, stream);
      if (result.ok() && !conv_active_)
        Trace("encoder-latent", 0, Current(), rows_, width_);
      break;
    case Stage::kQuantizerProjection: {
      const auto status =
          Matmul(Current(), w.quantizer_projection[group_ == 0 ? 0 : 1],
                 At(layout_.residual), static_cast<int>(rows_), 256, 512, 512,
                 CUDA_R_32F, CUDA_R_32F, workspace_ + layout_.gemm,
                 TtsDenseLayout::kGemmBytes, stream);
      result = Complete(status, stream);
      if (result.ok()) {
        Trace("encoder-projection", group_ == 0 ? 0 : 1, At(layout_.residual),
              rows_, 256);
        stage_ = Stage::kQuantizer;
      }
      break;
    }
    case Stage::kQuantizer:
      result = Quantize(stream);
      if (result.ok()) {
        Trace("encoder-rvq-residual", group_, At(layout_.residual), rows_, 256);
        ++group_;
        if (group_ == 1) stage_ = Stage::kQuantizerProjection;
        if (group_ == 16) stage_ = Stage::kFinish;
      }
      break;
    case Stage::kFinish: {
      int32_t invalid = 0;
      result = Complete(
          {}, stream,
          cudaMemcpyAsync(&invalid, workspace_ + layout_.finite_flag,
                          sizeof(invalid), cudaMemcpyDeviceToHost, stream));
      if (result.ok() && invalid)
        result = Invalid("TTS encoder nonfinite intermediate or codebook");
      if (result.ok())
        result =
            Complete({}, stream,
                     cudaMemcpyAsync(output_.data(), workspace_ + layout_.codes,
                                     output_.size() * sizeof(TtsCodes),
                                     cudaMemcpyDeviceToHost, stream));
      if (result.ok()) stage_ = Stage::kDone;
      break;
    }
    case Stage::kDone:
      return Invalid("TTS encoder already complete");
  }
  if (!result.ok()) failed_ = true;
  return result;
}

}  // namespace q4t::audio
