// Native audio preparation in an owned, bounded CPU codec worker.
#pragma once

#include "q4t/server/file_input.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
struct FileAudioLimits {
  uint64_t max_source_bytes = 25ULL << 20;
  uint64_t max_decoded_values = 64ULL << 20;
  uint64_t max_output_samples = 480000;
  uint64_t max_duration_ns = 30000000000ULL;
  uint32_t max_sample_rate = 192000;
  uint32_t max_channels = 8;
  uint32_t max_thread_creations = 16;
  int timeout_ms = 15000;
};
struct FileAudioPcm {
  // Always finite F32, 16 kHz mono. Duration derives from these samples;
  // it is not a word/segment alignment or a fabricated source timestamp.
  std::vector<float> samples;
  uint64_t duration_ns = 0;
  io::Json conversion;
};
bool IsAudioContainer(std::string_view bytes);
bool ValidFileAudioLimits(const FileAudioLimits& limits);
ObjectReply ValidateAudioPcm(std::string_view source,
                             const FileAudioLimits& limits,
                             const FileAudioPcm& pcm);
ObjectReply DecodeAudioContainer(std::string_view source,
                                 const FileAudioLimits& limits,
                                 const BinaryAccess& access, FileAudioPcm* pcm,
                                 GrammarControl control = {});
int MaybeRunFileAudioWorker(int argc, char** argv);
namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetAudioWorkerEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
