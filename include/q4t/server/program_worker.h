// Isolated execution component. Its input is constructed by the authorized
// Responses executor, never accepted as a public client replay fingerprint.
#pragma once

#include "q4t/server/binary_store.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {

ObjectReply ExecuteProgramWorker(const io::Json& input,
                                 const BinaryAccess& access,
                                 GrammarControl control = {});
int MaybeRunProgramWorker(int argc, char** argv);

}  // namespace q4t::server
