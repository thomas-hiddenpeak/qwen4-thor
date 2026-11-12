// Isolated system-codec adaptation for legacy and application document formats.
#pragma once

#include "q4t/server/file_document_worker.h"
#include "q4t/server/worker_io_budget.h"

namespace q4t::server {
struct FileLegacyOptions {
  // Deployment-owned private directory; never read from the client request.
  // All filesystem setup/cleanup runs in owned workers, not the model thread.
  std::string work_directory;
  uint64_t max_converted_bytes = kFileInputBytes;
  uint64_t timeout_ms = 30000;
  WorkerIoBudget io;
};
ObjectReply ParseLegacyFileDocument(const FileInputContent& input,
                                    const FileDocumentLimits& limits,
                                    const FileLegacyOptions& options,
                                    const BinaryAccess& access,
                                    FileDocument* output,
                                    GrammarControl control = {});
int MaybeRunFileLegacyWorker(int argc, char** argv);
namespace detail {
// Private scratch inode is the only writable tree. Install before loading
// codecs or creating threads. Returns the owned seccomp notification listener.
int EnterLegacyDocumentSandbox(int scratch_fd);
}  // namespace detail
}  // namespace q4t::server
