#include "q4t/audio/tts_codec_layout.h"
#include "q4t/test.h"

#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>

namespace {
using namespace q4t::audio;
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();
}

Q4T_TEST(tts_codec_binding_preserves_every_vocoder_stage) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/tts/codec-header.json");
  std::ostringstream text;
  text << file.rdbuf();
  q4t::io::Json header;
  Q4T_CHECK(file && q4t::io::ParseJson(text.str(), &header, false,
                                      {.reject_duplicate_keys = true}));
  TtsWeightLayout layout;
  Q4T_CHECK(ParseTtsCodecWeightLayout(header, TtsConfig::kCodecFileDataBytes,
                                      &layout));
  const void* base = reinterpret_cast<const void*>(uintptr_t{0x10000000});
  TtsCodecDeviceWeights w;
  Q4T_CHECK(BindTtsCodecDeviceWeights(base, layout.bytes, layout, &w));
  Q4T_CHECK(w.usage[15] && w.embedding_sum[15] && w.final_norm);
  Q4T_CHECK(w.layers[7].q && w.layers[7].mlp_scale);
  Q4T_CHECK(w.upsample[1].depthwise.groups == 1024);
  Q4T_CHECK(w.upsample[0].transpose.stride == 2);
  Q4T_CHECK(w.vocoder[0].transpose.kernel == 16);
  Q4T_CHECK(w.vocoder[1].transpose.stride == 5);
  Q4T_CHECK(w.vocoder[2].transpose.stride == 4);
  Q4T_CHECK(w.vocoder[3].transpose.stride == 3);
  Q4T_CHECK(w.vocoder[3].transpose.output == 96);
  Q4T_CHECK(w.vocoder[3].residuals[2].conv1.dilation == 9);
  Q4T_CHECK(w.vocoder_output.input == 96 && w.vocoder_output.output == 1);
  const auto* old = w.final_norm;
  Q4T_CHECK(!BindTtsCodecDeviceWeights(base, layout.bytes - 1, layout, &w));
  Q4T_CHECK(!BindTtsCodecDeviceWeights(reinterpret_cast<const void*>(
      std::numeric_limits<uintptr_t>::max() - 3), layout.bytes, layout, &w));
  layout.tensors.back().offset -= 4;
  Q4T_CHECK(!BindTtsCodecDeviceWeights(base, layout.bytes, layout, &w));
  Q4T_CHECK(w.final_norm == old);
  return true;
}

Q4T_TEST(tts_codec_workspace_covers_wave_residuals_and_bounded_tiles) {
  TtsCodecLayout w;
  Q4T_CHECK(PlanTtsCodecWorkspace(325, kUnlimited, &w));
  Q4T_CHECK(w.samples == 624000 && w.padded_frames == 336);
  Q4T_CHECK(w.buffer_elements == 624000 * 96);
  const size_t wave_bytes = w.buffer_elements * sizeof(float);
  Q4T_CHECK(w.wave_buffers[1] >= w.wave_buffers[0] + wave_bytes);
  Q4T_CHECK(w.wave_buffers[2] >= w.wave_buffers[1] + wave_bytes);
  Q4T_CHECK(w.codes >= w.wave_buffers[2] + wave_bytes);
  Q4T_CHECK(w.transpose_weights >= w.columns + (32ULL << 20));
  Q4T_CHECK(w.gemm >= w.transpose_weights + (8ULL << 20));
  Q4T_CHECK(w.bytes < (1ULL << 30));
  const auto bytes = w.bytes;
  Q4T_CHECK(PlanTtsCodecWorkspace(325, bytes, &w));
  Q4T_CHECK(!PlanTtsCodecWorkspace(325, bytes - 1, &w));
  Q4T_CHECK(w.bytes == bytes);
  Q4T_CHECK(!PlanTtsCodecWorkspace(326, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsCodecWorkspace(kUnlimited, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsCodecWorkspace(0, kUnlimited, &w));
  Q4T_CHECK(PlanTtsCodecWorkspace(57, 256ULL << 20, &w));
  return true;
}

Q4T_TEST(tts_codec_chunks_preserve_context_trim_and_last_samples) {
  std::vector<TtsCodecChunk> chunks;
  Q4T_CHECK(PlanTtsCodecChunks(601, 300, 601 * 1920, &chunks));
  Q4T_CHECK(chunks.size() == 3);
  Q4T_CHECK(chunks[0].input_begin == 0 && chunks[0].input_frames == 300);
  Q4T_CHECK(chunks[0].discard_samples == 0);
  Q4T_CHECK(chunks[1].input_begin == 275 && chunks[1].input_frames == 325);
  Q4T_CHECK(chunks[1].discard_samples == 48000);
  Q4T_CHECK(chunks[2].input_begin == 575 && chunks[2].input_frames == 26);
  Q4T_CHECK(chunks[2].output_samples == 1920);
  Q4T_CHECK(!PlanTtsCodecChunks(601, 300, 601 * 1920 - 1, &chunks));
  Q4T_CHECK(chunks.size() == 3);
  Q4T_CHECK(PlanTtsCodecChunks(37, 16, 37 * 1920, &chunks));
  Q4T_CHECK(chunks.size() == 3);
  Q4T_CHECK(chunks[1].input_begin == 0 && chunks[1].input_frames == 32);
  Q4T_CHECK(chunks[1].discard_samples == 16 * 1920);
  Q4T_CHECK(chunks[2].input_begin == 7 && chunks[2].input_frames == 30);
  Q4T_CHECK(chunks[2].discard_samples == 25 * 1920);
  Q4T_CHECK(chunks[2].output_samples == 5 * 1920);
  for (const auto frames : {size_t{0}, size_t{4097}, kUnlimited})
    Q4T_CHECK(!PlanTtsCodecChunks(frames, 300, kUnlimited, &chunks));
  Q4T_CHECK(!PlanTtsCodecChunks(1, 0, kUnlimited, &chunks));
  Q4T_CHECK(!PlanTtsCodecChunks(1, 301, kUnlimited, &chunks));
  return true;
}

Q4T_TEST(tts_codec_codes_reject_control_tokens_before_gpu_work) {
  std::vector<TtsCodes> codes(325);
  for (auto& frame : codes)
    for (size_t i = 0; i < frame.size(); ++i) frame[i] = i * 127;
  Q4T_CHECK(ValidateTtsCodecCodes(codes));
  for (const int id : {-1, 2048, 2150, 3061}) {
    codes.back().back() = id;
    Q4T_CHECK(!ValidateTtsCodecCodes(codes));
  }
  codes.back().back() = 2047;
  Q4T_CHECK(ValidateTtsCodecCodes(codes));
  codes.emplace_back();
  Q4T_CHECK(!ValidateTtsCodecCodes(codes));
  Q4T_CHECK(!ValidateTtsCodecCodes({}));
  return true;
}
