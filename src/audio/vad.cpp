#include "q4t/audio/vad.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>

namespace q4t::audio {
namespace {
struct Tensor {
  const char* name;
  std::array<int, 3> shape;
  size_t rank;
};
constexpr std::array<Tensor, 15> kTensors{{
    {"stft_conv.weight", {258, 1, 256}, 3},
    {"conv1.weight", {128, 129, 3}, 3},
    {"conv1.bias", {128, 0, 0}, 1},
    {"conv2.weight", {64, 128, 3}, 3},
    {"conv2.bias", {64, 0, 0}, 1},
    {"conv3.weight", {64, 64, 3}, 3},
    {"conv3.bias", {64, 0, 0}, 1},
    {"conv4.weight", {128, 64, 3}, 3},
    {"conv4.bias", {128, 0, 0}, 1},
    {"lstm_cell.weight_ih", {512, 128, 0}, 2},
    {"lstm_cell.weight_hh", {512, 128, 0}, 2},
    {"lstm_cell.bias_ih", {512, 0, 0}, 1},
    {"lstm_cell.bias_hh", {512, 0, 0}, 1},
    {"final_conv.weight", {1, 128, 1}, 3},
    {"final_conv.bias", {1, 0, 0}, 1},
}};
bool Finite(std::span<const float> values) {
  return std::all_of(values.begin(), values.end(),
                     [](float v) { return std::isfinite(v); });
}
float Sigmoid(float value) { return 1.f / (1.f + std::exp(-value)); }
Status Interrupted() { return Status::Fail("VAD execution cancelled"); }

// Fixed float32 convolution. Preserve trained channel/time and padding order.
bool Conv(const float* input, int channels, int length, int output_channels,
          int kernel, int stride, int padding, const float* weights,
          const float* bias, float* output, const SileroVad::Cancelled& stop) {
  const int count = (length + 2 * padding - kernel) / stride + 1;
  for (int row = 0; row < output_channels; ++row) {
    if (row % 16 == 0 && stop && stop()) return false;
    for (int t = 0; t < count; ++t) {
      float sum = 0;
      for (int c = 0; c < channels; ++c)
        for (int k = 0; k < kernel; ++k) {
          const int at = t * stride + k - padding;
          if (at >= 0 && at < length)
            sum += input[c * length + at] *
                   weights[(row * channels + c) * kernel + k];
        }
      output[row * count + t] = sum + (bias ? bias[row] : 0.f);
    }
  }
  return true;
}
void Relu(std::span<float> values) {
  for (float& v : values) v = std::max(0.f, v);
}
}  // namespace

Status ParseVadTensorLayout(const io::Json& header, size_t data_bytes,
                            VadTensorLayout* out) {
  if (!out || !header.IsObject() || header.object.size() != kTensors.size() ||
      data_bytes != kVadWeightBytes)
    return Status::Fail("VAD tensor file shape differs");
  VadTensorLayout result;
  size_t offset = 0;
  for (size_t i = 0; i < kTensors.size(); ++i) {
    const auto& spec = kTensors[i];
    const auto* item = header.Find(spec.name);
    if (!item || !item->IsObject() || item->object.size() != 3 ||
        item->GetString("dtype") != "F32")
      return Status::Fail("VAD tensor dtype or name differs");
    const auto* shape = item->GetArray("shape");
    const auto* bounds = item->GetArray("data_offsets");
    if (!shape || shape->array.size() != spec.rank || !bounds ||
        bounds->array.size() != 2)
      return Status::Fail("VAD tensor rank or bounds differ");
    size_t bytes = sizeof(float);
    for (size_t j = 0; j < spec.rank; ++j) {
      if (shape->array[j].AsInt(-1) != spec.shape[j])
        return Status::Fail("VAD tensor dimension differs");
      bytes *= spec.shape[j];
    }
    if (bounds->array[0].AsInt(-1) != static_cast<int64_t>(offset) ||
        bounds->array[1].AsInt(-1) != static_cast<int64_t>(offset + bytes))
      return Status::Fail("VAD tensor storage order differs");
    result.offsets[i] = offset / sizeof(float);
    offset += bytes;
  }
  if (offset != data_bytes)
    return Status::Fail("VAD tensor byte count differs");
  *out = result;
  return {};
}

Status SileroVad::Load(std::span<const uint8_t> file, Cancelled cancelled) {
  if (cancelled && cancelled()) return Interrupted();
  if (std::endian::native != std::endian::little || file.size() < 8 ||
      file.size() > kVadWeightBytes + 16392)
    return Status::Fail("VAD file size or byte order invalid");
  uint64_t length = 0;
  for (int i = 0; i < 8; ++i) length |= uint64_t{file[i]} << (8 * i);
  if (!length || length > 16384 || file.size() != 8 + length + kVadWeightBytes)
    return Status::Fail("VAD file header range invalid");
  io::Json header;
  auto status = io::ParseJson(
      std::string(reinterpret_cast<const char*>(file.data() + 8), length),
      &header, false,
      {.max_depth = 5,
       .max_values = 512,
       .max_string_bytes = 4096,
       .reject_duplicate_keys = true});
  VadTensorLayout layout;
  if (status) status = ParseVadTensorLayout(header, kVadWeightBytes, &layout);
  if (!status) return status;
  std::vector<float> weights(kVadWeightBytes / sizeof(float));
  std::memcpy(weights.data(), file.data() + 8 + length, kVadWeightBytes);
  for (size_t i = 0; i < weights.size(); ++i) {
    if (i % 1024 == 0 && cancelled && cancelled()) return Interrupted();
    if (!std::isfinite(weights[i]))
      return Status::Fail("VAD weights nonfinite");
  }
  if (cancelled && cancelled()) return Interrupted();
  weights_ = std::move(weights);
  layout_ = layout;
  return {};
}

Status SileroVad::Step(std::span<const float> samples, VadState* state,
                       float* probability, Cancelled cancelled) const {
  if (cancelled && cancelled()) return Interrupted();
  if (!Ready() || !state || !probability ||
      samples.size() != kVadFrameSamples || !Finite(samples) ||
      !Finite(state->context) || !Finite(state->hidden) || !Finite(state->cell))
    return Status::Fail("VAD frame or recurrent state invalid");
  const auto weight = [&](size_t i) {
    return weights_.data() + layout_.offsets[i];
  };
  std::array<float, 640> padded;
  std::copy(state->context.begin(), state->context.end(), padded.begin());
  std::copy(samples.begin(), samples.end(), padded.begin() + 64);
  // The official model pads only the right side with reflection. Context is
  // the preceding 64 PCM samples, zero only at the beginning of a new stream.
  for (size_t i = 0; i < 64; ++i) padded[576 + i] = padded[574 - i];
  std::array<float, 1032> fourier;
  if (!Conv(padded.data(), 1, 640, 258, 256, 128, 0, weight(0), nullptr,
            fourier.data(), cancelled))
    return Interrupted();
  if (!Finite(fourier)) return Status::Fail("VAD Fourier output nonfinite");
  std::array<float, 516> a{}, b{};
  for (size_t i = 0; i < 516; ++i)
    a[i] = std::sqrt(fourier[i] * fourier[i] +
                     fourier[516 + i] * fourier[516 + i]);
  constexpr std::array<int, 4> channels{129, 128, 64, 64};
  constexpr std::array<int, 4> outputs{128, 64, 64, 128};
  constexpr std::array<int, 4> strides{1, 2, 2, 1};
  int length = 4;
  for (size_t i = 0; i < channels.size(); ++i) {
    if (!Conv(a.data(), channels[i], length, outputs[i], 3, strides[i], 1,
              weight(1 + i * 2), weight(2 + i * 2), b.data(), cancelled))
      return Interrupted();
    length = (length - 1) / strides[i] + 1;
    if (!Finite({b.data(), size_t(outputs[i] * length)}))
      return Status::Fail("VAD convolution output nonfinite");
    Relu({b.data(), size_t(outputs[i] * length)});
    a.swap(b);
  }
  std::array<float, 512> gates;
  for (size_t row = 0; row < gates.size(); ++row) {
    if (row % 16 == 0 && cancelled && cancelled()) return Interrupted();
    float input = 0, recurrent = 0;
    for (size_t c = 0; c < 128; ++c) {
      input += a[c] * weight(9)[row * 128 + c];
      recurrent += state->hidden[c] * weight(10)[row * 128 + c];
    }
    gates[row] = (input + weight(11)[row]) + (recurrent + weight(12)[row]);
  }
  if (!Finite(gates)) return Status::Fail("VAD recurrent gates nonfinite");
  VadState next;
  for (size_t i = 0; i < 128; ++i) {
    next.cell[i] = Sigmoid(gates[128 + i]) * state->cell[i] +
                   Sigmoid(gates[i]) * std::tanh(gates[256 + i]);
    next.hidden[i] = Sigmoid(gates[384 + i]) * std::tanh(next.cell[i]);
  }
  float logit = 0;
  for (size_t i = 0; i < 128; ++i)
    logit += std::max(0.f, next.hidden[i]) * weight(13)[i];
  const float value = Sigmoid(logit + weight(14)[0]);
  if (!std::isfinite(value) || !Finite(next.hidden) || !Finite(next.cell))
    return Status::Fail("VAD model produced nonfinite output");
  if (cancelled && cancelled()) return Interrupted();
  std::copy(samples.end() - 64, samples.end(), next.context.begin());
  *state = next;
  *probability = value;
  return {};
}

Status SileroVad::Score(std::span<const float> pcm, size_t max_samples,
                        std::vector<VadFrame>* out, Cancelled cancelled) const {
  if (!out || !Ready() || pcm.empty() || pcm.size() > max_samples ||
      max_samples > 400ULL * 16000)
    return Status::Fail("VAD audio sample budget invalid");
  std::vector<VadFrame> frames;
  frames.reserve((pcm.size() + 511) / 512);
  VadState state;
  for (size_t i = 0; i < pcm.size(); i += 512) {
    const auto count = std::min(size_t{512}, pcm.size() - i);
    std::array<float, 512> samples{};
    std::copy_n(pcm.data() + i, count, samples.begin());
    float probability;
    if (auto s = Step(samples, &state, &probability, cancelled); !s) return s;
    frames.push_back({i, i + count, probability});
  }
  if (cancelled && cancelled()) return Interrupted();
  *out = std::move(frames);
  return {};
}
}  // namespace q4t::audio
