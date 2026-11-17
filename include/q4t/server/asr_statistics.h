// Actual compression diagnostics; dynamically loaded zlib stays in a child.
#pragma once

#include <string_view>

#include "q4t/server/grammar_process.h"

namespace q4t::server {
struct AsrCompression {
  uint64_t text_bytes = 0, compressed_bytes = 0;
  std::string library_version, library_sha256;
  double Ratio() const {
    return compressed_bytes ? double(text_bytes) / compressed_bytes : 0;
  }
};
Status MeasureAsrCompression(std::string_view text, AsrCompression* out,
                             GrammarControl control = {},
                             GrammarFailure* failure = nullptr);
int MaybeRunAsrStatisticsWorker(int argc, char** argv);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetAsrStatisticsWorkerForTesting(std::string entry);
#endif
}  // namespace q4t::server
