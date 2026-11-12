// Request-owned retrieval execution. Native HTTP and Responses share this
// implementation; callers own authentication, model permits and rate leases.
#pragma once

#include "q4t/server/embeddings_execution.h"
#include "q4t/server/vector_ingestion.h"

namespace q4t::server {
inline constexpr std::string_view kVectorQueryInstruction =
    "Instruct: Given a question about this project, retrieve the "
    "documentation passage that answers the question.\nQuery: ";

struct VectorSearchCallbacks {
  // Invoked only for rewrite_query=true. Must run the actual primary model
  // and account its prompt/output through the caller's existing request rate.
  std::function<ObjectReply(const std::vector<std::string>&,
                            std::vector<std::string>*)>
      rewrite;
  VectorTextEncoder encode;
  // Exact cost of all queries, including instruction and one EOS each. Called
  // once, even for an empty corpus (zero tokens still admits the request).
  std::function<ObjectReply(uint64_t)> reserve;
  EmbeddingStepExecutor embed;
};
struct VectorSearchUsage {
  uint64_t admitted_input = 0;
  uint64_t committed_input = 0;
  bool unknown = false;
};

// Complete source/owner/filter/index validation precedes model execution.
// Actual receipts survive failures and exceptions. No partial results, no
// automatic replay on concurrent membership changes, no model restarts.
ObjectReply ExecuteVectorSearchRequest(
    ObjectStore& objects, const BinaryAccess& access, const std::string& id,
    const io::Json& request, size_t dimensions,
    const VectorSearchCallbacks& callbacks, VectorSearchUsage* usage);

struct VectorRewritePlan {
  io::Json request;
  uint64_t output_tokens = 0;
};
// The generated request uses constrained JSON and treats all query strings
// as user data. Parsing requires the complete, exact number of rewrites.
ObjectReply PlanVectorRewrite(const std::vector<std::string>& queries,
                              VectorRewritePlan* plan);
ObjectReply ParseVectorRewrite(const std::vector<std::string>& original,
                               std::string_view content,
                               std::string_view finish_reason,
                               std::vector<std::string>* queries);
}  // namespace q4t::server
