#pragma once

#include "q4t/server/file_document.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
// Native text/delimited extraction in an exec-only, syscall-restricted child.
// Complex document codecs use the same ownership/budget contract when added;
// callers must dispatch those formats to their actual decoder.
ObjectReply ParseNativeFileDocument(const FileInputContent& input,
                                    const FileDocumentLimits& limits,
                                    const BinaryAccess& access,
                                    FileDocument* output,
                                    GrammarControl control = {});
int MaybeRunFileDocumentWorker(int argc, char** argv);
namespace detail {
// Invoke only after exec/entry validation. No filesystem/network/process
// creation capabilities remain; inherited stdio and fd 3 are the only IO.
bool EnterNativeDocumentSandbox();
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
// Host-only fault injection. Production always uses its exact built-in entry.
void SetDocumentWorkerEntryForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
