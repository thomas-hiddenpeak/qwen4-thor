#pragma once

#include <cstdint>
#include <stdexcept>
#include <string_view>

namespace q4t::server {

class SchemaDependencyUnavailable : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};
using SchemaIdnaRegister = int (*)(const uint8_t*, const uint8_t*, uint8_t**,
                                   int);
bool CheckSchemaIdnaLabel(std::string_view label, SchemaIdnaRegister validate);
bool CheckSchemaIdnaLabel(std::string_view label);

}  // namespace q4t::server
