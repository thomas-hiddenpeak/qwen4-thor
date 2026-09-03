// End-to-end PLE gather test: ngram hash -> SSD page read -> FP8->BF16.
//
// Uses a small synthetic sidecar + small hash params (so row_ids land inside a
// few-KB file) to verify the ORCHESTRATION and LAYOUT are correct: each token's
// ngram_heads rows are gathered token-major and converted to BF16, matching a
// CPU reference that decodes the same file rows. The hash, reader, and
// conversion primitives are each validated separately against the real
// checkpoint params / 51.2 GB sidecar / e4m3 reference.
#include "q4t/ple/ple_embedding.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::ple::NgramHashParams;
using q4t::ple::PleEmbedding;
using q4t::Status;

// CPU reference e4m3fn decode (same as ple_fp8_convert_test).
float DecodeE4M3(uint8_t b) {
  const int sign = (b >> 7) & 1;
  const int exp = (b >> 3) & 0xF;
  const int mant = b & 0x7;
  if (exp == 15 && mant == 7) return std::nan("");
  float val;
  if (exp == 0) {
    val = static_cast<float>(std::ldexp(static_cast<double>(mant), -9));
  } else {
    val = static_cast<float>(
        std::ldexp(1.0 + static_cast<double>(mant) / 8.0, exp - 7));
  }
  return sign ? -val : val;
}

// float -> BF16 bits (top 16 bits of float32).
uint16_t FloatToBf16Bits(float f) {
  uint32_t bits;
  std::memcpy(&bits, &f, sizeof(bits));
  return static_cast<uint16_t>(bits >> 16);
}

float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}

bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

std::string MakePatternFile(size_t nbytes) {
  const char* path = "/tmp/q4t_ple_e2e_test.bin";
  int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0644);
  if (fd < 0) return "";
  std::vector<uint8_t> buf(nbytes);
  for (size_t i = 0; i < nbytes; ++i) {
    buf[i] = static_cast<uint8_t>((i * 7 + 13) & 0xFF);
  }
  ssize_t w = write(fd, buf.data(), nbytes);
  close(fd);
  return w == static_cast<ssize_t>(nbytes) ? path : "";
}

}  // namespace

Q4T_TEST(ple_e2e_gather_matches_cpu_reference) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }

  const size_t kRowBytes = 160;
  // Small params so row_ids stay inside a 500-row file.
  NgramHashParams params;
  params.ngram_size = 3;
  params.heads_per_ngram = 2;  // heads = 4
  params.multipliers = {123456789LL, 987654321LL, 314159265LL};
  params.head_vocab_sizes = {97, 89, 83, 79};
  params.head_offsets = {0, 97, 186, 269};  // max row_id = 269 + 78 = 347
  const int heads = params.ngram_heads();
  const size_t embed_dim = static_cast<size_t>(heads) * kRowBytes;
  const int64_t kTotalRows = 500;

  const std::string path = MakePatternFile(kTotalRows * kRowBytes);
  Q4T_CHECK(!path.empty());

  PleEmbedding::Config cfg;
  cfg.sidecar_path = path;
  cfg.row_bytes = kRowBytes;
  cfg.total_rows = kTotalRows;
  cfg.hash_params = params;
  cfg.eos_token_id = 248044;
  cfg.capacity_tokens = 64;

  PleEmbedding* emb = nullptr;
  Status s = PleEmbedding::Create(cfg, &emb);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(emb->ple_embed_dim() == embed_dim);

  const size_t n_tokens = 5;
  const int64_t tokens[] = {1000, 2000, 3000, 4000, 5000};
  const int64_t history[] = {10, 11, 12, 13, 14, 15, 16, 17, 18, 19};

  // GPU output.
  std::vector<uint16_t> h_out(n_tokens * embed_dim, 0);
  uint16_t* d_out = nullptr;
  Q4T_CHECK(cudaMalloc(&d_out, n_tokens * embed_dim * 2) == cudaSuccess);
  cudaStream_t stream = nullptr;
  Q4T_CHECK(cudaStreamCreate(&stream) == cudaSuccess);

  s = emb->Gather(tokens, n_tokens, history, d_out, stream);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(cudaStreamSynchronize(stream) == cudaSuccess);
  Q4T_CHECK(cudaMemcpy(h_out.data(), d_out, n_tokens * embed_dim * 2,
                       cudaMemcpyDeviceToHost) == cudaSuccess);

  // CPU reference: recompute row ids, decode the matching file rows.
  std::vector<int64_t> row_ids(n_tokens * heads);
  s = emb->ComputeRowIds(tokens, n_tokens, history, row_ids.data());
  Q4T_CHECK(s.ok());

  // Load the file.
  std::vector<uint8_t> file;
  {
    int fd = open(path.c_str(), O_RDONLY);
    Q4T_CHECK(fd >= 0);
    file.resize(kTotalRows * kRowBytes);
    Q4T_CHECK(pread(fd, file.data(), file.size(), 0) ==
              static_cast<ssize_t>(file.size()));
    close(fd);
  }

  int mismatches = 0;
  for (size_t t = 0; t < n_tokens && mismatches < 5; ++t) {
    for (int h = 0; h < heads; ++h) {
      const int64_t row = row_ids[t * heads + h];
      Q4T_CHECK(row >= 0 && row < kTotalRows);
      for (size_t b = 0; b < kRowBytes && mismatches < 5; ++b) {
        const uint8_t fp8byte = file[row * kRowBytes + b];
        const float expected = DecodeE4M3(fp8byte);
        const uint16_t got = h_out[t * embed_dim + h * kRowBytes + b];
        const float got_f = Bf16ToFloat(got);
        const bool ok = std::isnan(expected)
                            ? std::isnan(got_f)
                            : (FloatToBf16Bits(expected) == got);
        if (!ok) {
          ++mismatches;
          std::printf("  t=%zu h=%d b=%zu row=%lld: expected %f got %f\n", t, h,
                      b, (long long)row, expected, got_f);
        }
      }
    }
  }
  Q4T_CHECK(mismatches == 0);

  cudaFree(d_out);
  cudaStreamDestroy(stream);
  delete emb;
  unlink(path.c_str());
  return true;
}

Q4T_TEST(ple_e2e_capacity_guard) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  const size_t kRowBytes = 160;
  NgramHashParams params;
  params.ngram_size = 3;
  params.heads_per_ngram = 2;
  params.multipliers = {1LL, 3LL, 5LL};
  params.head_vocab_sizes = {97, 89, 83, 79};
  params.head_offsets = {0, 97, 186, 269};
  const int64_t kTotalRows = 500;
  const std::string path = MakePatternFile(kTotalRows * kRowBytes);
  Q4T_CHECK(!path.empty());

  PleEmbedding::Config cfg;
  cfg.sidecar_path = path;
  cfg.row_bytes = kRowBytes;
  cfg.total_rows = kTotalRows;
  cfg.hash_params = params;
  cfg.eos_token_id = 248044;
  cfg.capacity_tokens = 2;  // small

  PleEmbedding* emb = nullptr;
  Status s = PleEmbedding::Create(cfg, &emb);
  Q4T_CHECK(s.ok());

  // 3 tokens > capacity 2 -> ComputeRowIds must fail.
  const int64_t tokens[] = {1, 2, 3};
  const int64_t history[] = {1, 2, 3, 4, 5, 6};
  std::vector<int64_t> row_ids(3 * params.ngram_heads());
  s = emb->ComputeRowIds(tokens, 3, history, row_ids.data());
  Q4T_CHECK(!s.ok());

  delete emb;
  unlink(path.c_str());
  return true;
}
