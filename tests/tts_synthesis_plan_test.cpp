#include "q4t/audio/tts_synthesis_plan.h"
#include "q4t/server/tts_service.h"
#include "q4t/test.h"

#include <algorithm>
#include <limits>

namespace {
using namespace q4t::audio;
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();
std::vector<TtsCodes> Codes(size_t count, int offset) {
  std::vector<TtsCodes> result(count);
  for (size_t row = 0; row < count; ++row)
    for (size_t group = 0; group < 16; ++group)
      result[row][group] = (offset + row * 17 + group) % 2048;
  return result;
}
}  // namespace

Q4T_TEST(tts_synthesis_reference_schedule_matches_full_decode_then_crop) {
  for (const size_t reference : {0, 1, 25, 30, 299, 300, 301, 750}) {
    for (const size_t generated : {2, 7, 37, 301}) {
      for (const size_t width : {16, 300}) {
        std::vector<TtsCodecChunk> official;
        Q4T_CHECK(PlanTtsCodecChunks(reference + generated, width, kUnlimited,
                                     &official));
        size_t emitted = 0;
        for (const auto& full : official) {
          const size_t output_begin =
              full.input_begin + full.discard_samples / 1920;
          const size_t end = output_begin + full.output_samples / 1920;
          if (end <= reference) continue;
          TtsSynthesisChunk chunk;
          Q4T_CHECK(PlanTtsSynthesisChunk(reference, generated, emitted, width,
                                          true, true, &chunk));
          Q4T_CHECK(chunk.ready && chunk.input_begin == full.input_begin &&
                    chunk.input_frames == full.input_frames);
          const size_t crop = std::max(reference, output_begin);
          Q4T_CHECK(chunk.discard_frames == crop - full.input_begin &&
                    chunk.output_frames == end - crop);
          emitted += chunk.output_frames;
        }
        Q4T_CHECK(emitted == generated);
        TtsSynthesisChunk complete;
        Q4T_CHECK(PlanTtsSynthesisChunk(reference, generated, emitted, width,
                                        true, true, &complete));
        Q4T_CHECK(!complete.ready);
      }
    }
  }
  return true;
}

Q4T_TEST(tts_synthesis_reference_deferred_output_waits_for_natural_eos) {
  TtsSynthesisChunk chunk;
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 40, 0, 16, false, true, &chunk));
  Q4T_CHECK(!chunk.ready);
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 40, 0, 16, true, true, &chunk));
  Q4T_CHECK(chunk.ready && chunk.input_begin == 0 && chunk.input_frames == 32 &&
            chunk.discard_frames == 30 && chunk.output_frames == 2);
  return true;
}

Q4T_TEST(tts_synthesis_reference_streaming_respects_global_chunk_boundary) {
  TtsSynthesisChunk chunk;
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 1, 0, 16, false, false, &chunk));
  Q4T_CHECK(!chunk.ready);
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 2, 0, 16, false, false, &chunk));
  Q4T_CHECK(chunk.ready && chunk.output_frames == 2);
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 17, 2, 16, false, false, &chunk));
  Q4T_CHECK(!chunk.ready);
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 18, 2, 16, false, false, &chunk));
  Q4T_CHECK(chunk.ready && chunk.input_begin == 7 && chunk.input_frames == 41 &&
            chunk.discard_frames == 25 && chunk.output_frames == 16);
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 19, 18, 16, true, false, &chunk));
  Q4T_CHECK(chunk.ready && chunk.discard_frames == 25 &&
            chunk.output_frames == 1);
  return true;
}

Q4T_TEST(tts_synthesis_reference_slice_copies_every_group_and_hides_reference) {
  const auto reference = Codes(750, 19), generated = Codes(2, 991);
  TtsSynthesisChunk chunk;
  Q4T_CHECK(PlanTtsSynthesisChunk(750, 2, 0, 300, true, true, &chunk));
  Q4T_CHECK(chunk.input_begin == 575 && chunk.input_frames == 177 &&
            chunk.discard_frames == 175 && chunk.output_frames == 2);
  std::vector<TtsCodes> output;
  Q4T_CHECK(BuildTtsSynthesisChunk(reference, generated, chunk, &output));
  Q4T_CHECK(output.size() == 177);
  Q4T_CHECK(
      std::equal(reference.begin() + 575, reference.end(), output.begin()));
  Q4T_CHECK(std::equal(generated.begin(), generated.end(),
                       output.begin() + chunk.discard_frames));
  return true;
}

