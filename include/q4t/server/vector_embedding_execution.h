#pragma once

#include "q4t/server/embeddings_execution.h"
#include "q4t/server/vector_ingestion.h"

namespace q4t::server {
// Native ingestion's GPU bridge supplies the existing Qwen3 BF16 final row
// executor. This adapter owns one shared online TPM reservation, exact actual
// usage and FP32 L2 normalization. Public API RPM belongs to the operation
// which published the durable job, not to each chunk or its recovery attempt.
// Caller owns a current authenticated data permit through execution/cleanup.
ObjectReply ExecuteVectorIngestionWithQuota(
    ObjectStore& objects, const BinaryAccess& access,
    const VectorIngestionPrepared& prepared, ClientAdmission& clients,
    const ClientIdentity& client, const ClientLease& permit,
    ActualUsageCounter& usage, const EmbeddingStepExecutor& execute,
    VectorIngestionResult* result);
}  // namespace q4t::server
