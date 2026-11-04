#pragma once

#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Deliberately finite profile. Unknown schema keywords are errors, including
// keywords an external grammar compiler might otherwise ignore.
inline constexpr char kOutputSchemaProfile[] = "q4t-json-schema-v1";
Status ValidateOutputSchema(const io::Json& schema, bool strict,
                            bool xml_parameters = false,
                            std::string* error_param = nullptr,
                            std::string_view path = "schema");
Status ValidateOutputValue(const io::Json& schema, const io::Json& value);
bool IsExactJsonInteger(std::string_view spelling);
std::string OutputJson(const io::Json& value);

}  // namespace q4t::server
