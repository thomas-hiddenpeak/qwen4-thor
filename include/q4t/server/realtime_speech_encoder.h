// Continuous native TTS PCM -> the negotiated Realtime wire audio format.
#pragma once

#include <functional>
#include <string_view>

#include "q4t/audio/stream_resample.h"
#include "q4t/server/object_store.h"
#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
struct RealtimeSpeechReceipt {
  uint64_t input_samples = 0, output_samples = 0, bytes = 0;
};

// One worker owns this converter. Input is actual 24kHz mono S16LE output
// from native speech synthesis. Odd chunks retain one byte, never pad a sample.
// The sink synchronously accepts each bounded binary chunk. Any failure ends
// this stream; partially accepted output must not be replayed or marked done.
class RealtimeSpeechEncoder {
 public:
  using Sink = std::function<ObjectReply(std::string_view)>;
  static constexpr size_t kMaximumChunkBytes = 64 * 1024;
  static constexpr size_t kOutputChunkBytes = 2048;
  RealtimeSpeechEncoder(RealtimeAudioFormat format, size_t maximum_samples,
                        size_t maximum_bytes,
                        std::function<bool()> cancelled = {});
  RealtimeSpeechEncoder(const RealtimeSpeechEncoder&) = delete;
  RealtimeSpeechEncoder& operator=(const RealtimeSpeechEncoder&) = delete;
  bool Valid() const { return valid_; }
  bool Finished() const { return finished_; }
  ObjectReply Append(std::string_view pcm, const Sink& sink);
  // Only full natural EOF publishes a receipt. It remains unchanged on error.
  ObjectReply Finish(const Sink& sink, RealtimeSpeechReceipt* receipt);

 private:
  ObjectReply Fail(std::string_view code, std::string_view message);
  ObjectReply Check();
  ObjectReply Write(std::span<const float> samples, const Sink& sink);
  const RealtimeAudioFormat format_;
  const size_t maximum_samples_, maximum_bytes_;
  const std::function<bool()> cancelled_;
  audio::AsrStreamResampler resampler_;
  uint64_t input_bytes_ = 0, output_bytes_ = 0;
  uint8_t pending_byte_ = 0;
  bool valid_ = false, pending_ = false, finished_ = false;
  ObjectReply error_;
};
}  // namespace q4t::server
