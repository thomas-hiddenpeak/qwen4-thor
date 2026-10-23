// PLE end-to-end gather implementation.
#include "q4t/ple/ple_embedding.h"

#include <cuda_runtime.h>

#include <cstring>
#include <vector>

#include "q4t/ple/fp8_convert.h"
#include "q4t/diagnostic/execution_observer.h"
#include "q4t/ple/page_reader.h"

namespace q4t {
namespace ple {

struct PleEmbedding::Impl {
  Config config;
  PlePageReader* reader = nullptr;
  uint8_t* staging = nullptr;  // pinned host FP8 rows
  size_t staging_bytes = 0;
  uint8_t* gpu_fp8 = nullptr;  // GPU scratch FP8 rows
  size_t gpu_fp8_bytes = 0;
  int64_t* host_row_ids = nullptr;  // pinned host row ids
  size_t host_row_ids_cap = 0;
  cudaEvent_t upload_complete = nullptr;
  bool upload_pending = false;
  bool upload_failed = false;
};

PleEmbedding::PleEmbedding(Impl* impl) : impl_(impl) {}

PleEmbedding::~PleEmbedding() {
  if (!impl_) return;
  // The pinned source and FP8 scratch outlive every queued conversion.
  if (impl_->upload_pending) cudaEventSynchronize(impl_->upload_complete);
  if (impl_->upload_complete) cudaEventDestroy(impl_->upload_complete);
  if (impl_->reader) delete impl_->reader;
  if (impl_->staging) cudaFreeHost(impl_->staging);
  if (impl_->gpu_fp8) cudaFree(impl_->gpu_fp8);
  if (impl_->host_row_ids) cudaFreeHost(impl_->host_row_ids);
  delete impl_;
  impl_ = nullptr;
}

Status PleEmbedding::Create(const Config& config, PleEmbedding** out) {
  *out = nullptr;
  const int heads = config.hash_params.ngram_heads();
  if (heads <= 0) return Status::Fail("invalid ngram_heads");
  if (config.row_bytes == 0 || config.capacity_tokens == 0) {
    return Status::Fail("row_bytes and capacity_tokens must be positive");
  }
  const size_t rows_per_token = static_cast<size_t>(heads);
  const size_t max_rows = config.capacity_tokens * rows_per_token;
  const size_t fp8_bytes = max_rows * config.row_bytes;

  Impl* impl = new Impl();
  impl->config = config;

  Status s = PlePageReader::Create(config.sidecar_path, config.row_bytes, 0, 0,
                                    config.total_rows, &impl->reader);
  if (!s.ok()) {
    delete impl;
    return s;
  }

  if (cudaHostAlloc(&impl->staging, fp8_bytes, cudaHostAllocDefault) !=
      cudaSuccess) {
    delete impl->reader;
    delete impl;
    return Status::Fail("cudaHostAlloc staging failed");
  }
  impl->staging_bytes = fp8_bytes;

  if (cudaMalloc(&impl->gpu_fp8, fp8_bytes) != cudaSuccess) {
    cudaFreeHost(impl->staging);
    delete impl->reader;
    delete impl;
    return Status::Fail("cudaMalloc gpu_fp8 failed");
  }
  impl->gpu_fp8_bytes = fp8_bytes;

  if (cudaHostAlloc(&impl->host_row_ids, max_rows * sizeof(int64_t),
                    cudaHostAllocDefault) != cudaSuccess) {
    cudaFree(impl->gpu_fp8);
    cudaFreeHost(impl->staging);
    delete impl->reader;
    delete impl;
    return Status::Fail("cudaHostAlloc host_row_ids failed");
  }
  impl->host_row_ids_cap = max_rows;
  if (cudaEventCreateWithFlags(&impl->upload_complete, cudaEventDisableTiming) !=
      cudaSuccess) {
    cudaFreeHost(impl->host_row_ids);
    cudaFree(impl->gpu_fp8);
    cudaFreeHost(impl->staging);
    delete impl->reader;
    delete impl;
    return Status::Fail("cudaEventCreate PLE upload failed");
  }

  *out = new PleEmbedding(impl);
  return Status();
}

size_t PleEmbedding::working_memory_bytes() const {
  size_t total = 0;
  if (impl_->reader) {
    total += impl_->reader->pool_bytes();
    total += kRingBytesEstimate;
    total += impl_->reader->scratch_bytes();
  }
  total += impl_->staging_bytes;
  total += impl_->gpu_fp8_bytes;
  total += impl_->host_row_ids_cap * sizeof(int64_t);
  return total;
}

size_t PleEmbedding::capacity_tokens() const {
  return impl_->config.capacity_tokens;
}
size_t PleEmbedding::ple_embed_dim() const {
  return static_cast<size_t>(impl_->config.hash_params.ngram_heads()) *
         impl_->config.row_bytes;
}
int64_t PleEmbedding::eos_token_id() const {
  return impl_->config.eos_token_id;
}
const NgramHashParams& PleEmbedding::hash_params() const {
  return impl_->config.hash_params;
}

Status PleEmbedding::ComputeRowIds(const int64_t* tokens, size_t n_tokens,
                                    const int64_t* history,
                                    int64_t* out_row_ids) const {
  const NgramHashParams& p = impl_->config.hash_params;
  const int heads = p.ngram_heads();
  const int ngram = p.ngram_size;
  const int hist = ngram - 1;
  if (static_cast<size_t>(heads) * n_tokens >
      static_cast<size_t>(heads) * impl_->config.capacity_tokens) {
    return Status::Fail("n_tokens exceeds capacity");
  }
  for (size_t t = 0; t < n_tokens; ++t) {
    int64_t context[ngram];
    for (int k = 0; k < hist; ++k) {
      context[k] = history[t * hist + k];
    }
    context[hist] = tokens[t];
    ComputeNgramRowIds(p, context, impl_->config.eos_token_id,
                       out_row_ids + t * heads);
  }
  return Status();
}

Status PleEmbedding::GatherRows(const int64_t* row_ids, size_t n_rows,
                                 uint16_t* out_bf16, cudaStream_t stream) const {
  const size_t row_bytes = impl_->config.row_bytes;
  if (n_rows > impl_->host_row_ids_cap) {
    return Status::Fail("n_rows exceeds capacity");
  }
  if (impl_->upload_failed) {
    return Status::Fail("PLE upload completion failed; recreate embedding");
  }
  // A CUDA stream orders device work, not the CPU writes performed by the
  // reader. Wait before overwriting the pinned source. Completion includes
  // conversion so the shared FP8 scratch is also safe across streams.
  if (impl_->upload_pending) {
    diagnostic::Scope wait("ple.previous_upload_wait");
    if (cudaEventSynchronize(impl_->upload_complete) != cudaSuccess) {
      impl_->upload_failed = true;
      return Status::Fail("PLE previous upload completion failed");
    }
    impl_->upload_pending = false;
  }
  // The reader scatters rows into the pinned staging buffer (token-major).
  ReadStats stats;
  Status s;
  {
    diagnostic::Scope read("ple.reader");
    s = impl_->reader->Gather(row_ids, n_rows, impl_->staging, &stats);
    // Existing reader counters: IO wall ns, scatter wall ns, submitted bytes.
    read.SetCounters(stats.io_ns, stats.scatter_ns, stats.submitted_bytes);
  }
  if (!s.ok()) return s;

  // Error exits also drain submitted work before releasing buffer ownership.
  auto fail_upload = [&](const char* message) {
    if (cudaStreamSynchronize(stream) != cudaSuccess) {
      impl_->upload_failed = true;
      return Status::Fail(std::string(message) + "; stream completion failed");
    }
    return Status::Fail(message);
  };
  // H2D the FP8 rows, then convert to BF16 on the stream.
  const size_t fp8_bytes = n_rows * row_bytes;
  diagnostic::Scope upload("ple.upload_submit");
  if (cudaMemcpyAsync(impl_->gpu_fp8, impl_->staging, fp8_bytes,
                      cudaMemcpyHostToDevice, stream) != cudaSuccess) {
    return fail_upload("H2D staging->gpu_fp8 failed");
  }
  if (ConvertFp8ToBf16Async(impl_->gpu_fp8, out_bf16, fp8_bytes, stream) !=
      cudaSuccess) {
    return fail_upload("FP8->BF16 conversion failed");
  }
  if (cudaEventRecord(impl_->upload_complete, stream) != cudaSuccess) {
    return fail_upload("PLE upload event record failed");
  }
  impl_->upload_pending = true;
  return Status();
}

Status PleEmbedding::Gather(const int64_t* tokens, size_t n_tokens,
                             const int64_t* history, uint16_t* out_bf16,
                             cudaStream_t stream) const {
  diagnostic::Scope gather("ple.gather", -1, static_cast<int>(n_tokens));
  const int heads = impl_->config.hash_params.ngram_heads();
  const size_t n_rows = n_tokens * static_cast<size_t>(heads);
  if (n_rows > impl_->host_row_ids_cap) {
    return Status::Fail("n_tokens exceeds capacity");
  }
  Status s = ComputeRowIds(tokens, n_tokens, history, impl_->host_row_ids);
  if (!s.ok()) return s;
  return GatherRows(impl_->host_row_ids, n_rows, out_bf16, stream);
}

}  // namespace ple
}  // namespace q4t
