// Native container decoding is confined to an owned, bounded CPU worker.
#pragma once

#include "q4t/server/file_input.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
struct FileVideoLimits {
  uint64_t max_source_bytes = 32ULL << 20;
  uint64_t max_frame_pixels = 4096ULL * 2160;
  uint64_t max_decoded_pixels = 512ULL << 20;
  uint64_t max_output_bytes = 64ULL << 20;
  uint64_t max_duration_ns = 600000000000ULL;
  uint32_t max_edge = 4096;
  uint32_t max_scanned_frames = 7200;
  uint32_t sample_frames = 8;
  int timeout_ms = 15000;
};
struct FileVideoFrame {
  std::string png;
  uint64_t timestamp_ns = 0, source_index = 0;
};
struct FileVideoPixels {
  uint32_t width = 0, height = 0;
  uint64_t duration_ns = 0, scanned_frames = 0;
  std::vector<FileVideoFrame> frames;
  io::Json conversion;
};
bool IsVideoContainer(std::string_view bytes);
bool ValidFileVideoLimits(const FileVideoLimits& limits);
// Revalidates frozen bytes and timestamps without loading any codec.
ObjectReply ValidateVideoPixels(std::string_view source,
                                const FileVideoLimits& limits,
                                const FileVideoPixels& pixels);
ObjectReply DecodeVideoContainer(std::string_view source,
                                 const FileVideoLimits& limits,
                                 const BinaryAccess& access,
                                 FileVideoPixels* pixels,
                                 GrammarControl control = {});
int MaybeRunFileVideoWorker(int argc, char** argv);
namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetVideoWorkerEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
