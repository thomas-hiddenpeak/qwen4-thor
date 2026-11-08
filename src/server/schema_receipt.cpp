#include "q4t/server/schema_receipt.h"

#include "q4t/server/schema_graph.h"

namespace q4t::server {
namespace {
trace::Digest Hash(std::string_view text) {
  return trace::Sha256(
      {reinterpret_cast<const uint8_t*>(text.data()), text.size()});
}
}  // namespace

Status SchemaBindingDigest(std::string_view scope, const io::Json& schema,
                           trace::Digest* out) {
  std::string canonical;
  auto status = CanonicalSchemaValue(schema, &canonical);
  if (!status) return status;
  *out = Hash("q4t-schema-binding-v2:" + std::to_string(scope.size()) + ':' +
              std::string(scope) + ':' + canonical);
  return {};
}
Status SchemaValueDigest(const io::Json& value, trace::Digest* out) {
  std::string canonical;
  auto status = CanonicalSchemaValue(value, &canonical);
  if (status) *out = Hash("q4t-schema-value-v2:" + canonical);
  return status;
}
Status MakeSchemaReceipt(std::string_view scope, const io::Json& schema,
                         const io::Json& value, SchemaReceipt* out) {
  auto status = SchemaBindingDigest(scope, schema, &out->binding);
  if (status) status = SchemaValueDigest(value, &out->value);
  return status;
}

}  // namespace q4t::server
