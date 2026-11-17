#include "q4t/audio/aligner_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace {
using namespace q4t::audio;
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();

AsrWeightLayout Fixture() {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/aligner/tensor-header.json");
  std::ostringstream text;
  text << file.rdbuf();
  q4t::io::Json header;
  AsrWeightLayout layout;
  if (!file || !q4t::io::ParseJson(text.str(), &header) ||
      !ParseAlignerWeightLayout(header, 1835457792, &layout))
    throw std::runtime_error("alignment layout fixture");
  return layout;
}
}  // namespace

Q4T_TEST(aligner_layout_validates_actual_weight_ranges_and_family) {
  const auto layout = Fixture();
  Q4T_CHECK(ValidateAlignerWeightLayout(layout));
  Q4T_CHECK(!ValidateAsrWeightLayout(layout));
  Q4T_CHECK(layout.bytes == 1835457792 && layout.tensors.size() == 708);
  const auto head = std::find_if(
      layout.tensors.begin(), layout.tensors.end(),
      [](const auto& value) { return value.name == "thinker.lm_head.weight"; });
  Q4T_CHECK(head != layout.tensors.end() && head->bytes == 10240000);
  for (int kind = 0; kind < 5; ++kind) {
    auto changed = layout;
    if (kind == 0) changed.tensors[0].offset = kUnlimited - 1;
    if (kind == 1) changed.tensors[0].offset = changed.tensors[1].offset;
    if (kind == 2) changed.tensors[0].bytes += 2;
    if (kind == 3) changed.tensors.pop_back();
    if (kind == 4) changed.bytes++;
    Q4T_CHECK(!ValidateAlignerWeightLayout(changed));
  }
  auto unchanged = layout;
  Q4T_CHECK(!ParseAlignerWeightLayout({}, 1835457792, &unchanged));
  Q4T_CHECK(unchanged.bytes == layout.bytes &&
            unchanged.tensors.size() == layout.tensors.size());
  return true;
}

Q4T_TEST(aligner_encoder_layout_covers_24_layer_width_and_tail_windows) {
  AsrEncoderLayout w;
  Q4T_CHECK(PlanAlignerEncoderWorkspace(101, 101, kUnlimited, &w));
  Q4T_CHECK(w.chunks == 2 && w.tokens == 14 && w.attention_window == 104);
  // Each interval has enough space for its actual mathematical shape, and
  // none overlaps its successor. Dimensions come from the pinned checkpoint.
  const std::array<std::pair<size_t, size_t>, 19> intervals{
      {{w.features, 101 * 128 * 4},
       {w.columns, 32 * 25 * 480 * 9 * 2},
       {w.convolution_a, 64 * 50 * 480 * 2},
       {w.convolution_b, 32 * 25 * 480 * 2},
       {w.conv_flat, 13 * 480 * 16 * 2},
       {w.chunk_hidden, 13 * 1024 * 2},
       {w.hidden, 14 * 1024 * 2},
       {w.normalized, 14 * 1024 * 2},
       {w.q, 14 * 1024 * 2},
       {w.k, 14 * 1024 * 2},
       {w.v, 14 * 1024 * 2},
       {w.attention, 14 * 1024 * 2},
       {w.projected, 14 * 1024 * 2},
       {w.intermediate, 14 * 4096 * 2},
       {w.output, 14 * 1024 * 2},
       {w.linear_fp32, 64 * 50 * 480 * 4},
       {w.gemm, AsrEncoderLayout::kGemmBytes},
       {w.bytes, 0},
       {w.bytes, 0}}};
  for (size_t i = 0; i + 1 < intervals.size(); ++i) {
    Q4T_CHECK(intervals[i].first % 256 == 0);
    Q4T_CHECK(intervals[i].first + intervals[i].second <=
              intervals[i + 1].first);
  }
  Q4T_CHECK(PlanAlignerEncoderWorkspace(1, 1, kUnlimited, &w));
  Q4T_CHECK(w.tokens == 1 && w.attention_window == 8);
  return true;
}

