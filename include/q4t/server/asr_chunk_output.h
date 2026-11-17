#pragma once

#include "q4t/audio/vad_chunks.h"
#include "q4t/server/asr_request.h"
#include "q4t/server/asr_response.h"

namespace q4t::server {
// Request-local collection of independently decoded clips. Text joins use one
// display space; each segment's original text/IDs/probabilities stay intact.
// A failed append poisons final publication, including after streamed deltas.
class AsrChunkOutput {
 public:
  AsrChunkOutput(const AsrRequest& request, size_t total_samples);
  Status Append(const AsrTranscript& transcript, std::string formatted,
                const audio::VadChunk& chunk, uint64_t audio_tokens,
                size_t subtitle_cues = 0);
  Status Finish(const ActualUsage& usage, std::string* out) const;
  const std::string& Text() const { return text_; }
  size_t CueCount() const { return cues_; }

 private:
  bool valid_ = true;
  AsrFormat format_;
  bool stream_, logprobs_, word_timestamps_, segment_timestamps_;
  size_t total_samples_, previous_end_ = 0, clips_ = 0, cues_ = 0, bytes_ = 0;
  uint64_t audio_tokens_ = 0;
  std::string text_, subtitles_, forced_language_;
  io::Json words_, segments_, probabilities_;
  std::vector<std::pair<std::string, size_t>> languages_;
};
}  // namespace q4t::server
