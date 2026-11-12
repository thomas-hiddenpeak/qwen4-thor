#pragma once

#include <functional>
#include <string>
#include <vector>

#include "q4t/server/schema_graph.h"

namespace q4t::server {

// Worker-only compiler. The string compiler returns an entire JSON string
// language, including quotes/escapes. The separate checker filters const/enum
// against every conjunct before those values become generation alternatives.
using SchemaStringCompiler =
    std::function<std::string(const std::vector<const io::Json*>&)>;
std::string CompileOutputSchemaEbnf(
    const OutputSchemaGraph& graph, const SchemaStringCompiler& strings,
    const OutputSchemaGraph::StringCheck& check_strings);

}  // namespace q4t::server
