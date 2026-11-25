// Isolated execution component. Its input is constructed by the authorized
// Responses executor, never accepted as a public client replay fingerprint.
#pragma once

#include "q4t/server/binary_store.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {

ObjectReply ExecuteProgramWorker(const io::Json& input,
                                 const BinaryAccess& access,
                                 GrammarControl control = {});
// Progress frames are provisional. Successful process exit authenticates the
// terminal result before the caller can commit any application state.
ObjectReply ExecuteActionWorker(
    const io::Json& input, const BinaryAccess& access,
    const std::function<bool(std::string_view)>& progress);
ObjectReply ExecutePythonWorker(const io::Json& input,
                                const BinaryAccess& access,
                                GrammarControl control = {});
ObjectReply ExecuteMeteorWorker(const io::Json& input,
                                const BinaryAccess& access);
int MaybeRunProgramWorker(int argc, char** argv);

}  // namespace q4t::server