Q4T_TEST(tts_synthesis_reference_invalid_bounds_never_overwrite_owner) {
  TtsSynthesisChunk prior{true, 575, 177, 175, 2};
  const std::array<std::array<size_t, 4>, 8> bad{{{751, 2, 0, 300},
                                                  {0, 4097, 0, 300},
                                                  {0, 2, 3, 300},
                                                  {1, 2, 0, 0},
                                                  {1, 2, 0, 301},
                                                  {30, 20, 3, 16},
                                                  {kUnlimited, 2, 0, 16},
                                                  {0, kUnlimited, 0, 16}}};
  for (const auto& shape : bad) {
    auto out = prior;
    Q4T_CHECK(!PlanTtsSynthesisChunk(shape[0], shape[1], shape[2], shape[3],
                                     true, true, &out));
    Q4T_CHECK(out.input_begin == prior.input_begin && out.ready);
  }
  Q4T_CHECK(!PlanTtsSynthesisChunk(0, 2, 0, 16, true, true, nullptr));
  TtsSynthesisChunk longest;
  Q4T_CHECK(PlanTtsSynthesisChunk(750, 4096, 0, 300, true, true, &longest));
  Q4T_CHECK(longest.input_frames == 325 && longest.output_frames == 150);
  return true;
}

Q4T_TEST(tts_synthesis_reference_slice_rejects_controls_and_preserves_aliases) {
  auto reference = Codes(30, 1), generated = Codes(2, 991);
  TtsSynthesisChunk chunk;
  Q4T_CHECK(PlanTtsSynthesisChunk(30, 2, 0, 16, true, true, &chunk));
  auto out = Codes(1, 77);
  reference[0][15] = 2048;
  Q4T_CHECK(!BuildTtsSynthesisChunk(reference, generated, chunk, &out));
  Q4T_CHECK(out == Codes(1, 77));
  reference = Codes(30, 1);
  generated[1][15] = -1;
  Q4T_CHECK(!BuildTtsSynthesisChunk(reference, generated, chunk, &out));
  generated = Codes(2, 991);
  for (int mutation = 0; mutation < 5; ++mutation) {
    auto bad = chunk;
    if (mutation == 0) bad.discard_frames = 29;
    if (mutation == 1) bad.input_frames = 326;
    if (mutation == 2) bad.output_frames = 3;
    if (mutation == 3) bad.input_begin = kUnlimited;
    if (mutation == 4) bad.ready = false;
    Q4T_CHECK(!BuildTtsSynthesisChunk(reference, generated, bad, &out));
    Q4T_CHECK(out == Codes(1, 77));
  }
  Q4T_CHECK(BuildTtsSynthesisChunk(reference, generated, chunk, &reference));
  Q4T_CHECK(reference.size() == 32 && reference.back() == generated.back());
  return true;
}

Q4T_TEST(tts_synthesis_reference_resources_charge_context_and_owned_copies) {
  q4t::server::TtsResourcePlan base, icl;
  constexpr auto variant = TtsVariant::kBase17B;
  Q4T_CHECK(
      q4t::server::PlanTtsServiceResources({}, 32, 800, 2, &base, variant));
  Q4T_CHECK(
      q4t::server::PlanTtsServiceResources({}, 32, 800, 2, &icl, variant, 750));
  Q4T_CHECK(icl.codec_bytes > base.codec_bytes &&
            icl.generation_bytes > base.generation_bytes);
  Q4T_CHECK(icl.reference_host_bytes == (3 * 750 + 325) * sizeof(TtsCodes));
  Q4T_CHECK(icl.request_host_bytes ==
            base.request_host_bytes + icl.reference_host_bytes);
  Q4T_CHECK(icl.load_peak_bytes == base.load_peak_bytes &&
            icl.resident_weight_bytes == base.resident_weight_bytes);
  Q4T_CHECK(!q4t::server::PlanTtsServiceResources({}, 32, 800, 2, &icl, variant,
                                                  751));
  return true;
}

Q4T_TEST(tts_synthesis_without_reference_keeps_legacy_resource_contract) {
  q4t::server::TtsResourcePlan base, builtin, explicit_zero;
  Q4T_CHECK(q4t::server::PlanTtsServiceResources({}, 32, 48, 160, &base,
                                                 TtsVariant::kBase17B));
  Q4T_CHECK(q4t::server::PlanTtsServiceResources({}, 32, 48, 160, &builtin));
  Q4T_CHECK(q4t::server::PlanTtsServiceResources(
      {}, 32, 48, 160, &explicit_zero, TtsVariant::kBase17B, 0));
  Q4T_CHECK(base.request_device_bytes == explicit_zero.request_device_bytes &&
            base.request_host_bytes == explicit_zero.request_host_bytes &&
            base.reference_host_bytes == 0);
  Q4T_CHECK(base.request_device_bytes == builtin.request_device_bytes + 8192 &&
            base.request_host_bytes == builtin.request_host_bytes + 16384);
  Q4T_CHECK(!q4t::server::PlanTtsServiceResources(
      {}, 32, 48, 160, &builtin, TtsVariant::kCustomVoice17B, 2));
  return true;
}
