// Frozen same-input projection replay; no model load or production mutation.
#define main Q4tOriginalCaptureMain
#include "../moe_replay_real/capture.cpp"
#undef main
#include "q4t/model/linear.h"

namespace q4t::diagnostic::projection {
using namespace real_capture;
constexpr int kM = 4, kN = 48, kK = 2560;
template <typename T>
std::vector<T> Load(const fs::path& path, size_t count) {
  Check(fs::file_size(path) == count * sizeof(T), "fixture byte count");
  std::vector<T> data(count);
  std::ifstream input(path, std::ios::binary);
  input.read(reinterpret_cast<char*>(data.data()), count * sizeof(T));
  Check(bool(input), "fixture read");
  return data;
}
void Blas(cublasStatus_t status) {
  Check(status == CUBLAS_STATUS_SUCCESS,
        "cuBLAS status " + std::to_string(status));
}
void Algorithm(std::ostream& out, const cublasLtMatmulAlgo_t& algorithm) {
  out << '[';
  bool first = true;
  for (auto attribute :
       {CUBLASLT_ALGO_CONFIG_ID, CUBLASLT_ALGO_CONFIG_TILE_ID,
        CUBLASLT_ALGO_CONFIG_SPLITK_NUM, CUBLASLT_ALGO_CONFIG_REDUCTION_SCHEME,
        CUBLASLT_ALGO_CONFIG_CTA_SWIZZLING, CUBLASLT_ALGO_CONFIG_STAGES_ID}) {
    uint32_t value = 0;
    size_t written = 0;
    Blas(cublasLtMatmulAlgoConfigGetAttribute(&algorithm, attribute, &value,
                                              sizeof(value), &written));
    Check(written == sizeof(value), "algorithm attribute size");
    if (!first) out << ',';
    first = false;
    out << value;
  }
  out << ']';
}
int Run(int argc, char** argv) {
  Check(argc == 3, "usage: projection FIXTURE NEW_OUTPUT");
  const fs::path input = fs::canonical(argv[1]);
  const fs::path output = fs::weakly_canonical(argv[2]);
  const auto relative = output.lexically_relative(fs::current_path());
  Check(!relative.empty() &&
            (*relative.begin() == ".q4t-work" || *relative.begin() == "build"),
        "output location");
  Check(!fs::exists(output), "output exists");
  fs::create_directories(output);
  auto x = Load<uint16_t>(input / "x", kM * kK);
  auto w = Load<uint16_t>(input / "weight", kN * kK);
  auto t4 = Load<uint16_t>(input / "t4", kM * kN);
  auto t1 = Load<uint16_t>(input / "t1", kM * kN);
  Resources resources;
  auto stream = resources.stream;
  const auto upload = [&](const auto& data) {
    auto* device = static_cast<uint16_t*>(resources.Alloc(data.size() * 2));
    Cuda(cudaMemcpyAsync(device, data.data(), data.size() * 2,
                         cudaMemcpyHostToDevice, stream));
    Cuda(cudaStreamSynchronize(stream));
    return device;
  };
  auto* dx = upload(x);
  auto* dw = upload(w);
  auto* dy = static_cast<uint16_t*>(resources.Alloc(kM * kN * 2));
  auto* df = static_cast<float*>(resources.Alloc(kM * kN * 4));
  void* workspace = resources.Alloc(kGemmBytes);
  for (int row = 0; row < kM; ++row) {
    Check(Bf16GevFp32(dx + row * kK, dw, dy + row * kN, df + row * kN, kN, kK,
                      1, stream),
          "GEMV FP32 replay");
  }
  std::vector<uint16_t> actual;
  std::vector<float> fp32;
  Read(actual, dy, kM * kN, stream);
  Read(fp32, df, kM * kN, stream);
  Check(Bits(actual, t1), "GEMV replay differs from capture");
  Save(output / "gemv.bf16", actual);
  Save(output / "gemv.f32", fp32);
  auto result =
      Bf16Gemm(dx, dw, dy, kM, kN, kK, 1, 0, workspace, kGemmBytes, stream);
  Check(result.has_algo, "missing production plan");
  Blas(result.status);
  Read(actual, dy, kM * kN, stream);
  Check(Bits(actual, t4), "T4 replay differs from capture");
  Save(output / "t4.bf16", actual);
  auto* plan = GetBf16Plan(kM, kN, kK, kGemmBytes);
  Check(plan && plan->has_algo, "production cached plan");
  std::ofstream metadata(output / "result.json");
  metadata
      << "{\"production_replay\":\"BITEXACT_PASS\",\"algorithm_fields\":["
         "\"id\",\"tile\",\"split_k\",\"reduction\",\"swizzle\",\"stages\"],"
         "\"bf16_algorithm\":";
  Algorithm(metadata, plan->algo);
  // Output dtype can change the algorithm. Record it explicitly; never claim
  // a float-output invocation exposes the BF16 kernel's internal accumulator.
  cublasLtMatrixLayout_t float_layout = nullptr;
  Blas(cublasLtMatrixLayoutCreate(&float_layout, CUDA_R_32F, kN, kM, kN));
  cublasLtMatmulHeuristicResult_t compatibility{};
  auto compatible = cublasLtMatmulAlgoCheck(
      quant::GlobalLtHandle(), plan->op, plan->la, plan->lb, float_layout,
      float_layout, &plan->algo, &compatibility);
  metadata << ",\"same_algorithm_float_check\":" << int(compatible)
           << ",\"same_algorithm_float_state\":" << int(compatibility.state);
  cublasLtMatmulPreference_t preference = nullptr;
  Blas(cublasLtMatmulPreferenceCreate(&preference));
  const size_t bytes = kGemmBytes;
  const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
  Blas(cublasLtMatmulPreferenceSetAttribute(
      preference, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &bytes,
      sizeof(bytes)));
  Blas(cublasLtMatmulPreferenceSetAttribute(
      preference, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction,
      sizeof(reduction)));
  cublasLtMatmulHeuristicResult_t heuristic{};
  int count = 0;
  Blas(cublasLtMatmulAlgoGetHeuristic(
      quant::GlobalLtHandle(), plan->op, plan->la, plan->lb, float_layout,
      float_layout, preference, 1, &heuristic, &count));
  Check(count == 1, "float output heuristic missing");
  const float alpha = 1, beta = 0;
  Blas(cublasLtMatmul(quant::GlobalLtHandle(), plan->op, &alpha, dw, plan->la,
                      dx, plan->lb, &beta, df, float_layout, df, float_layout,
                      &heuristic.algo, workspace, bytes, stream));
  Read(fp32, df, kM * kN, stream);
  Save(output / "float_output.f32", fp32);
  metadata << ",\"float_output_algorithm\":";
  Algorithm(metadata, heuristic.algo);
  metadata << ",\"status\":\"PASS\"}\n";
  metadata.close();
  Check(bool(metadata), "result write");
  Blas(cublasLtMatmulPreferenceDestroy(preference));
  Blas(cublasLtMatrixLayoutDestroy(float_layout));
  std::cout
      << "PASS projection: 4 GEMV, 1 BF16 batch, 1 float-output diagnostic\n";
  return 0;
}
}  // namespace q4t::diagnostic::projection
int main(int argc, char** argv) {
  try {
    return q4t::diagnostic::projection::Run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
