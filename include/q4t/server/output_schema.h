#pragma once

#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Official structured-output schema graph. Unknown keywords remain errors;
// constraints are never silently handed to a compiler that ignores them.
inline constexpr char kOutputSchemaProfile[] = "q4t-json-schema-v2";
Status ValidateOutputSchema(const io::Json& schema, bool strict,
                            bool xml_parameters = false,
                            std::string* error_param = nullptr,
                            std::string_view path = "schema");
Status ValidateOutputValue(const io::Json& schema, const io::Json& value);
// The trained XML representation remains available for its unambiguous
// legacy subset. Rich schemas use one JSON-object native parameter; the wire
// function.arguments value is still the original, unwrapped argument object.
bool NeedsJsonToolArguments(const io::Json& function);
// The original schema subset has an independent native value checker. Rich
// schemas additionally require the isolated worker's semantic receipt.
bool NeedsSchemaGuard(const io::Json& schema);
std::string EncodeSchemaJson(const io::Json& value);
bool IsExactJsonInteger(std::string_view spelling);
std::string OutputJson(const io::Json& value);

}  // namespace q4t::server
