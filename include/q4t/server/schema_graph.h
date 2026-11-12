#pragma once

#include <functional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

struct SchemaPrefixValue;
enum class OutputSchemaPurpose { kGeneration, kToolResult };

// Deterministic value identity for a bounded worker receipt. Exact decimal
// equality and object-key order independence match schema const/enum checks.
Status CanonicalSchemaValue(const io::Json& value, std::string* out);

// A checked, local-only schema graph. root must outlive this object. References
// retain identity instead of recursive textual expansion. The string checker
// must enforce both pattern and format; absence never silently drops a rule.
class OutputSchemaGraph {
 public:
  using StringCheck =
      std::function<Status(const io::Json&, std::string_view, size_t*, bool*)>;
  Status Init(const io::Json& root, bool strict, std::string* error_param,
              std::string_view path = "schema",
              OutputSchemaPurpose purpose = OutputSchemaPurpose::kGeneration);
  Status Check(const io::Json& value, const StringCheck& strings = {}) const;
  Status CheckNode(const io::Json& schema, const io::Json& value,
                   const StringCheck& strings = {}) const;
  Status CheckPrefix(const SchemaPrefixValue& value, size_t* work_remaining,
                     const StringCheck& strings = {}) const;
  const io::Json* RefTarget(const io::Json& schema) const;
  const io::Json& Root() const { return *root_; }
  const std::vector<const io::Json*>& Nodes() const { return order_; }

 private:
  Status Collect(const io::Json& schema, const std::string& path, size_t depth);
  Status Definition(const io::Json& schema, const std::string& path);
  Status Depth(const io::Json& schema, size_t depth,
               std::unordered_set<const io::Json*>* active,
               std::unordered_map<const io::Json*, size_t>* seen);
  Status Value(const io::Json& schema, const io::Json& value, size_t depth,
               size_t* work, const StringCheck& strings,
               std::unordered_set<const io::Json*>* active) const;
  Status Prefix(const io::Json& schema, const SchemaPrefixValue& value,
                size_t depth, size_t* work, const StringCheck& strings,
                std::unordered_set<const io::Json*>* active) const;
  Status Error(const std::string& path, const std::string& message);
  const io::Json* root_ = nullptr;
  std::string* error_param_ = nullptr;
  bool strict_ = false;
  OutputSchemaPurpose purpose_ = OutputSchemaPurpose::kGeneration;
  bool valid_ = false;
  size_t property_count_ = 0;
  size_t enum_count_ = 0;
  size_t string_count_ = 0;
  std::unordered_map<const io::Json*, std::string> nodes_;
  std::vector<const io::Json*> order_;
  std::unordered_map<const io::Json*, const io::Json*> references_;
};

}  // namespace q4t::server
