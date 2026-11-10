// Owned, bounded PDF extraction. The decoder is a system executable, never
// an in-process GPL library or a separate document service.
#pragma once

#include "q4t/server/file_document.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
struct FilePdfOptions {
  size_t max_pages = 32;
  uint64_t max_pixels = 64ULL << 20;
  uint64_t max_image_bytes = 64ULL << 20;
  int timeout_ms = 30000;
};
// Extract both UTF-8 text and every rendered page; no silent page truncation.
// detail selects a 512 (low), 1536 (auto), or 2048 (high) pixel bounding box.
// The media admission planner must still validate actual model/context costs.
ObjectReply ParsePdfFileDocument(const FileInputContent& input,
                                 const FileDocumentLimits& limits,
                                 const FilePdfOptions& options,
                                 std::string_view detail,
                                 const BinaryAccess& access,
                                 FileDocument* output,
                                 GrammarControl control = {});
int MaybeRunFilePdfWorker(int argc, char** argv);
namespace detail {
// Only in the already-exec'd owned worker, with sealed stdin and fixed fd 4
// for the trusted system binary. File contents and network are denied except
// system libraries/fonts and inherited IO. No process creation is allowed.
bool EnterPdfSandbox(int executable_fd);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetPdfWorkerEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
