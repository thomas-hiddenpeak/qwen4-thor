// Bounded WebP conversion. Runtime codecs are loaded only by an owned child.
#pragma once

#include "q4t/server/file_input.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
struct WebpHeader {
  uint32_t width = 0, height = 0;
};
struct FileWebpLimits {
  uint64_t max_pixels = 16ULL << 20;
  uint32_t max_edge = 16384;
  uint64_t max_output_bytes = 64ULL << 20;
  int timeout_ms = 10000;
};
struct FileWebpPixels {
  std::string png;
  io::Json conversion;
};
bool IsWebpImage(std::string_view bytes);
ObjectReply ReadWebpHeader(std::string_view bytes, WebpHeader* header);
// Revalidates original container, actual PNG dimensions, identities and exact
// conversion metadata. No runtime codec or external state is read on replay.
ObjectReply ValidateWebpPixels(std::string_view source,
                               const FileWebpLimits& limits,
                               const FileWebpPixels& pixels,
                               WebpHeader* header);
ObjectReply DecodeWebpImage(std::string_view source,
                            const FileWebpLimits& limits,
                            const BinaryAccess& access, FileWebpPixels* pixels,
                            GrammarControl control = {});
int MaybeRunFileWebpWorker(int argc, char** argv);
namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetWebpWorkerEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
