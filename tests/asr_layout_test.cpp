#include "q4t/audio/asr_layout.h"
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
                     "fixtures/asr/tensor-header.json");
  std::ostringstream text;
  text << file.rdbuf();
  q4t::io::Json header;
  AsrWeightLayout layout;
  if (!file ||
      !q4t::io::ParseJson(text.str(), &header, false,
                          {.reject_duplicate_keys = true}) ||
      !ParseAsrWeightLayout(header, 1876017152, &layout))
    throw std::runtime_error("ASR layout fixture");
  return layout;
}
}  // namespace

Q4T_TEST(asr_layout_binds_all_stored_tensors_with_independent_heads) {
  const auto layout = Fixture();
  Q4T_CHECK(ValidateAsrWeightLayout(layout));
  const auto find = [&](const char* name) {
    return std::find_if(
        layout.tensors.begin(), layout.tensors.end(),
        [&](const auto& tensor) { return tensor.name == name; });
  };
  const auto input = find("thinker.model.embed_tokens.weight");
  const auto output = find("thinker.lm_head.weight");
  Q4T_CHECK(input != layout.tensors.end() && output != layout.tensors.end());
  Q4T_CHECK(input->offset != output->offset);
  Q4T_CHECK(input->bytes == 311164928 && output->bytes == input->bytes);
  Q4T_CHECK(layout.tensors.size() == 612);
  return true;
}

Q4T_TEST(asr_layout_rejects_forged_names_extents_and_overflow) {
  const auto original = Fixture();
  for (int kind = 0; kind < 8; ++kind) {
    auto layout = original;
    switch (kind) {
      case 0:
        layout.bytes--;
        break;
      case 1:
        layout.tensors.pop_back();
        break;
      case 2:
        layout.tensors[0].name = layout.tensors[1].name;
        break;
      case 3:
        layout.tensors[0].bytes++;
        break;
      case 4:
        layout.tensors[0].offset++;
        break;
      case 5:
        layout.tensors[0].offset = kUnlimited - 1;
        break;
      case 6:
        layout.tensors[0].offset = layout.tensors[1].offset;
        break;
      case 7:
        std::swap(layout.tensors[0], layout.tensors[1]);
        break;
    }
    Q4T_CHECK(!ValidateAsrWeightLayout(layout));
  }
  return true;
}

Q4T_TEST(asr_layout_parse_failure_preserves_verified_output) {
  auto layout = Fixture();
  const auto original = layout;
  q4t::io::Json invalid;
  Q4T_CHECK(!ParseAsrWeightLayout(invalid, original.bytes, &layout));
  Q4T_CHECK(layout.bytes == original.bytes &&
            layout.tensors.size() == original.tensors.size() &&
            layout.tensors.back().offset == original.tensors.back().offset);
  Q4T_CHECK(!ParseAsrWeightLayout(invalid, original.bytes, nullptr));
  return true;
}

Q4T_TEST(asr_encoder_layout_keeps_padding_and_104_position_windows) {
  struct Case {
    size_t frames;
    int padded, tokens, chunks, window;
  };
  const Case cases[] = {{1, 1, 1, 1, 8},         {8, 8, 1, 1, 8},
                        {9, 9, 2, 1, 16},        {99, 99, 13, 1, 104},
                        {100, 100, 13, 1, 104},  {101, 100, 14, 2, 104},
                        {199, 100, 26, 2, 104},  {800, 100, 104, 8, 104},
                        {801, 100, 105, 9, 104}, {3000, 100, 390, 30, 104}};
  for (const auto& c : cases) {
    AsrEncoderLayout layout;
    Q4T_CHECK(PlanAsrEncoderWorkspace(c.frames, 3000, kUnlimited, &layout));
    Q4T_CHECK(layout.padded_frames == c.padded && layout.tokens == c.tokens &&
              layout.chunks == c.chunks && layout.attention_window == c.window);
  }
  return true;
}

