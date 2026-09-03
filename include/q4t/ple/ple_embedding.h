// PLE end-to-end gather: orchestrate n-gram hash -> SSD page read -> FP8->BF16
// conversion into a [tokens, ple_embed_dim] BF16 embedding on the GPU.
//
// This is the glue that turns a token sequence (plus its per-token 2-token
// history) into PLE embeddings. It composes the three verified building
// blocks:
//   - ComputeNgramRowIds (CPU, ngram_hash.h)
//   - PlePageReader::Gather (CPU io_uring, page_reader.h)
//   - ConvertFp8ToBf16Async (CUDA, fp8_convert.h)
//
// Layout: each token yields `ngram_heads` rows of `row_bytes` FP8 bytes; the
// reader scatters them token-major, so the gathered bytes are already laid out
// as [tokens, ngram_heads * row_bytes] = [tokens, ple_embed_dim]. The FP8->BF16
// conversion preserves that layout, producing [tokens, ple_embed_dim] BF16.
//
// The per-table `weight_scale` is NOT applied here; it is multiplied in the PLE
// layer forward (after the 16-head reduce), matching the SGLang reference.
#pragma once

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>
#include <string>

#include "q4t/ple/ngram_hash.h"
#include "q4t/status.h"

namespace q4t {
namespace ple {

class PlePageReader;

class PleEmbedding {
 public:
  struct Config {
    std::string sidecar_path;
    size_t row_bytes = 160;
    int64_t total_rows = 0;
    NgramHashParams hash_params;
    int64_t eos_token_id = 0;
    // Max tokens per Gather call; sizes the pinned staging + GPU scratch.
    size_t capacity_tokens = 8192;
  };

  static Status Create(const Config& config, PleEmbedding** out);
  ~PleEmbedding();
  PleEmbedding(const PleEmbedding&) = delete;
  PleEmbedding& operator=(const PleEmbedding&) = delete;

  // (1) Pure CPU: compute [n_tokens * ngram_heads] row ids (token-major).
  //   tokens:  [n_tokens] current token ids.
  //   history: [n_tokens, ngram_size-1] the ngram_size-1 tokens preceding each
  //            token, ordered oldest -> newest. Sequence boundaries must be
  //            filled with eos_token_id by the caller.
  //   out_row_ids: must hold n_tokens * ngram_heads() int64.
  Status ComputeRowIds(const int64_t* tokens, size_t n_tokens,
                       const int64_t* history, int64_t* out_row_ids) const;

  // (2) Read + convert: gather `n_rows` table rows into `out_bf16` (GPU),
  //   which must hold n_rows * row_bytes BF16 values (2 bytes each). The read
  //   and conversion are enqueued on `stream`.
  Status GatherRows(const int64_t* row_ids, size_t n_rows, uint16_t* out_bf16,
                    cudaStream_t stream) const;

  // (3) Combined: tokens + history -> [n_tokens, ple_embed_dim] BF16 (GPU).
  //   out_bf16 must hold n_tokens * ple_embed_dim() BF16 values.
  Status Gather(const int64_t* tokens, size_t n_tokens,
                const int64_t* history, uint16_t* out_bf16,
                cudaStream_t stream) const;

  size_t capacity_tokens() const;
  size_t ple_embed_dim() const;
  int64_t eos_token_id() const;
  const NgramHashParams& hash_params() const;

 private:
  struct Impl;
  explicit PleEmbedding(Impl* impl);
  // Mutable: Gather mutates the reader's scratch and the host row-id scratch.
  mutable Impl* impl_;
};

}  // namespace ple
}  // namespace q4t
