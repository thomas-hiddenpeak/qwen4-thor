// Independent dense WMMA reference: no q4t or cuBLAS implementation included.
#include <cuda_bf16.h>
#include <cuda_runtime.h>
#include <mma.h>

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
constexpr int kK = 2560, kN = 48, kRows = 16;
void Check(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
void Cuda(cudaError_t result) {
  Check(result == cudaSuccess, cudaGetErrorString(result));
}
__global__ void Dot(const __nv_bfloat16* w, const __nv_bfloat16* x,
                    float* y) {
  using namespace nvcuda;
  wmma::fragment<wmma::matrix_a, 16, 16, 16, __nv_bfloat16,
                 wmma::row_major> a;
  wmma::fragment<wmma::matrix_b, 16, 16, 16, __nv_bfloat16,
                 wmma::col_major> b;
  wmma::fragment<wmma::accumulator, 16, 16, 16, float> c;
  wmma::fill_fragment(c, 0.0f);
  for (int k = 0; k < kK; k += 16) {
    wmma::load_matrix_sync(a, w + blockIdx.x * 16 * kK + k, kK);
    wmma::load_matrix_sync(b, x + k, kK);
    wmma::mma_sync(c, a, b, c);
  }
  wmma::store_matrix_sync(y + blockIdx.x * 16, c, kN,
                          wmma::mem_col_major);
}
std::vector<uint16_t> Load(const std::filesystem::path& path, size_t n) {
  Check(std::filesystem::file_size(path) == n * 2, "input size");
  std::vector<uint16_t> data(n);
  std::ifstream in(path, std::ios::binary);
  in.read(reinterpret_cast<char*>(data.data()), n * 2);
  Check(bool(in), "input read");
  return data;
}
void Run(const std::filesystem::path& input,
         const std::filesystem::path& output) {
  Check(!std::filesystem::exists(output), "output exists");
  std::filesystem::create_directories(output);
  auto x = Load(input / "x", 4 * kK);
  x.resize(kRows * kK, 0);
  auto w = Load(input / "weight", kN * kK);
  __nv_bfloat16 *dx = nullptr, *dw = nullptr;
  float* dy = nullptr;
  Cuda(cudaMalloc(&dx, x.size() * 2));
  Cuda(cudaMalloc(&dw, w.size() * 2));
  Cuda(cudaMalloc(&dy, kRows * kN * 4));
  for (int test = 0; test < 3; ++test) {
    if (test != 0) {
      std::fill(x.begin(), x.end(), 0);
      std::fill(w.begin(), w.end(), 0);
    }
    if (test == 2) {
      // Four distinct K indices, including both endpoints, identify transpose,
      // row/head indexing and loop coverage; exact integer products/sums.
      const int indices[4] = {0, 15, 16, kK - 1};
      for (int row = 0; row < 4; ++row) {
        x[row * kK + indices[row]] = 0x3f80;  // 1
        for (int head = 0; head < kN; ++head) {
          const auto value = __float2bfloat16(float(head + 1));
          w[head * kK + indices[row]] =
              reinterpret_cast<const uint16_t&>(value);
        }
      }
    }
    Cuda(cudaMemcpy(dx, x.data(), x.size() * 2, cudaMemcpyHostToDevice));
    Cuda(cudaMemcpy(dw, w.data(), w.size() * 2, cudaMemcpyHostToDevice));
    Dot<<<3, 32>>>(dw, dx, dy);
    Cuda(cudaGetLastError());
    Cuda(cudaDeviceSynchronize());
    std::vector<float> y(kRows * kN);
    Cuda(cudaMemcpy(y.data(), dy, y.size() * 4, cudaMemcpyDeviceToHost));
    for (int row = 0; row < kRows; ++row) {
      for (int head = 0; head < kN; ++head) {
        if (test != 0 || row >= 4) {
          const float expected = test == 2 && row < 4 ? head + 1 : 0;
          Check(y[row * kN + head] == expected, "control mismatch");
        }
      }
    }
    std::ofstream out(output / (std::to_string(test) + ".f32"),
                      std::ios::binary);
    out.write(reinterpret_cast<const char*>(y.data()), y.size() * 4);
    Check(bool(out), "output write");
  }
  Cuda(cudaFree(dx));
  Cuda(cudaFree(dw));
  Cuda(cudaFree(dy));
}
}  // namespace
int main(int argc, char** argv) {
  try {
    Check(argc == 3, "usage: wmma_reference FIXTURE NEW_OUTPUT");
    Run(argv[1], argv[2]);
    std::cout << "PASS 3 independent WMMA launches and controls\n";
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
