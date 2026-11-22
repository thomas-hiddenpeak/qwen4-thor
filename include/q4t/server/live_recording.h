// Store only observed audio spans. WAV downloads interleave them on the session
// clock.
#pragma once

#include <array>

#include "q4t/server/binary_store.h"
#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
struct LiveRecordingSpan {
  uint64_t frame;
  size_t chunk;
  uint32_t frames;
};
struct LiveRecordingLayout {
  uint32_t rate = 0;
  uint64_t frames = 0;
  std::array<std::vector<LiveRecordingSpan>, 2> tracks;
};
enum class LiveAudioChannel { kInput, kOutput };
using LiveRecordSink =
    std::function<void(LiveAudioChannel, uint64_t, std::string_view)>;
io::Json LiveRecordingMetadata(const LiveRecordingLayout& layout);
Status ReadLiveRecordingLayout(const io::Json& data, const BinaryManifest& blob,
                               LiveRecordingLayout* out);

// Worker-owned, two bounded PCM buffers. 'append' durably appends exactly one
// chunk in call order. On any failed or unknown append the stage is discarded.
class LiveRecordingWriter {
 public:
  using Append = std::function<ObjectReply(std::string_view)>;
  LiveRecordingWriter(RealtimeAudioFormat format, Append append);
  ObjectReply Audio(LiveAudioChannel channel, uint64_t begin_ticks,
                    std::string_view encoded);
  ObjectReply Finish(uint64_t end_ticks, LiveRecordingLayout* layout);

 private:
  ObjectReply Flush(size_t channel);
  const RealtimeAudioFormat format_;
  const Append append_;
  LiveRecordingLayout layout_;
  std::array<std::string, 2> pending_;
  std::array<uint64_t, 2> end_{};
  size_t chunks_ = 0;
  bool done_ = false;
  ObjectReply error_;
};

// A validated durable manifest and a scope-pinned BinaryRead provide each
// chunk. At most one chunk per direction and one output block are resident.
class LiveRecordingWaveReader {
 public:
  using Read = std::function<ObjectReply(size_t, std::string*)>;
  LiveRecordingWaveReader(LiveRecordingLayout layout, Read read);
  uint64_t Bytes() const { return 44 + layout_.frames * 4; }
  ObjectReply Next(std::string* bytes);

 private:
  const LiveRecordingLayout layout_;
  const Read read_;
  std::array<size_t, 2> span_{};
  std::array<std::string, 2> pcm_;
  uint64_t frame_ = 0;
  bool header_ = false, failed_ = false;
};
}  // namespace q4t::server
