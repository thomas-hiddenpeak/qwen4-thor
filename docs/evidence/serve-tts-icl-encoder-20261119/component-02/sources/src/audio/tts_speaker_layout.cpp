#include "q4t/audio/tts_speaker_layout.h"

#include <algorithm>
#include <limits>
#include <map>
#include <utility>

namespace q4t::audio {
namespace {
std::vector<TtsTensorContract> SpeakerContracts() {
  auto tensors = TtsBaseTensorContracts();
  std::erase_if(tensors, [](const TtsTensorContract& tensor) {
    return !tensor.name.starts_with("speaker_encoder.");
  });
  return tensors;
}
}  // namespace

Status ParseTtsSpeakerWeightLayout(const io::Json& header, size_t data_bytes,
                                   TtsWeightLayout* out) {
  if (!out) return Status::Fail("TTS speaker layout owner is null");
  if (auto s = ValidateTtsBaseTensorHeader(header, data_bytes); !s) return s;
  TtsWeightLayout result;
  result.file_bytes = data_bytes;
  for (const auto& spec : SpeakerContracts()) {
    const auto begin =
        header.Find(spec.name)->GetArray("data_offsets")->array[0].AsInt(-1);
    result.tensors.push_back(
        {spec.name, static_cast<size_t>(begin), result.bytes, spec.bytes});
    result.bytes += spec.bytes;
  }
  if (auto s = ValidateTtsSpeakerWeightLayout(result); !s) return s;
  *out = std::move(result);
  return {};
}

Status ValidateTtsSpeakerWeightLayout(const TtsWeightLayout& layout) {
  const auto specs = SpeakerContracts();
  if (layout.file_bytes != TtsSpeakerConfig::kBaseDataBytes ||
      layout.bytes != TtsSpeakerConfig::kSourceBytes ||
      layout.tensors.size() != specs.size() || specs.size() != 76)
    return Status::Fail("TTS speaker layout storage differs");
  size_t offset = 0;
  std::vector<std::pair<size_t, size_t>> ranges;
  for (size_t i = 0; i < specs.size(); ++i) {
    const auto& t = layout.tensors[i];
    if (t.name != specs[i].name || t.bytes != specs[i].bytes ||
        t.offset != offset || t.file_offset % 2 ||
        t.file_offset > TtsSpeakerConfig::kSourceBytes ||
        t.bytes > TtsSpeakerConfig::kSourceBytes - t.file_offset)
      return Status::Fail("TTS speaker tensor extent differs");
    ranges.emplace_back(t.file_offset, t.file_offset + t.bytes);
    offset += t.bytes;
  }
  std::sort(ranges.begin(), ranges.end());
  size_t end = 0;
  for (const auto& [begin, next] : ranges) {
    if (begin != end) return Status::Fail("TTS speaker source overlap or gap");
    end = next;
  }
  if (end != TtsSpeakerConfig::kSourceBytes || offset != layout.bytes)
    return Status::Fail("TTS speaker tensor graph size differs");
  return {};
}

Status BindTtsSpeakerDeviceWeights(const void* data, size_t bytes,
                                   const TtsWeightLayout& layout,
                                   TtsSpeakerDeviceWeights* out) {
  const auto base = reinterpret_cast<uintptr_t>(data);
  if (!data || !out || base % alignof(float) ||
      bytes != TtsSpeakerConfig::kDeviceBytes ||
      base > std::numeric_limits<uintptr_t>::max() - bytes)
    return Status::Fail("TTS speaker device storage invalid");
  if (auto s = ValidateTtsSpeakerWeightLayout(layout); !s) return s;
  std::map<std::string, const float*> values;
  for (const auto& tensor : layout.tensors)
    values.emplace(tensor.name,
                   reinterpret_cast<const float*>(base + tensor.offset * 2));
  const auto affine = [&](const std::string& name) {
    const auto path = "speaker_encoder." + name;
    return TtsAffineWeights{values.at(path + ".weight"),
                            values.at(path + ".bias")};
  };
  TtsSpeakerDeviceWeights result;
  result.input = affine("blocks.0.conv");
  for (size_t i = 0; i < result.layers.size(); ++i) {
    const auto prefix = "blocks." + std::to_string(i + 1) + ".";
    auto& layer = result.layers[i];
    layer.before = affine(prefix + "tdnn1.conv");
    layer.after = affine(prefix + "tdnn2.conv");
    for (size_t j = 0; j < layer.branches.size(); ++j)
      layer.branches[j] = affine(prefix + "res2net_block.blocks." +
                                 std::to_string(j) + ".conv");
    layer.squeeze_first = affine(prefix + "se_block.conv1");
    layer.squeeze_second = affine(prefix + "se_block.conv2");
  }
  result.aggregate = affine("mfa.conv");
  result.attention = affine("asp.tdnn.conv");
  result.attention_output = affine("asp.conv");
  result.output = affine("fc");
  *out = result;
  return {};
}

Status PlanTtsSpeakerWorkspace(size_t frames, size_t max_frames,
                               size_t max_bytes, TtsSpeakerWorkspace* out) {
  if (!out || frames < TtsSpeakerConfig::kMinFrames || frames > max_frames ||
      frames > TtsSpeakerConfig::kMaxFrames)
    return Status::Fail("TTS speaker frames exceed reflection or budget");
  TtsSpeakerWorkspace layout;
  layout.frames = static_cast<int>(frames);
  const auto carve = [&](size_t bytes) {
    const size_t start = (layout.bytes + 255) / 256 * 256;
    layout.bytes = start + bytes;
    return start;
  };
  // At most 5625 frames; all intermediate products fit 32-bit size_t.
  const auto matrix = [&](size_t channels) {
    return carve(channels * frames * sizeof(float));
  };
  layout.mel = matrix(128);
  layout.hidden = matrix(512);
  layout.temporary = matrix(512);
  layout.res2 = matrix(512);
  layout.update = matrix(512);
  layout.history = matrix(1536);
  layout.aggregate = matrix(1536);
  layout.context = matrix(4608);
  layout.attention_hidden = matrix(128);
  layout.attention = matrix(1536);
  layout.columns = matrix(640);
  layout.pool = carve(3072 * sizeof(float));
  layout.squeeze = carve(512 * sizeof(float));
  layout.squeeze_hidden = carve(128 * sizeof(float));
  layout.squeeze_gate = carve(512 * sizeof(float));
  layout.output = carve(2048 * sizeof(float));
  layout.gemm = carve(TtsSpeakerWorkspace::kGemmBytes);
  layout.finite_flag = carve(sizeof(int));
  if (layout.bytes > max_bytes)
    return Status::Fail("TTS speaker physical RAM budget exceeded");
  *out = layout;
  return {};
}
}  // namespace q4t::audio
