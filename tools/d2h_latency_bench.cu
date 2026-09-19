// Microbenchmark: D2H latency of a 2048-byte (E=512 x int32) copy, the exact
// pattern of MoERoutedForward's per-expert count readback (moe_gemm.cu:359).
// Determine whether the ~4.4ms observed in nsys (GPU idle during the window)
// is pageable-host staging latency, and whether pinned memory removes it.
#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>
#include <vector>

#define CUDA_CHECK(x)                                                        \
  do {                                                                        \
    cudaError_t err__ = (x);                                                  \
    if (err__ != cudaSuccess) {                                               \
      fprintf(stderr, "CUDA error %s at %s:%d\n", cudaGetErrorString(err__),  \
              __FILE__, __LINE__);                                            \
      return 1;                                                               \
    }                                                                         \
  } while (0)

__global__ void BurnKernel(float* buf, int iters) {
  float v = buf[threadIdx.x] + 1.0f;
  for (int i = 0; i < iters; ++i) v = v * 1.000001f + 0.000001f;
  if (v == 12345.678f) buf[threadIdx.x] = v;
}

static double TimeMs(int iters, bool use_pinned, bool with_prior_kernels,
                     int32_t* d_counts, int32_t* pageable, int32_t* pinned,
                     float* burn_buf, cudaStream_t stream) {
  const int E = 512;
  const size_t bytes = E * sizeof(int32_t);
  double total = 0.0;
  for (int it = 0; it < iters; ++it) {
    CUDA_CHECK(cudaMemsetAsync(d_counts, 0, bytes, stream));
    if (with_prior_kernels) {
      BurnKernel<<<16, 256, 0, stream>>>(burn_buf, 200000);
    }
    cudaEvent_t t0, t1;
    CUDA_CHECK(cudaEventCreate(&t0));
    CUDA_CHECK(cudaEventCreate(&t1));
    CUDA_CHECK(cudaEventRecord(t0, stream));
    int32_t* dst = use_pinned ? pinned : pageable;
    CUDA_CHECK(cudaMemcpyAsync(dst, d_counts, bytes, cudaMemcpyDeviceToHost,
                               stream));
    CUDA_CHECK(cudaStreamSynchronize(stream));
    CUDA_CHECK(cudaEventRecord(t1, stream));
    CUDA_CHECK(cudaEventSynchronize(t1));
    float ms = 0.0f;
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    total += ms;
    CUDA_CHECK(cudaEventDestroy(t0));
    CUDA_CHECK(cudaEventDestroy(t1));
  }
  return total / iters;
}

int main() {
  const int E = 512;
  const size_t bytes = E * sizeof(int32_t);
  int32_t* d_counts = nullptr;
  CUDA_CHECK(cudaMallocAsync(&d_counts, bytes, 0));
  int32_t* pageable = new int32_t[E];
  int32_t* pinned = nullptr;
  CUDA_CHECK(cudaHostAlloc(&pinned, bytes, cudaHostAllocDefault));
  float* burn_buf = nullptr;
  CUDA_CHECK(cudaMalloc(&burn_buf, 16 * 256 * sizeof(float)));
  cudaStream_t stream;
  CUDA_CHECK(cudaStreamCreate(&stream));

  const int warmup = 20;
  const int iters = 200;
  for (int i = 0; i < warmup; ++i) {
    TimeMs(1, false, false, d_counts, pageable, pinned, burn_buf, stream);
    TimeMs(1, true, false, d_counts, pageable, pinned, burn_buf, stream);
    TimeMs(1, false, true, d_counts, pageable, pinned, burn_buf, stream);
    TimeMs(1, true, true, d_counts, pageable, pinned, burn_buf, stream);
  }

  double a = TimeMs(iters, false, false, d_counts, pageable, pinned, burn_buf, stream);
  double b = TimeMs(iters, true, false, d_counts, pageable, pinned, burn_buf, stream);
  double c = TimeMs(iters, false, true, d_counts, pageable, pinned, burn_buf, stream);
  double d = TimeMs(iters, true, true, d_counts, pageable, pinned, burn_buf, stream);

  printf("D2H 2048B (E=512 int32) + streamSync, mean over %d iters:\n", iters);
  printf("  A empty-stream  pageable : %8.3f ms\n", a);
  printf("  B empty-stream  pinned   : %8.3f ms\n", b);
  printf("  C +300us kernels pageable: %8.3f ms\n", c);
  printf("  D +300us kernels pinned  : %8.3f ms\n", d);
  printf("  => prior-kernel overhead (C-A): %8.3f ms\n", c - a);
  printf("  => pinned saves vs pageable (C-D): %8.3f ms\n", c - d);

  CUDA_CHECK(cudaFreeAsync(d_counts, 0));
  delete[] pageable;
  CUDA_CHECK(cudaFreeHost(pinned));
  CUDA_CHECK(cudaFree(burn_buf));
  CUDA_CHECK(cudaStreamDestroy(stream));
  return 0;
}
