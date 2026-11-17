#include "q4t/audio/vad_chunks.h"

#include <limits>

#include "q4t/test.h"

namespace {
using namespace q4t::audio;
std::vector<VadFrame> Frames(std::initializer_list<float> probabilities) {
  std::vector<VadFrame> frames;
  for (float probability : probabilities) {
    const auto begin = frames.size() * 512;
    frames.push_back({begin, begin + 512, probability});
  }
  return frames;
}
VadChunkOptions Options() {
  VadChunkOptions options;
  options.prefix_padding_ms = 0;
  options.silence_duration_ms = 64;
  return options;
}
}  // namespace

Q4T_TEST(vad_chunks_hysteresis_keeps_short_pauses_and_real_sample_edges) {
  auto frames = Frames({.1f, .8f, .2f, .4f, .8f, .1f, .1f, .9f});
  std::vector<VadChunk> chunks;
  Q4T_CHECK(SplitVadChunks(frames, 4096, Options(), &chunks));
  Q4T_CHECK(chunks ==
            std::vector<VadChunk>({{512, 2560, false}, {3584, 4096, false}}));
  return true;
}

Q4T_TEST(vad_chunks_prefix_padding_never_duplicates_audio_or_goes_negative) {
  auto frames = Frames({.1f, .8f, .1f, .1f, .9f, .1f, .1f});
  auto options = Options();
  options.prefix_padding_ms = 300;
  std::vector<VadChunk> chunks;
  Q4T_CHECK(SplitVadChunks(frames, 3584, options, &chunks));
  Q4T_CHECK(chunks ==
            std::vector<VadChunk>({{0, 1024, false}, {1024, 2560, false}}));
  return true;
}

Q4T_TEST(vad_chunks_hard_limits_preserve_tail_and_never_silently_drop_chunks) {
  auto frames = Frames({.8f, .8f, .8f, .8f, .8f});
  frames.back().end_sample = 2200;
  auto options = Options();
  options.max_chunk_samples = 1024;
  std::vector<VadChunk> chunks;
  Q4T_CHECK(SplitVadChunks(frames, 2200, options, &chunks));
  const std::vector<VadChunk> expected{
      {0, 1024, true}, {1024, 2048, true}, {2048, 2200, false}};
  Q4T_CHECK(chunks == expected);
  options.max_chunks = 2;
  Q4T_CHECK(!SplitVadChunks(frames, 2200, options, &chunks));
  Q4T_CHECK(chunks == expected);
  return true;
}

Q4T_TEST(vad_chunks_threshold_and_zero_silence_have_actual_effect) {
  auto frames = Frames({0, .5f, 0, 0});
  auto options = Options();
  options.silence_duration_ms = 0;
  std::vector<VadChunk> chunks;
  Q4T_CHECK(SplitVadChunks(frames, 2048, options, &chunks));
  Q4T_CHECK(chunks == std::vector<VadChunk>({{512, 1024, false}}));
  options.threshold = .6;
  Q4T_CHECK(SplitVadChunks(frames, 2048, options, &chunks) && chunks.empty());
  options.threshold = 0;
  Q4T_CHECK(SplitVadChunks(frames, 2048, options, &chunks));
  Q4T_CHECK(chunks == std::vector<VadChunk>({{0, 2048, false}}));
  return true;
}

Q4T_TEST(vad_chunks_invalid_observations_and_options_preserve_owner) {
  const auto original = Frames({.8f, .8f});
  const std::vector<VadChunk> kept{{17, 24, false}};
  for (int fault = 0; fault < 7; ++fault) {
    auto frames = original;
    auto options = Options();
    if (fault == 0) ++frames[1].begin_sample;
    if (fault == 1) --frames[0].end_sample;
    if (fault == 2) frames[0].speech_probability = -1;
    if (fault == 3)
      frames[0].speech_probability = std::numeric_limits<float>::quiet_NaN();
    if (fault == 4) options.threshold = 1.1;
    if (fault == 5) options.prefix_padding_ms = -1;
    if (fault == 6) options.max_samples = kVadMaxSamples + 1;
    auto chunks = kept;
    Q4T_CHECK(!SplitVadChunks(frames, 1024, options, &chunks));
    Q4T_CHECK(chunks == kept);
  }
  return true;
}

Q4T_TEST(vad_chunks_cancellation_and_silence_publish_atomically) {
  auto frames = Frames({.8f, 0, 0, .9f, 0, 0});
  const std::vector<VadChunk> kept{{17, 24, false}};
  auto chunks = kept;
  int checkpoints = 0;
  Q4T_CHECK(!SplitVadChunks(frames, 3072, Options(), &chunks,
                            [&] { return ++checkpoints >= 6; }));
  Q4T_CHECK(chunks == kept);
  frames = Frames({0, .1f, .2f});
  Q4T_CHECK(SplitVadChunks(frames, 1536, Options(), &chunks) && chunks.empty());
  return true;
}
