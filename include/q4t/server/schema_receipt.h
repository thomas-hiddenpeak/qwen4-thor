#pragma once

#include <string_view>

#include "q4t/io/json.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {

// A receipt is emitted only for a fully checked value in the isolated worker.
// Bind it to the exact requested schema and response/tool scope, then compare
// the parser's reconstructed value before publishing a successful terminal.
struct SchemaReceipt {
  trace::Digest binding{};
  trace::Digest value{};
  bool operator==(const SchemaReceipt&) const = default;
};
static_assert(sizeof(SchemaReceipt) == 64);
Status SchemaBindingDigest(std::string_view scope, const io::Json& schema,
                           trace::Digest* out);
Status SchemaValueDigest(const io::Json& value, trace::Digest* out);
Status MakeSchemaReceipt(std::string_view scope, const io::Json& schema,
                         const io::Json& value, SchemaReceipt* out);
// Distinct domains prevent a refusal from substituting for a schema value.
SchemaReceipt MakeRefusalReceipt(std::string_view refusal);

}  // namespace q4t::server
