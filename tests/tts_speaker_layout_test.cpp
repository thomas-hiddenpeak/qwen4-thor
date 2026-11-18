#include "q4t/audio/tts_speaker_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>

namespace {
using namespace q4t::audio;
constexpr auto kUnlimited = std::numeric_limits<size_t>::max();
constexpr uintptr_t kBase = 0x10000000;
q4t::io::Json Header() {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/tts/base-17b-header.json");
  std::ostringstream text;
  text << file.rdbuf();
  q4t::io::Json header;
  if (!file || !q4t::io::ParseJson(text.str(), &header, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error("TTS speaker independent header missing");
  return header;
}
TtsWeightLayout Layout() {
  TtsWeightLayout result;
  if (!ParseTtsSpeakerWeightLayout(Header(), TtsSpeakerConfig::kBaseDataBytes,
                                   &result))
    throw std::runtime_error("TTS speaker fixture layout rejected");
  return result;
}
}  // namespace

Q4T_TEST(tts_speaker_layout_selects_complete_independent_speaker_graph) {
  const auto header = Header();
  const auto layout = Layout();
  Q4T_CHECK(ValidateTtsSpeakerWeightLayout(layout));
  Q4T_CHECK(layout.tensors.size() == 76 && layout.bytes == 24002176);
  size_t total = 0;
  for (const auto& t : layout.tensors) {
    Q4T_CHECK(t.name.starts_with("speaker_encoder."));
    const auto* offsets = header.Find(t.name)->GetArray("data_offsets");
    Q4T_CHECK(t.file_offset ==
              static_cast<size_t>(offsets->array[0].AsInt(-1)));
    Q4T_CHECK(t.bytes ==
              static_cast<size_t>(offsets->array[1].AsInt(-1)) - t.file_offset);
    Q4T_CHECK(t.offset == total);
    total += t.bytes;
  }
  Q4T_CHECK(total == layout.bytes);
  return true;
}

Q4T_TEST(tts_speaker_layout_rejects_changed_compact_tensor_contracts) {
  const auto valid = Layout();
  for (int mutation = 0; mutation < 7; ++mutation) {
    auto value = valid;
    if (mutation == 0) value.tensors.pop_back();
    if (mutation == 1) value.tensors.back().name = "talker.fc.weight";
    if (mutation == 2) value.tensors.back().bytes -= 2;
    if (mutation == 3) value.tensors.back().offset -= 2;
    if (mutation == 4) value.bytes++;
    if (mutation == 5) value.file_bytes++;
    if (mutation == 6) std::swap(value.tensors[1], value.tensors[2]);
    Q4T_CHECK(!ValidateTtsSpeakerWeightLayout(value));
  }
  return true;
}

Q4T_TEST(tts_speaker_layout_rejects_source_alias_gaps_and_overflow) {
  const auto valid = Layout();
  for (const size_t at : {valid.tensors[1].file_offset, size_t{1},
                          TtsSpeakerConfig::kSourceBytes, kUnlimited}) {
    auto value = valid;
    value.tensors[0].file_offset = at;
    Q4T_CHECK(!ValidateTtsSpeakerWeightLayout(value));
  }
  auto value = valid;
  value.tensors[0].file_offset += 2;
  Q4T_CHECK(!ValidateTtsSpeakerWeightLayout(value));
  return true;
}

Q4T_TEST(tts_speaker_layout_invalid_full_header_keeps_prior_owner) {
  auto out = Layout();
  const auto bytes = out.bytes;
  const auto first = out.tensors.front().name;
  Q4T_CHECK(!ParseTtsSpeakerWeightLayout(
      Header(), TtsSpeakerConfig::kBaseDataBytes - 2, &out));
  auto header = Header();
  header.object.pop_back();
  Q4T_CHECK(!ParseTtsSpeakerWeightLayout(
      header, TtsSpeakerConfig::kBaseDataBytes, &out));
  Q4T_CHECK(!ParseTtsSpeakerWeightLayout(
      Header(), TtsSpeakerConfig::kBaseDataBytes, nullptr));
  Q4T_CHECK(out.bytes == bytes && out.tensors.front().name == first &&
            out.tensors.size() == 76);
  return true;
}

Q4T_TEST(tts_speaker_binding_maps_every_trained_affine_and_branch) {
  const auto layout = Layout();
  TtsSpeakerDeviceWeights w;
  Q4T_CHECK(BindTtsSpeakerDeviceWeights(reinterpret_cast<void*>(kBase),
                                        TtsSpeakerConfig::kDeviceBytes, layout,
                                        &w));
  std::map<std::string, TtsAffineWeights> found = {
      {"blocks.0.conv", w.input},
      {"mfa.conv", w.aggregate},
      {"asp.tdnn.conv", w.attention},
      {"asp.conv", w.attention_output},
      {"fc", w.output}};
  for (size_t i = 0; i < w.layers.size(); ++i) {
    const auto prefix = "blocks." + std::to_string(i + 1) + ".";
    const auto& layer = w.layers[i];
    found.emplace(prefix + "tdnn1.conv", layer.before);
    found.emplace(prefix + "tdnn2.conv", layer.after);
    found.emplace(prefix + "se_block.conv1", layer.squeeze_first);
    found.emplace(prefix + "se_block.conv2", layer.squeeze_second);
    for (size_t j = 0; j < layer.branches.size(); ++j)
      found.emplace(
          prefix + "res2net_block.blocks." + std::to_string(j) + ".conv",
          layer.branches[j]);
  }
  Q4T_CHECK(found.size() == 38);
  for (const auto& t : layout.tensors) {
    const bool bias = t.name.ends_with(".bias");
    const auto name = t.name.substr(16, t.name.size() - 16 - (bias ? 5 : 7));
    Q4T_CHECK(found.contains(name));
    const auto actual = bias ? found.at(name).bias : found.at(name).weight;
    Q4T_CHECK(reinterpret_cast<uintptr_t>(actual) == kBase + t.offset * 2);
  }
  return true;
}

Q4T_TEST(tts_speaker_binding_rejects_bad_memory_without_publication) {
  const auto layout = Layout();
  TtsSpeakerDeviceWeights out;
  out.input.weight = reinterpret_cast<float*>(kBase);
  for (const auto address :
       {uintptr_t{0}, kBase + 1, std::numeric_limits<uintptr_t>::max() - 3}) {
    Q4T_CHECK(!BindTtsSpeakerDeviceWeights(reinterpret_cast<void*>(address),
                                           TtsSpeakerConfig::kDeviceBytes,
                                           layout, &out));
    Q4T_CHECK(reinterpret_cast<uintptr_t>(out.input.weight) == kBase);
    Q4T_CHECK(out.output.weight == nullptr);
  }
  Q4T_CHECK(!BindTtsSpeakerDeviceWeights(reinterpret_cast<void*>(kBase),
                                         TtsSpeakerConfig::kDeviceBytes - 4,
                                         layout, &out));
  Q4T_CHECK(!BindTtsSpeakerDeviceWeights(reinterpret_cast<void*>(kBase),
                                         TtsSpeakerConfig::kDeviceBytes, layout,
                                         nullptr));
  auto malformed = layout;
  malformed.tensors.back().offset--;
  Q4T_CHECK(!BindTtsSpeakerDeviceWeights(reinterpret_cast<void*>(kBase),
                                         TtsSpeakerConfig::kDeviceBytes,
                                         malformed, &out));
  Q4T_CHECK(out.output.weight == nullptr);
  return true;
}

Q4T_TEST(tts_speaker_workspace_bounds_reflection_duration_and_overflow) {
  TtsSpeakerWorkspace out;
  out.bytes = 17;
  for (const size_t frames : {size_t{0}, size_t{4}, size_t{5626}, kUnlimited}) {
    Q4T_CHECK(!PlanTtsSpeakerWorkspace(frames, kUnlimited, kUnlimited, &out));
    Q4T_CHECK(out.bytes == 17);
  }
  Q4T_CHECK(!PlanTtsSpeakerWorkspace(5, 4, kUnlimited, &out));
  Q4T_CHECK(!PlanTtsSpeakerWorkspace(5, 5, kUnlimited, nullptr));
  Q4T_CHECK(PlanTtsSpeakerWorkspace(5, 5, kUnlimited, &out));
  Q4T_CHECK(PlanTtsSpeakerWorkspace(5625, 5625, kUnlimited, &out));
  Q4T_CHECK(out.frames == 5625 && out.bytes < (300ULL << 20));
  return true;
}

Q4T_TEST(tts_speaker_workspace_covers_all_buffers_and_exact_ram_budget) {
  for (const size_t frames : {5, 1021, 5625}) {
    TtsSpeakerWorkspace w;
    Q4T_CHECK(PlanTtsSpeakerWorkspace(frames, frames, kUnlimited, &w));
    std::vector<std::pair<size_t, size_t>> intervals;
    const auto matrix = [&](size_t at, size_t channels) {
      intervals.emplace_back(at, at + channels * frames * 4);
    };
    for (auto [at, channels] : {std::pair{w.mel, 128},
                                {w.hidden, 512},
                                {w.temporary, 512},
                                {w.res2, 512},
                                {w.update, 512},
                                {w.history, 1536},
                                {w.aggregate, 1536},
                                {w.context, 4608},
                                {w.attention_hidden, 128},
                                {w.attention, 1536},
                                {w.columns, 640}})
      matrix(at, channels);
    for (auto [at, elements] : {std::pair{w.pool, 3072},
                                {w.squeeze, 512},
                                {w.squeeze_hidden, 128},
                                {w.squeeze_gate, 512},
                                {w.output, 2048},
                                {w.finite_flag, 1}})
      intervals.emplace_back(at, at + elements * 4);
    intervals.emplace_back(w.gemm, w.gemm + TtsSpeakerWorkspace::kGemmBytes);
    std::sort(intervals.begin(), intervals.end());
    size_t end = 0;
    for (auto [begin, next] : intervals) {
      Q4T_CHECK(begin % 256 == 0 && begin >= end && next <= w.bytes);
      end = next;
    }
    Q4T_CHECK(end == w.bytes && intervals.size() == 18);
    const auto bytes = w.bytes;
    Q4T_CHECK(!PlanTtsSpeakerWorkspace(frames, frames, bytes - 1, &w));
    Q4T_CHECK(w.bytes == bytes);
    Q4T_CHECK(PlanTtsSpeakerWorkspace(frames, frames, bytes, &w));
  }
  return true;
}
