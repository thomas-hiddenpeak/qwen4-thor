#include "q4t/audio/separator_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <limits>
#include <map>

namespace {
using namespace q4t::audio;
constexpr uintptr_t kBase = 0x10000000;
SeparatorTensorLayout Canonical() {
  SeparatorTensorLayout layout;
  const auto specs = SeparatorTensorContracts();
  size_t offset = 0;
  for (size_t i = 0; i < specs.size(); ++i) {
    layout.offsets[i] = offset;
    offset += specs[i].bytes;
  }
  return layout;
}
bool Bound(const SeparatorTensorLayout& layout, SeparatorDeviceWeights* out,
           uintptr_t base = kBase,
           size_t bytes = SeparatorConfig::kWeightBytes) {
  return BindSeparatorDeviceWeights(reinterpret_cast<const void*>(base), bytes,
                                    layout, out)
      .ok();
}
}  // namespace

Q4T_TEST(separator_binding_complete_original_tensor_map) {
  const auto layout = Canonical();
  SeparatorDeviceWeights weights;
  Q4T_CHECK(Bound(layout, &weights));
  std::map<std::string, uintptr_t> address;
  const auto specs = SeparatorTensorContracts();
  for (size_t i = 0; i < specs.size(); ++i)
    address[specs[i].name] = kBase + layout.offsets[i];
  const auto equal = [&](const float* value, const std::string& name) {
    return reinterpret_cast<uintptr_t>(value) == address.at(name);
  };
  Q4T_CHECK(equal(weights.encoder, "encoder.conv1d.weight"));
  Q4T_CHECK(equal(weights.decoder, "decoder.weight"));
  Q4T_CHECK(equal(weights.sources.weight, "masknet.conv2d.weight"));
  Q4T_CHECK(equal(weights.gate.bias, "masknet.output_gate.0.bias"));
  for (size_t p = 0; p < 4; ++p) {
    const auto prefix = "masknet.dual_mdl." + std::to_string(p / 2) + "." +
                        (p % 2 ? "inter" : "intra");
    const auto& path = weights.paths[p];
    Q4T_CHECK(equal(path.position, prefix + "_mdl.pos_enc.pe"));
    Q4T_CHECK(equal(path.group_norm.weight, prefix + "_norm.weight"));
    for (size_t i = 0; i < 8; ++i) {
      const auto name = prefix + "_mdl.mdl.layers." + std::to_string(i);
      Q4T_CHECK(equal(path.layers[i].qkv.weight,
                      name + ".self_att.att.in_proj_weight"));
      Q4T_CHECK(equal(path.layers[i].norm2.bias, name + ".norm2.norm.bias"));
      Q4T_CHECK(
          equal(path.layers[i].ffn2.weight, name + ".pos_ffn.ffn.3.weight"));
    }
  }
  return true;
}

Q4T_TEST(separator_binding_rejects_bad_layout_atomically) {
  const auto original = Canonical();
  for (size_t index : {size_t{0}, size_t{206}, size_t{416}}) {
    for (size_t delta : {size_t{1}, size_t{256}}) {
      auto bad = original;
      bad.offsets[index] += delta;
      SeparatorDeviceWeights weights;
      weights.encoder = reinterpret_cast<const float*>(123);
      Q4T_CHECK(!Bound(bad, &weights));
      Q4T_CHECK(reinterpret_cast<uintptr_t>(weights.encoder) == 123);
    }
  }
  return true;
}

Q4T_TEST(separator_binding_rejects_pointer_extent_overflow) {
  const auto layout = Canonical();
  SeparatorDeviceWeights weights;
  for (uintptr_t base :
       {uintptr_t{0}, kBase + 1, std::numeric_limits<uintptr_t>::max() - 3})
    Q4T_CHECK(!Bound(layout, &weights, base));
  Q4T_CHECK(!Bound(layout, nullptr));
  Q4T_CHECK(!Bound(layout, &weights, kBase, 112957443));
  Q4T_CHECK(!Bound(layout, &weights, kBase, 112957445));
  return true;
}

