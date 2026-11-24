#pragma once

#include <string>
#include <string_view>

namespace q4t::server::detail {
// Called only once in a disposable, resource-limited worker after fd isolation.
bool RunIsolatedPython(std::string_view request, std::string* result);
}  // namespace q4t::server::detail
