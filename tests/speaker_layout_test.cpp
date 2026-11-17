#include "q4t/audio/speaker_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <vector>

namespace {
using namespace q4t::audio;
using q4t::io::Json;
Json Header() {
  std::ifstream input(std::filesystem::path(__FILE__).parent_path() /
                      "fixtures/speaker/tensor-header.json");
  std::ostringstream text;
  text << input.rdbuf();
  Json value;
  if (!input || !q4t::io::ParseJson(text.str(), &value))
    throw std::runtime_error("speaker header fixture");
  return value;
}
SpeakerTensorLayout Layout() {
  SpeakerTensorLayout layout;
  if (!ParseSpeakerTensorLayout(Header(), SpeakerConfig::kWeightBytes, &layout))
    throw std::runtime_error("speaker layout fixture");
  return layout;
}
constexpr uintptr_t kBase = 0x100000;
const void* Base(uintptr_t offset = 0) {
  // Address arithmetic only: these CPU contracts never dereference GPU memory.
  return reinterpret_cast<const void*>(kBase + offset);
}
std::vector<std::pair<std::string, const float*>> Pointers(
    const SpeakerDeviceWeights& w) {
  std::vector<std::pair<std::string, const float*>> rows;
  const auto affine = [&](const std::string& name, SpeakerAffineWeights a) {
    rows.emplace_back(name + ".conv.weight", a.weight);
    rows.emplace_back(name + ".conv.bias", a.bias);
  };
  const auto norm = [&](const std::string& name, SpeakerNormWeights n) {
    rows.emplace_back(name + ".norm.weight", n.weight);
    rows.emplace_back(name + ".norm.bias", n.bias);
    rows.emplace_back(name + ".norm.running_mean", n.mean);
    rows.emplace_back(name + ".norm.running_var", n.variance);
  };
  const auto tdnn = [&](const std::string& name, SpeakerTdnnWeights t) {
    affine(name + ".conv", t.affine);
    norm(name + ".norm", t.norm);
  };
  tdnn("blocks.0", w.input);
  for (size_t i = 0; i < w.layers.size(); ++i) {
    const auto name = "blocks." + std::to_string(i + 1);
    const auto& layer = w.layers[i];
    tdnn(name + ".tdnn1", layer.before);
    tdnn(name + ".tdnn2", layer.after);
    for (size_t j = 0; j < layer.branches.size(); ++j)
      tdnn(name + ".res2net_block.blocks." + std::to_string(j),
           layer.branches[j]);
    affine(name + ".se_block.conv1", layer.se_first);
    affine(name + ".se_block.conv2", layer.se_second);
  }
  tdnn("mfa", w.aggregate);
  tdnn("asp.tdnn", w.attention);
  affine("asp.conv", w.attention_output);
  norm("asp_bn", w.pool_norm);
  affine("fc", w.output);
  return rows;
}
}  // namespace

Q4T_TEST(speaker_binding_matches_all_actual_float_tensor_offsets) {
  SpeakerDeviceWeights weights;
  Q4T_CHECK(BindSpeakerDeviceWeights(Base(), SpeakerConfig::kWeightBytes,
                                     Layout(), &weights));
  const auto rows = Pointers(weights);
  Q4T_CHECK(rows.size() == 200);
  auto header = Header();
  for (const auto& [name, pointer] : rows) {
    const auto* tensor = header.Find(name);
    Q4T_CHECK(tensor && tensor->GetString("dtype") == "F32");
    const auto* offsets = tensor->GetArray("data_offsets");
    Q4T_CHECK(offsets && offsets->array.size() == 2);
    Q4T_CHECK(reinterpret_cast<uintptr_t>(pointer) ==
              kBase + static_cast<size_t>(offsets->array.front().number));
    Q4T_CHECK(reinterpret_cast<uintptr_t>(pointer) % alignof(float) == 0);
    header.object.erase(
        std::find_if(header.object.begin(), header.object.end(),
                     [&](const auto& member) { return member.first == name; }));
  }
  Q4T_CHECK(header.object.size() == 31);
  for (const auto& [name, tensor] : header.object)
    Q4T_CHECK(name.ends_with("num_batches_tracked") &&
              tensor.GetString("dtype") == "I64");
  return true;
}

Q4T_TEST(speaker_binding_relocation_changes_every_tensor_by_exact_base) {
  SpeakerDeviceWeights first, second;
  Q4T_CHECK(BindSpeakerDeviceWeights(Base(), SpeakerConfig::kWeightBytes,
                                     Layout(), &first));
  constexpr uintptr_t kMove = 1ULL << 34;
  Q4T_CHECK(BindSpeakerDeviceWeights(Base(kMove), SpeakerConfig::kWeightBytes,
                                     Layout(), &second));
  const auto a = Pointers(first), b = Pointers(second);
  Q4T_CHECK(a.size() == b.size());
  for (size_t i = 0; i < a.size(); ++i) {
    Q4T_CHECK(a[i].first == b[i].first);
    Q4T_CHECK(reinterpret_cast<uintptr_t>(b[i].second) -
                  reinterpret_cast<uintptr_t>(a[i].second) ==
              kMove);
  }
  return true;
}