Q4T_TEST(separator_workspace_convolution_and_chunk_tail_boundaries) {
  for (const auto samples : {16, 17, 23, 24, 1008, 2008, 64000}) {
    SeparatorWorkspaceLayout layout;
    Q4T_CHECK(PlanSeparatorWorkspace(samples, 64000, 1ULL << 30, &layout));
    Q4T_CHECK(layout.frames == (samples - 16) / 8 + 1);
    Q4T_CHECK(layout.decoded_samples <= samples);
    Q4T_CHECK(samples - layout.decoded_samples < 8);
    Q4T_CHECK(layout.gap >= 1 && layout.gap <= 250);
    Q4T_CHECK(layout.segments % 2 == 0);
    Q4T_CHECK(layout.rows == layout.segments * 250);
    // Every real encoder frame is represented twice in the half-overlap
    // tensor, including first/last frames and the exact chunk boundary.
    std::vector<int> coverage(layout.frames);
    for (int s = 0; s < layout.segments; ++s)
      for (int k = 0; k < 250; ++k) {
        const int at = s * 125 + k - 125;
        if (at >= 0 && at < layout.frames) ++coverage[at];
      }
    Q4T_CHECK(std::all_of(coverage.begin(), coverage.end(),
                          [](int n) { return n == 2; }));
  }
  return true;
}

Q4T_TEST(separator_workspace_disjoint_full_extent_and_exact_budget) {
  for (size_t samples : {size_t{16}, size_t{257}, size_t{64000}}) {
    SeparatorWorkspaceLayout l;
    Q4T_CHECK(PlanSeparatorWorkspace(samples, 64000, 1ULL << 30, &l));
    const size_t f = l.frames, r = l.rows;
    const std::pair<size_t, size_t> buffers[] = {
        {l.pcm, samples * 4},
        {l.encoded, f * 256 * 4},
        {l.normalized, f * 256 * 4},
        {l.projected, f * 256 * 4},
        {l.hidden, r * 256 * 4},
        {l.path, r * 256 * 4},
        {l.norm, r * 256 * 4},
        {l.qkv, r * 768 * 4},
        {l.scores, 8 * r * std::max(250, l.segments) * 4},
        {l.attention, r * 256 * 4},
        {l.ffn, r * 1024 * 4},
        {l.update, r * 256 * 4},
        {l.source_chunks, r * 512 * 4},
        {l.joined, 2 * f * 256 * 4},
        {l.output_hidden, 2 * f * 256 * 4},
        {l.gate, 2 * f * 256 * 4},
        {l.masks, 2 * f * 256 * 4},
        {l.separated, 2 * samples * 4},
        {l.partial, size_t(l.reduction_blocks) * 4},
        {l.statistics, 8},
        {l.gemm, SeparatorWorkspaceLayout::kGemmBytes},
        {l.finite_flag, 4}};
    size_t end = 0;
    for (const auto& [begin, bytes] : buffers) {
      Q4T_CHECK(begin % 256 == 0 && begin >= end && bytes > 0);
      end = begin + bytes;
    }
    Q4T_CHECK(end == l.bytes && l.bytes < (512ULL << 20));
    SeparatorWorkspaceLayout same;
    Q4T_CHECK(PlanSeparatorWorkspace(samples, samples, l.bytes, &same));
    Q4T_CHECK(same.bytes == l.bytes);
    Q4T_CHECK(!PlanSeparatorWorkspace(samples, samples, l.bytes - 1, &same));
  }
  return true;
}

Q4T_TEST(separator_workspace_invalid_limits_preserve_owner) {
  SeparatorWorkspaceLayout layout;
  layout.samples = 777;
  for (size_t samples : {size_t{0}, size_t{15}, size_t{64001},
                         std::numeric_limits<size_t>::max()}) {
    Q4T_CHECK(!PlanSeparatorWorkspace(samples, SIZE_MAX, SIZE_MAX, &layout));
    Q4T_CHECK(layout.samples == 777);
  }
  Q4T_CHECK(!PlanSeparatorWorkspace(16, 15, SIZE_MAX, &layout));
  Q4T_CHECK(!PlanSeparatorWorkspace(16, 16, 0, &layout));
  Q4T_CHECK(!PlanSeparatorWorkspace(16, 16, SIZE_MAX, nullptr));
  Q4T_CHECK(layout.samples == 777);
  return true;
}
