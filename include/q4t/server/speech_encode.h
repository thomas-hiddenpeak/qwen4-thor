// Bounded speech output encoding; compressed codecs live only in a child.
#pragma once

#include <span>

#include "q4t/server/file_input.h"
#include "q4t/server/grammar_process.h"
#include "q4t/server/speech_request.h"

namespace q4t::server {
struct SpeechEncodeLimits {
  uint64_t max_samples = 4096ULL * 1920 * 4;
  uint64_t max_output_bytes = 64ULL << 20;
  uint32_t max_thread_creations = 16;
  int timeout_ms = 15000;
};
struct EncodedSpeech {
  std::string bytes;
  io::Json encoding;
};
bool ValidSpeechEncodeLimits(const SpeechEncodeLimits& limits);
// One complete bounded waveform. Streaming synthesis/encoding is separate.
// On failure, output is unchanged. No filesystem writes or codec subprocess
// executables; the owned child loads only registered system codec libraries.
ObjectReply EncodeSpeech(std::span<const float> pcm, SpeechFormat format,
                         const SpeechEncodeLimits& limits,
                         const BinaryAccess& access, EncodedSpeech* out,
                         GrammarControl control = {});
int MaybeRunSpeechEncodeWorker(int argc, char** argv);
namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetSpeechEncodeEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
