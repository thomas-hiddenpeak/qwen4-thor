#pragma once

#include <memory>
#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Complete siblings are ordinary JSON values. Only the final open child needs
// prefix metadata, so no duplicated tree or invented JSON sentinel is needed.
struct SchemaPrefixValue {
  io::Json value;
  bool started = false;
  bool complete = false;
  std::unique_ptr<SchemaPrefixValue> pending;
  bool has_pending_key = false;
  std::string pending_key;
};

// Worker-only, bounded parse of one JSON value, including an incomplete UTF-8
// or escaped string suffix. On a complete root, consumed stops after trailing
// JSON whitespace: the caller must handle/reject any remaining protocol bytes.
Status ParseSchemaPrefix(std::string_view text, SchemaPrefixValue* out,
                         size_t* consumed, size_t* work_remaining);

}  // namespace q4t::server