Q4T_TEST(asr_encoder_layout_accounts_every_aligned_buffer) {
  AsrEncoderLayout w;
  Q4T_CHECK(PlanAsrEncoderWorkspace(3000, 3000, kUnlimited, &w));
  // Independently bound each temporary by its actual consumer shape.
  std::array<std::pair<size_t, size_t>, 17> buffers{
      {{w.features, 128 * 3000 * 4},
       {w.columns, 32 * 25 * 480 * 9 * 2},
       {w.convolution_a, 64 * 50 * 480 * 2},
       {w.convolution_b, 32 * 25 * 480 * 2},
       {w.conv_flat, 13 * 480 * 16 * 2},
       {w.chunk_hidden, 13 * 896 * 2},
       {w.hidden, 390 * 896 * 2},
       {w.normalized, 390 * 896 * 2},
       {w.q, 390 * 896 * 2},
       {w.k, 390 * 896 * 2},
       {w.v, 390 * 896 * 2},
       {w.attention, 390 * 896 * 2},
       {w.projected, 390 * 896 * 2},
       {w.intermediate, 390 * 3584 * 2},
       {w.output, 390 * 1024 * 2},
       {w.linear_fp32, 64 * 50 * 480 * 4},
       {w.gemm, 32 * 1024 * 1024}}};
  size_t end = 0;
  for (const auto& [offset, size] : buffers) {
    Q4T_CHECK(offset % 256 == 0 && offset >= end);
    Q4T_CHECK(offset <= w.bytes && size <= w.bytes - offset);
    end = offset + size;
  }
  Q4T_CHECK(w.bytes == end && w.bytes < 128 * 1024 * 1024);
  return true;
}

Q4T_TEST(asr_encoder_layout_rejects_budget_and_index_overflow_atomically) {
  AsrEncoderLayout original;
  Q4T_CHECK(PlanAsrEncoderWorkspace(801, 3000, kUnlimited, &original));
  auto w = original;
  Q4T_CHECK(!PlanAsrEncoderWorkspace(801, 3000, original.bytes - 1, &w));
  Q4T_CHECK(!PlanAsrEncoderWorkspace(3001, 3000, kUnlimited, &w));
  Q4T_CHECK(!PlanAsrEncoderWorkspace(0, 3000, kUnlimited, &w));
  Q4T_CHECK(!PlanAsrEncoderWorkspace(kUnlimited, kUnlimited, kUnlimited, &w));
  Q4T_CHECK(!PlanAsrEncoderWorkspace(1, 1, kUnlimited, nullptr));
  Q4T_CHECK(w.bytes == original.bytes && w.frames == original.frames);
  Q4T_CHECK(PlanAsrEncoderWorkspace(801, 3000, original.bytes, &w));
  return true;
}

Q4T_TEST(asr_decoder_layout_contains_audio_prefix_and_full_output_head) {
  for (size_t tokens : {1, 16, 512, 8192}) {
    AsrDecoderLayout w;
    Q4T_CHECK(PlanAsrDecoderWorkspace(tokens, tokens, kUnlimited, &w));
    Q4T_CHECK(w.input >= w.ids + tokens * sizeof(int32_t));
    Q4T_CHECK(w.last >= w.input + tokens * 1024 * 2);
    Q4T_CHECK(w.logits >= w.last + 1024 * 2);
    Q4T_CHECK(w.dense_offset >= w.logits + 151936 * 2);
    Q4T_CHECK(w.dense_offset % 256 == 0 &&
              w.bytes == w.dense_offset + w.dense.bytes);
    Q4T_CHECK(w.dense.tokens == static_cast<int>(tokens));
  }
  return true;
}

Q4T_TEST(asr_decoder_layout_rejects_limits_without_replacing_output) {
  AsrDecoderLayout original;
  Q4T_CHECK(PlanAsrDecoderWorkspace(512, 8192, kUnlimited, &original));
  auto w = original;
  Q4T_CHECK(!PlanAsrDecoderWorkspace(512, 511, kUnlimited, &w));
  Q4T_CHECK(!PlanAsrDecoderWorkspace(512, 8192, original.bytes - 1, &w));
  Q4T_CHECK(!PlanAsrDecoderWorkspace(0, 8192, kUnlimited, &w));
  Q4T_CHECK(!PlanAsrDecoderWorkspace(8193, kUnlimited, kUnlimited, &w));
  Q4T_CHECK(!PlanAsrDecoderWorkspace(kUnlimited, kUnlimited, kUnlimited, &w));
  Q4T_CHECK(w.bytes == original.bytes && w.dense.tokens == 512);
  Q4T_CHECK(PlanAsrDecoderWorkspace(512, 8192, original.bytes, &w));
  return true;
}
