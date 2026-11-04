#pragma once

#include <initializer_list>
#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Check the original number spelling before converting the bounded value.
bool IsInputInteger(const io::Json& value);
bool ValidInputName(std::string_view name);
bool ValidRequestId(std::string_view id);
bool ValidCancelToken(std::string_view token);
// Identifier fields use dots; other object keys use JSON-quoted brackets.
// Array indices are appended separately, so keys containing dots/brackets
// cannot be confused with nested fields or array positions.
std::string InputParamPath(std::string_view parent, std::string_view key);
Status InputFields(const io::Json& object,
                   std::initializer_list<std::string_view> fields,
                   std::string* error_param = nullptr,
                   std::string_view path = "body");
Status ValidateToolDefinitions(const io::Json& request,
                               std::string* error_param = nullptr);
Status ValidateRequestCredentials(const io::Json& request);
Status ValidateCancelContract(const io::Json& request);

}  // namespace q4t::server
