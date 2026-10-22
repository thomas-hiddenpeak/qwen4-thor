// Direct GPU projection/mapping contract, run only after the HTTP gate.
#include <cuda_runtime.h>
#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "q4t/model/linear.h"
#include "q4t/mtp/mtp.h"
#include "q4t/mtp/subset_head.h"

namespace q4t::mtp {
Status DraftArgmax(const MtpModel&, const uint16_t*, int, int32_t*, cudaStream_t);
}
namespace {
void Check(bool ok, const char* message) {
  if (!ok) { std::fprintf(stderr, "FAIL %s\n", message); std::exit(1); }
}
float Float(uint16_t x) { return std::bit_cast<float>(uint32_t{x} << 16); }
uint16_t Bf16(float x) {
  uint32_t bits = std::bit_cast<uint32_t>(x);
  bits += 0x7fff + ((bits >> 16) & 1);
  return static_cast<uint16_t>(bits >> 16);
}
void* Alloc(size_t n) {
  void* p = nullptr;
  Check(cudaMalloc(&p, n) == cudaSuccess, "allocate");
  return p;
}
}  // namespace
int main(int argc, char** argv) {
  Check(argc == 3, "artifact and model required");
  q4t::mtp::SubsetHeadData data;
  auto status = q4t::mtp::ReadSubsetHead(argv[1], argv[2], 248320, 2560, &data);
  if (!status.ok()) std::fprintf(stderr, "%s\n", status.message().c_str());
  Check(status.ok(), "artifact load");
  constexpr int rows = q4t::mtp::kSubsetRows, hs = 2560, max_t = 17;
  q4t::mtp::MtpModel model;
  model.subset_head = static_cast<uint16_t*>(Alloc(data.weights.size() * 2));
  model.subset_ids = static_cast<int32_t*>(Alloc(data.ids.size() * 4));
  Check(cudaMemcpy(model.subset_head, data.weights.data(), data.weights.size()*2,
                   cudaMemcpyHostToDevice) == cudaSuccess, "upload weights");
  Check(cudaMemcpy(model.subset_ids, data.ids.data(), data.ids.size()*4,
                   cudaMemcpyHostToDevice) == cudaSuccess, "upload ids");
  auto* input = static_cast<uint16_t*>(Alloc(max_t * hs * 2));
  auto* logits = static_cast<uint16_t*>(Alloc(max_t * rows * 2));
  auto* tokens = static_cast<int32_t*>(Alloc(max_t * 4));
  constexpr size_t ws_bytes = 32u << 20;
  void* ws = Alloc(ws_bytes);
  std::vector<uint16_t> x(max_t * hs), actual(max_t * rows), repeat(actual.size());
  std::vector<int32_t> chosen(max_t);
  for (int i = 0; i < max_t * hs; ++i)
    x[i] = Bf16(float((i * 17 + 31) % 127 - 63) / 64);
  Check(cudaMemcpy(input, x.data(), x.size()*2, cudaMemcpyHostToDevice) ==
            cudaSuccess, "upload input");
  double maximum = 0;
  for (int t : {1, 4, 17}) {
    for (int attempt = 0; attempt < 2; ++attempt) {
      Check(q4t::model::Bf16Gemm(input, model.subset_head, logits, t, rows, hs,
                 1, 0, ws, ws_bytes, nullptr).status == CUBLAS_STATUS_SUCCESS, "project");
      auto& output = attempt ? repeat : actual;
      Check(cudaMemcpy(output.data(), logits, size_t(t)*rows*2,
                        cudaMemcpyDeviceToHost) == cudaSuccess, "read logits");
    }
    Check(std::equal(actual.begin(), actual.begin() + t*rows, repeat.begin()),
          "repeated projection differs");
    Check(q4t::mtp::DraftArgmax(model, logits, t, tokens, nullptr).ok(), "argmax");
    Check(cudaMemcpy(chosen.data(), tokens, t*4, cudaMemcpyDeviceToHost) ==
              cudaSuccess, "read tokens");
    for (int row = 0; row < t; ++row) {
      int best = 0;
      for (int j = 0; j < rows; ++j) {
        Check(std::isfinite(Float(actual[row*rows+j])), "finite logits");
        if (Float(actual[row*rows+j]) > Float(actual[row*rows+best])) best = j;
      }
      Check(chosen[row] == data.ids[best], "original-ID argmax");
      // Independent FP64 dot product; conservative FP32 accumulation plus
      // final BF16 rounding bound, fixed before execution.
      for (int j : {0, 1, 255, 32768, rows-1, best}) {
        double sum = 0, absolute = 0;
        for (int h = 0; h < hs; ++h) {
          const double term = double(Float(x[row*hs+h])) *
                              Float(data.weights[size_t(j)*hs+h]);
          sum += term;
          absolute += std::abs(term);
        }
        const double error = std::abs(Float(actual[row*rows+j]) - sum);
        const double bound = std::abs(sum)/256 + absolute*0.000306 + 1e-7;
        maximum = std::max(maximum, error);
        Check(error <= bound, "FP64 projection bound");
      }
    }
  }
  // Equal logits must choose smallest original ID, for all batched rows.
  Check(cudaMemset(logits, 0, max_t*rows*2) == cudaSuccess, "clear logits");
  Check(q4t::mtp::DraftArgmax(model, logits, max_t, tokens, nullptr).ok(), "tie");
  Check(cudaMemcpy(chosen.data(), tokens, max_t*4, cudaMemcpyDeviceToHost) ==
            cudaSuccess, "read ties");
  for (int id : chosen) Check(id == data.ids.front(), "tie ordering");
  model.Free();
  model.Free();
  Check(!model.subset_head && !model.subset_ids, "idempotent release");
  cudaFree(input); cudaFree(logits); cudaFree(tokens); cudaFree(ws);
  std::printf("PASS projection T=1,4,17 repeat mapping tie release max_error=%g\n",
              maximum);
}