Q4T_TEST(speaker_binding_rejects_null_truncated_unaligned_and_overflow) {
  SpeakerDeviceWeights weights;
  weights.output.weight = reinterpret_cast<const float*>(kBase);
  const auto unchanged = [&] { return weights.output.weight == Base(); };
  for (size_t bytes : {size_t{0}, size_t{SpeakerConfig::kWeightBytes - 1},
                       size_t{SpeakerConfig::kWeightBytes + 1},
                       std::numeric_limits<size_t>::max()}) {
    Q4T_CHECK(!BindSpeakerDeviceWeights(Base(), bytes, Layout(), &weights));
    Q4T_CHECK(unchanged());
  }
  const void* pointers[] = {
      nullptr, Base(1),
      reinterpret_cast<void*>(std::numeric_limits<uintptr_t>::max() - 3)};
  for (const auto* pointer : pointers) {
    Q4T_CHECK(!BindSpeakerDeviceWeights(pointer, SpeakerConfig::kWeightBytes,
                                        Layout(), &weights));
    Q4T_CHECK(unchanged());
  }
  Q4T_CHECK(!BindSpeakerDeviceWeights(Base(), SpeakerConfig::kWeightBytes,
                                      Layout(), nullptr));
  return true;
}

Q4T_TEST(speaker_binding_each_changed_offset_preserves_prior_owner) {
  const auto original = Layout();
  for (size_t i = 0; i < original.offsets.size(); ++i) {
    auto layout = original;
    layout.offsets[i] += 4;
    SpeakerDeviceWeights weights;
    weights.output.weight = reinterpret_cast<const float*>(kBase);
    Q4T_CHECK(!BindSpeakerDeviceWeights(Base(), SpeakerConfig::kWeightBytes,
                                        layout, &weights));
    Q4T_CHECK(weights.output.weight == Base());
  }
  return true;
}

Q4T_TEST(speaker_workspace_buffers_do_not_overlap_at_shape_boundaries) {
  for (size_t frames : {5, 6, 257, 3001}) {
    SpeakerWorkspaceLayout w;
    Q4T_CHECK(PlanSpeakerWorkspace(frames, 3001, 300ULL << 20, &w));
    Q4T_CHECK(w.frames == static_cast<int>(frames));
    const size_t stride = frames * sizeof(float);
    const std::pair<size_t, size_t> intervals[] = {
        {w.mel, 80 * stride},
        {w.hidden, 1024 * stride},
        {w.temporary, 1024 * stride},
        {w.res2, 1024 * stride},
        {w.update, 1024 * stride},
        {w.history, 3072 * stride},
        {w.aggregate, 3072 * stride},
        {w.context, 9216 * stride},
        {w.attention_hidden, 128 * stride},
        {w.attention, 3072 * stride},
        {w.columns, 400 * stride},
        {w.pool, 6144 * sizeof(float)},
        {w.squeeze, 1024 * sizeof(float)},
        {w.squeeze_hidden, 128 * sizeof(float)},
        {w.squeeze_gate, 1024 * sizeof(float)},
        {w.output, 192 * sizeof(float)},
        {w.gemm, SpeakerWorkspaceLayout::kGemmBytes},
        {w.finite_flag, sizeof(int)}};
    size_t end = 0;
    for (const auto& [offset, bytes] : intervals) {
      Q4T_CHECK(offset % 256 == 0 && offset >= end && bytes > 0);
      Q4T_CHECK(offset - end < 256 && offset <= w.bytes - bytes);
      end = offset + bytes;
    }
    Q4T_CHECK(end == w.bytes);
    SpeakerWorkspaceLayout exact;
    Q4T_CHECK(PlanSpeakerWorkspace(frames, frames, w.bytes, &exact));
    Q4T_CHECK(exact.bytes == w.bytes);
  }
  return true;
}

Q4T_TEST(speaker_workspace_invalid_length_and_budget_preserve_prior_owner) {
  SpeakerWorkspaceLayout original;
  Q4T_CHECK(PlanSpeakerWorkspace(3001, 3001, 300ULL << 20, &original));
  for (size_t frames : {size_t{0}, size_t{1}, size_t{4}, size_t{3002},
                        std::numeric_limits<size_t>::max()}) {
    auto output = original;
    Q4T_CHECK(!PlanSpeakerWorkspace(frames, std::numeric_limits<size_t>::max(),
                                    300ULL << 20, &output));
    Q4T_CHECK(output.bytes == original.bytes && output.frames == 3001);
  }
  for (size_t max_frames : {0, 4, 3000}) {
    auto output = original;
    Q4T_CHECK(!PlanSpeakerWorkspace(3001, max_frames, original.bytes, &output));
    Q4T_CHECK(output.frames == 3001 && output.bytes == original.bytes);
  }
  auto output = original;
  Q4T_CHECK(!PlanSpeakerWorkspace(3001, 3001, original.bytes - 1, &output));
  Q4T_CHECK(output.frames == 3001 && output.bytes == original.bytes);
  Q4T_CHECK(!PlanSpeakerWorkspace(3001, 3001, original.bytes, nullptr));
  return true;
}
