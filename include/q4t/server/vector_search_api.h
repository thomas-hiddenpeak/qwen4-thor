#pragma once

#include "q4t/server/model_preparation.h"
#include "q4t/server/vector_search_execution.h"

namespace q4t::server {
struct VectorRewriteResult {
  std::string content, finish_reason;
};
struct VectorNativeSearchCallbacks {
  VectorTextEncoder encode;
  EmbeddingStepExecutor embed;
  // The native internal model step reports its actual HTTP usage to the
  // enclosing request and charges this preparation owner, including failures.
  std::function<ObjectReply(const VectorRewritePlan&, ModelPreparation&,
                            VectorRewriteResult*)>
      rewrite;
};

// Standard standalone search: authenticated scope, one external RPM and the
// same TPM owner for real rewriting plus all query embeddings. HTTP caller
// holds the existing data permit and owns cancellation/model diagnostics.
ObjectReply HandleVectorSearchRequest(
    ObjectStore& objects, ClientAdmission& clients, bool configured,
    std::string_view method, std::string_view target, const std::string& body,
    const ClientIdentity& client, const BinaryAccess& access,
    ClientRateLease& rate, ActualUsageCounter& usage,
    const VectorNativeSearchCallbacks& callbacks);
}  // namespace q4t::server
