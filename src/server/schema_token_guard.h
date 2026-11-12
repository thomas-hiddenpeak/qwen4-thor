#pragma once

#include <memory>
#include <span>
#include <string>
#include <vector>

#include "q4t/server/schema_graph.h"
#include "q4t/server/schema_receipt.h"

namespace q4t::server {

// Worker-only semantic filter, layered on the lexical grammar mask. Caller
// owns the process/deadline limits. Invalid candidate values are masked;
// resource/dependency failure aborts with its distinct status, never fallback.
class SchemaTokenGuard {
 public:
  SchemaTokenGuard();
  ~SchemaTokenGuard();
  Status Init(const io::Json& configuration,
              OutputSchemaGraph::StringCheck strings = {});
  bool Active() const;
  bool Covers(std::string_view scope, const io::Json& schema) const;
  Status Filter(std::span<const std::string> vocabulary,
                std::span<const int32_t> stops, std::span<int32_t> mask);
  Status Accept(std::string_view bytes, bool stop);
  const std::vector<SchemaReceipt>& Receipts() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace q4t::server
