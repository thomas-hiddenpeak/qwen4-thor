#include "q4t/server/live_recording.h"

#include <algorithm>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
constexpr auto left = LiveAudioChannel::kInput,
               right = LiveAudioChannel::kOutput;
std::string Samples(std::initializer_list<int> values) {
  std::string out;
  for (auto value : values) {
    const auto word = uint16_t(value);
    out.push_back(char(word));
    out.push_back(char(word >> 8));
  }
  return out;
}
struct Capture {
  std::vector<std::string> stored;
  LiveRecordingLayout layout;
  BinaryManifest blob;
  bool fail = false;
  LiveRecordingWriter writer;
  explicit Capture(RealtimeAudioFormat format = {})
      : writer(format, [this](std::string_view bytes) {
          if (fail) return Error(503, "storage_failure", "lost write", true);
          stored.emplace_back(bytes);
          blob.bytes += bytes.size();
          blob.chunks.push_back({"chunk_" + std::to_string(stored.size()),
                                 Digest(bytes), bytes.size()});
          return ObjectReply{};
        }) {}
  ObjectReply Read(size_t index, std::string* out) {
    if (index >= stored.size()) return Error(503, "missing_chunk", "missing");
    *out = stored[index];
    return {};
  }
  std::string Render() {
    LiveRecordingLayout verified;
    if (!ReadLiveRecordingLayout(LiveRecordingMetadata(layout), blob,
                                 &verified))
      throw std::runtime_error("invalid persisted layout");
    LiveRecordingWaveReader reader(
        std::move(verified),
        [&](auto index, auto* out) { return Read(index, out); });
    std::string wave, part;
    do {
      if (!reader.Next(&part).ok())
        throw std::runtime_error("incomplete recording");
      wave += part;
    } while (!part.empty());
    if (wave.size() != reader.Bytes())
      throw std::runtime_error("incorrect WAVE length");
    return wave;
  }
};
}  // namespace
Q4T_TEST(live_recording_interleaves_overlaps_and_silence_on_the_sample_clock) {
  Capture c;
  Q4T_CHECK(c.writer.Audio(right, 3, Samples({-32768, 32767})).ok());
  Q4T_CHECK(c.writer.Audio(left, 0, Samples({10, 20})).ok());
  Q4T_CHECK(c.writer.Audio(left, 8, Samples({30})).ok());
  Q4T_CHECK(c.writer.Finish(12, &c.layout).ok() && c.layout.frames == 6);
  const auto wave = c.Render();
  Q4T_CHECK(wave.substr(44) ==
            Samples({10, 0, 20, -32768, 0, 32767, 0, 0, 30, 0, 0, 0}));
  Q4T_CHECK(wave.substr(0, 4) == "RIFF" && wave.substr(8, 8) == "WAVEfmt " &&
            wave[22] == 2 && uint8_t(wave[24]) == 0xc0 &&
            uint8_t(wave[25]) == 0x5d && wave[32] == 4 &&
            uint8_t(wave[40]) == 24 && wave[34] == 16);
  return true;
}
Q4T_TEST(
    live_recording_decodes_g711_exactly_and_allows_empty_or_silent_sessions) {
  for (auto encoding :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    Capture c({encoding, 8000});
    std::string raw;
    raw.push_back(
        char(encoding == RealtimeAudioEncoding::kMuLaw ? 0xff : 0xd5));
    raw.push_back(char(0));
    Q4T_CHECK(c.writer.Audio(right, 0, raw).ok() &&
              c.writer.Finish(12, &c.layout).ok());
    Q4T_CHECK(c.Render().substr(44) ==
              (encoding == RealtimeAudioEncoding::kMuLaw
                   ? Samples({0, 0, 0, -32124})
                   : Samples({0, 8, 0, -5504})));
  }
  for (auto ticks : {0u, 7u}) {
    Capture c({RealtimeAudioEncoding::kPcm16, 16000});
    Q4T_CHECK(c.writer.Finish(ticks, &c.layout).ok() && c.stored.empty() &&
              c.Render().substr(44) == std::string((ticks + 2) / 3 * 4, '\0'));
  }
  std::string header = "kept";
  Q4T_CHECK(!q4t::audio::SpeechWaveHeader(0, 44, &header) && header == "kept");
  return true;
}
Q4T_TEST(live_recording_streams_large_spans_and_reads_each_chunk_once) {
  Capture c;
  std::string raw(kBinaryChunkBytes + 4, '\x25');
  Q4T_CHECK(c.writer.Audio(left, 0, raw).ok() && c.stored.size() == 1);
  Q4T_CHECK(c.writer.Finish(raw.size(), &c.layout).ok() &&
            c.stored.size() == 2);
  std::vector<int> reads(2);
  LiveRecordingWaveReader reader(c.layout, [&](size_t index, std::string* out) {
    ++reads.at(index);
    return c.Read(index, out);
  });
  uint64_t bytes = 0;
  std::string part;
  do {
    Q4T_CHECK(reader.Next(&part).ok());
    bytes += part.size();
    Q4T_CHECK(part.size() <= 65536);
  } while (!part.empty());
  Q4T_CHECK(bytes == 44 + raw.size() * 2 && reads[0] == 1 && reads[1] == 1);
  return true;
}
Q4T_TEST(live_recording_rejects_partial_overlapping_or_unconfirmed_writes) {
  for (int i = 0; i < 5; ++i) {
    Capture c;
    Q4T_CHECK(c.writer.Audio(left, 0, Samples({1, 2})).ok());
    ObjectReply failed;
    if (i == 0) failed = c.writer.Audio(left, 0, Samples({3}));
    if (i == 1) failed = c.writer.Audio(right, 0, "x");
    if (i == 2)
      failed = c.writer.Audio(right, kRealtimeSessionTicks, Samples({2}));
    if (i == 3) failed = c.writer.Finish(1, &c.layout);
    if (i == 4) {
      c.fail = true;
      failed = c.writer.Finish(4, &c.layout);
      Q4T_CHECK(failed.commit_unknown);
    }
    Q4T_CHECK(!failed.ok() && !c.writer.Finish(4, &c.layout).ok() &&
              c.layout.rate == 0);
  }
  return true;
}
Q4T_TEST(
    live_recording_corrupt_chunk_map_or_stream_fails_before_false_completion) {
  Capture c;
  Q4T_CHECK(c.writer.Audio(left, 0, Samples({1})).ok() &&
            c.writer.Audio(right, 0, Samples({2})).ok() &&
            c.writer.Finish(2, &c.layout).ok());
  for (int i = 0; i < 5; ++i) {
    auto metadata = LiveRecordingMetadata(c.layout);
    auto& entry = Field(metadata, "tracks").array[1].array[0];
    if (i == 0) Set(&entry, "chunk", Number(0));
    if (i == 1) Set(&entry, "frames", Number(2));
    if (i == 2) Set(&entry, "frame", Number(1));
    if (i == 3) Field(metadata, "tracks").array[1].array.clear();
    if (i == 4) Set(&metadata, "sample_rate", Number(44100));
    LiveRecordingLayout sentinel;
    sentinel.frames = 45;
    Q4T_CHECK(!ReadLiveRecordingLayout(metadata, c.blob, &sentinel) &&
              sentinel.frames == 45);
  }
  LiveRecordingWaveReader reader(c.layout, [](auto, std::string* bytes) {
    *bytes = "x";
    return ObjectReply{};
  });
  std::string block;
  Q4T_CHECK(reader.Next(&block).ok() && block.size() == 44);
  Q4T_CHECK(!reader.Next(&block).ok() && block.size() == 44 &&
            !reader.Next(&block).ok());
  return true;
}
