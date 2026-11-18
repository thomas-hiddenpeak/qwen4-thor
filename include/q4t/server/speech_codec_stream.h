// Request-owned compressed audio stream using one isolated codec process.
#pragma once

#include <memory>

#include "q4t/server/speech_encode.h"

namespace q4t::server {
enum class SpeechCodecStep { kNeedInput, kOutput, kDone };
class SpeechCodecStream {
 public:
  static constexpr size_t kInputSamples = 2048;
  static constexpr size_t kOutputBytes = 65536;
  SpeechCodecStream();
  ~SpeechCodecStream();
  SpeechCodecStream(const SpeechCodecStream&) = delete;
  SpeechCodecStream& operator=(const SpeechCodecStream&) = delete;
  // MP3/AAC/Opus/FLAC. PCM/WAV have native byte writers, without this worker.
  // timeout_ms bounds each codec operation; access.deadline bounds the entire
  // request, including time spent by the caller generating subsequent PCM.
  ObjectReply Start(SpeechFormat format, const SpeechEncodeLimits& limits,
                    const BinaryAccess& access, GrammarControl control = {});
  // Only after kNeedInput. Invalid PCM/capacity leaves input state unchanged.
  // final=true may carry the last samples or an empty EOF. No inferred EOF.
  ObjectReply Append(std::span<const float> pcm, bool final);
  // Output bytes remain valid until the next Append/Advance. Previously
  // emitted bytes are provisional until kDone confirms metadata and child exit.
  ObjectReply Advance(SpeechCodecStep* step, std::string_view* output);
  const io::Json& Metadata() const;
  void Stop();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
int MaybeRunSpeechCodecStreamWorker(int argc, char** argv);
namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetSpeechCodecStreamEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
