#pragma once

#include "q4t/server/binary_store.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {

// Private checks: {schema, param, output?}. Output is JSON-encoded text;
// omission checks the schema definition only. No model or tool is executed.
ObjectReply ValidateToolResultSchemas(const io::Json& checks,
                                      const BinaryAccess& access,
                                      GrammarControl control = {});
int MaybeRunToolResultWorker(int argc, char** argv);

}  // namespace q4t::server
