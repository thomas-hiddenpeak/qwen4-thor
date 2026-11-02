#pragma once

#include <initializer_list>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Check the original number spelling before converting the bounded value.
bool IsInputInteger(const io::Json& value);
bool ValidInputName(std::string_view name);
bool ValidRequestId(std::string_view id);
bool ValidCancelToken(std::string_view token);
Status InputFields(const io::Json& object,
                   std::initializer_list<std::string_view> fields);
Status ValidateToolDefinitions(const io::Json& request);
Status ValidateRequestCredentials(const io::Json& request);
Status ValidateCancelContract(const io::Json& request);

}  // namespace q4t::server