Q4T_TEST(aligner_decoder_layout_reserves_all_slots_and_enforces_budgets) {
  AlignerDecoderLayout w;
  Q4T_CHECK(PlanAlignerDecoderWorkspace(17, 4, 17, kUnlimited, &w));
  Q4T_CHECK(w.dense.tokens == 17 && w.slots == 4);
  Q4T_CHECK(w.ids + 17 * 4 <= w.input);
  Q4T_CHECK(w.input + 17 * 1024 * 2 <= w.positions);
  Q4T_CHECK(w.positions + 4 * 4 <= w.selected);
  Q4T_CHECK(w.selected + 4 * 1024 * 2 <= w.logits);
  Q4T_CHECK(w.logits + 4 * 5000 * 2 <= w.dense_offset);
  Q4T_CHECK(w.bytes == w.dense_offset + w.dense.bytes);
  const auto bytes = w.bytes;
  Q4T_CHECK(!PlanAlignerDecoderWorkspace(17, 4, 17, bytes - 1, &w));
  Q4T_CHECK(w.bytes == bytes && w.slots == 4);
  Q4T_CHECK(!PlanAlignerDecoderWorkspace(17, 3, 17, kUnlimited, &w));
  Q4T_CHECK(!PlanAlignerDecoderWorkspace(17, 18, 17, kUnlimited, &w));
  Q4T_CHECK(!PlanAlignerDecoderWorkspace(17, 4, 16, kUnlimited, &w));
  Q4T_CHECK(!PlanAlignerDecoderWorkspace(8193, 2, 8193, kUnlimited, &w));
  Q4T_CHECK(
      !PlanAlignerDecoderWorkspace(kUnlimited, 2, kUnlimited, kUnlimited, &w));
  return true;
}

Q4T_TEST(aligner_sequence_and_encoder_reject_malformed_bounds_atomically) {
  std::vector<int32_t> positions{999};
  const std::vector<int32_t> tokens{151669, 151676, 151670, 9707,  151705,
                                    151705, 14957,  151705, 151705};
  Q4T_CHECK(PlanAlignerSequence(tokens, 1, &positions));
  Q4T_CHECK(positions == std::vector<int32_t>({4, 5, 7, 8}));
  for (size_t at = 0; at < tokens.size(); ++at) {
    auto changed = tokens;
    changed[at] = -1;
    Q4T_CHECK(!PlanAlignerSequence(changed, 1, &positions));
    Q4T_CHECK(positions == std::vector<int32_t>({4, 5, 7, 8}));
  }
  auto changed = tokens;
  changed[3] = 151705;  // Cannot create a word from an injected time marker.
  Q4T_CHECK(!PlanAlignerSequence(changed, 1, &positions));
  changed = tokens;
  changed.pop_back();
  Q4T_CHECK(!PlanAlignerSequence(changed, 1, &positions));
  Q4T_CHECK(!PlanAlignerSequence(tokens, kUnlimited, &positions));
  Q4T_CHECK(!PlanAlignerSequence(tokens, 1, nullptr));
  AsrEncoderLayout w;
  Q4T_CHECK(PlanAlignerEncoderWorkspace(101, 101, kUnlimited, &w));
  const auto bytes = w.bytes;
  Q4T_CHECK(!PlanAlignerEncoderWorkspace(101, 101, bytes - 1, &w));
  Q4T_CHECK(!PlanAlignerEncoderWorkspace(101, 100, kUnlimited, &w));
  Q4T_CHECK(
      !PlanAlignerEncoderWorkspace(kUnlimited, kUnlimited, kUnlimited, &w));
  Q4T_CHECK(!PlanAlignerEncoderWorkspace(0, 1, kUnlimited, &w));
  Q4T_CHECK(w.bytes == bytes && w.frames == 101);
  return true;
}
