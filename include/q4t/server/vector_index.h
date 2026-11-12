// Immutable native retrieval indexes reuse scoped, quota-accounted snapshots.
#pragma once

#include "q4t/server/input_snapshot.h"
#include "q4t/server/vector_search.h"

namespace q4t::server {
struct VectorTokenRange {
  size_t token_begin = 0, token_end = 0;
  size_t byte_begin = 0, byte_end = 0;
};
struct VectorIndexIdentity {
  std::string file_id, filename, source_sha256, text_sha256;
  std::string backend_sha256;
  size_t dimensions = 1024;
  VectorChunking chunking;
};
struct VectorIndexChunk {
  VectorTokenRange range;
  std::vector<float> embedding;
};
struct VectorIndex {
  VectorIndexIdentity identity;
  std::string text;
  std::vector<VectorIndexChunk> chunks;
  uint64_t input_tokens = 0;
};

// Exact pinned weights/tokenizer/pooling/dimensions and index algorithm. A new
// backend identity requires reindexing, never a silent mixed-vector search.
std::string VectorEmbeddingIdentity(size_t dimensions);
std::string VectorIndexBinding(const VectorIndexIdentity& identity);
io::Json VectorIndexIdentityJson(const VectorIndexIdentity& identity);
bool ParseVectorIndexIdentity(const io::Json& value,
                              VectorIndexIdentity* identity);

// Boundaries are exact prefix byte lengths from decoding each raw token with
// special tokens preserved. There are N+1 strictly increasing boundaries,
// from zero to text.size(). Encode must omit the embedding postprocessor EOS.
// UTF-8 splits retreat the end and advance the next start, so actual overlap
// can be smaller but never exceed the requested token overlap.
ObjectReply PlanVectorTokenChunks(std::string_view text,
                                  std::span<const size_t> boundaries,
                                  const VectorChunking& chunking,
                                  const VectorSearchControl& control,
                                  std::vector<VectorTokenRange>* chunks);

using VectorIndexEmbedding = std::function<ObjectReply(
    std::span<const uint32_t>, std::vector<float>*, uint64_t*)>;
// Actual executor receives each selected raw-token slice plus one trained EOS.
// It drains its GPU/slot owners before returning and reports committed input
// even on failure. No partial index escapes; usage is retained on every path.
ObjectReply BuildVectorIndex(const VectorIndexIdentity& identity,
                             std::string_view text,
                             std::span<const uint32_t> tokens,
                             std::span<const size_t> boundaries,
                             const VectorIndexEmbedding& embed,
                             const VectorSearchControl& control,
                             VectorIndex* index, uint64_t* committed_input);

// Logical data limit 64 MiB, two binary parts (UTF-8 and little-endian float32)
// plus bounded metadata. Temporary holds survive uncertain mutations; caller
// publishes the reference in its durable store before releasing the hold.
ObjectReply SaveVectorIndex(InputSnapshotStore& snapshots,
                            const BinaryAccess& access,
                            const VectorIndex& index, int hold_seconds,
                            InputSnapshotRef* reference,
                            InputSnapshotHold* hold);
ObjectReply ReadVectorIndex(InputSnapshotStore& snapshots,
                            const BinaryAccess& access,
                            const InputSnapshotRef& reference,
                            const VectorIndexIdentity& expected,
                            VectorIndex* index);
ObjectReply VectorIndexSearchChunks(const VectorIndex& index,
                                    const io::Json& attributes,
                                    const VectorSearchControl& control,
                                    std::vector<VectorSearchChunk>* chunks);
}  // namespace q4t::server
