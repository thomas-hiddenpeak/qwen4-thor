#pragma once

#include <string>
#include <string_view>

namespace q4t::server {

// Worker-only expansion of planner-owned custom formats. User grammars match
// decoded input; the generated grammar matches its model JSON representation.
// Allocation/compiler exceptions propagate to the existing worker boundary.
std::string ExpandCustomGrammarFormats(std::string_view structural_tag);
// Encode a regular language over Unicode scalars into canonical JSON string
// spellings. Used by the schema compiler as well as custom-tool input.
std::string EncodeJsonStringGrammar(std::string_view ebnf);

}  // namespace q4t::server
