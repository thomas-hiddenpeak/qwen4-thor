// Three production HC mixes, no prefill/model execution. Link-time observers
// retain intermediate activations without replacing a numerical operation.
// Independent projection bounds do not certify the nonlinear stages.
#include "q4t/io/weight_loader.h"
#include "q4t/model/decoder_workspace.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/test.h"
#include "support/mtp_hc_dot_envelope.h"

#include <cublasLt.h>

#include <algorithm>
#include <array>
#include <cfenv>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <vector>

namespace {
namespace dot = q4t::test::hc_dot;
using U16 = uint16_t;
constexpr int kHc = 4, kHidden = 2560, kTrunk = 10240, kRank = 320;
constexpr size_t kGemmBytes = 32u * 1024u * 1024u;
constexpr size_t kArrayBytes = 426240;
constexpr char kModelDir[] =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
constexpr char kPrefix[] =
    "model.language_model.layers.0.attn_hyper_connection";
constexpr std::array<const char*, 5> kStages{"normed", "down_raw", "silu", "up",
                                             "mixed"};
constexpr std::array<int, 5> kWidths{kTrunk, kRank, kRank, kTrunk, kHidden};

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}
void Cuda(cudaError_t status, const std::string& operation) {
  Require(status == cudaSuccess, operation + ": " + cudaGetErrorString(status));
}
void Status(const q4t::Status& status, const std::string& operation) {
  Require(status.ok(), operation + ": " + status.message());
}
void Blas(cublasStatus_t status, const char* operation) {
  Require(status == CUBLAS_STATUS_SUCCESS,
          std::string(operation) + ": cuBLAS status " +
              std::to_string(static_cast<int>(status)));
}
void Finite(const std::vector<U16>& values, const std::string& label) {
  for (size_t i = 0; i < values.size(); ++i)
    if (!std::isfinite(dot::Value(values[i])))
      throw std::runtime_error(label + ": nonfinite at " + std::to_string(i));
}
template <typename T>
class Device {
 public:
  explicit Device(size_t count) {
    Cuda(cudaMalloc(reinterpret_cast<void**>(&pointer_), count * sizeof(T)),
         "HC diagnostic buffer");
  }
  ~Device() {
    if (cudaFree(pointer_) != cudaSuccess) std::abort();
  }
  Device(const Device&) = delete;
  Device& operator=(const Device&) = delete;
  T* get() const { return pointer_; }

 private:
  T* pointer_ = nullptr;
};
std::vector<U16> ReadDevice(const U16* device, size_t elements,
                            cudaStream_t stream) {
  std::vector<U16> output(elements);
  Cuda(cudaMemcpyAsync(output.data(), device, elements * sizeof(U16),
                       cudaMemcpyDeviceToHost, stream),
       "capture HC activation");
  Cuda(cudaStreamSynchronize(stream), "complete HC observation");
  return output;
}
std::vector<U16> ReadFile(const std::filesystem::path& path, size_t elements) {
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  Require(input.good() && input.tellg() == static_cast<std::streamoff>(
                                               elements * sizeof(U16)),
          "frozen HC fixture shape: " + path.string());
  input.seekg(0);
  std::vector<U16> output(elements);
  input.read(reinterpret_cast<char*>(output.data()),
             static_cast<std::streamsize>(elements * sizeof(U16)));
  Require(input.good(), "read frozen HC fixture: " + path.string());
  Finite(output, path.filename().string());
  return output;
}
class Evidence {
 public:
  explicit Evidence(const char* directory) : directory_(directory) {
    Require(std::filesystem::create_directory(directory_),
            "HC evidence directory must be new");
  }
  void Save(const std::string& name, const std::vector<U16>& values) {
    const size_t bytes = values.size() * sizeof(U16);
    Require(bytes <= kArrayBytes - bytes_, "frozen 426240-byte HC array limit");
    const auto path = directory_ / (name + ".bf16");
    Require(!std::filesystem::exists(path), "cannot overwrite HC evidence");
    std::ofstream output(path, std::ios::binary);
    output.exceptions(std::ios::failbit | std::ios::badbit);
    output.write(reinterpret_cast<const char*>(values.data()),
                 static_cast<std::streamsize>(bytes));
    output.close();
    bytes_ += bytes;
    ++files_;
  }
  void Complete() const {
    Require(files_ == 15 && bytes_ == kArrayBytes,
            "incomplete frozen HC capture inventory");
  }

 private:
  std::filesystem::path directory_;
  size_t bytes_ = 0;
  int files_ = 0;
};
struct Weights {
  q4t::model::HyperConnectionWeights device;
  ~Weights() { device.Free(); }
};
struct Capture {
  std::string name;
  int rows = 0;
  const q4t::model::HyperConnectionWeights* weights = nullptr;
  const std::vector<U16>* down_weight = nullptr;
  const std::vector<U16>* up_weight = nullptr;
  U16* normed = nullptr;
  U16* down = nullptr;
  U16* up = nullptr;
  void* workspace = nullptr;
  Evidence* evidence = nullptr;
  int projections = 0, gev_calls = 0, lt_calls = 0;
  std::array<std::vector<U16>, 5> arrays;
  std::array<std::vector<dot::Envelope>, 2> expected;

  void Observe(int stage, const U16* pointer, cudaStream_t stream) {
    Require(arrays[stage].empty(), "duplicate HC stage observation");
    arrays[stage] =
        ReadDevice(pointer, static_cast<size_t>(rows) * kWidths[stage], stream);
    Finite(arrays[stage], name + "." + kStages[stage]);
    evidence->Save(name + "." + kStages[stage], arrays[stage]);
  }
  void Before(const U16* input, const U16* weight, U16* output, int outer,
              int inner, cudaStream_t stream) {
    Require(projections < 2 && stream == nullptr,
            "unexpected extra HC projection/stream");
    const bool is_down = projections == 0;
    Require(outer == (is_down ? kRank : kTrunk) &&
                inner == (is_down ? kTrunk : kRank) &&
                input == (is_down ? normed : down) &&
                output == (is_down ? down : up) &&
                weight == (is_down ? weights->mix_down : weights->mix_up),
            "HC projection pointer/shape/order mismatch");
    const int stage = is_down ? 0 : 2;
    Observe(stage, input, stream);
    // The contract and every output interval are fixed before this projection
    // runs. Input is its actual BF16 activation, not an ideal previous stage.
    const auto& matrix = is_down ? *down_weight : *up_weight;
    expected[projections].resize(outer);
    for (int n = 0; n < outer; ++n)
      expected[projections][n] = dot::DotEnvelope(
          arrays[stage].data(), matrix.data() + static_cast<size_t>(n) * inner,
          inner);
  }
  void After(const U16* output, cudaStream_t stream) {
    Observe(projections == 0 ? 1 : 3, output, stream);
    ++projections;
  }
};
Capture* active = nullptr;
struct ActiveCapture {
  explicit ActiveCapture(Capture* capture) {
    Require(!active, "nested HC capture");
    active = capture;
  }
  ~ActiveCapture() { active = nullptr; }
};

template <typename T>
T Layout(cublasLtMatrixLayout_t layout,
         cublasLtMatrixLayoutAttribute_t attribute) {
  T value{};
  size_t written = 0;
  Blas(cublasLtMatrixLayoutGetAttribute(layout, attribute, &value,
                                        sizeof(value), &written),
       "read cuBLAS layout");
  Require(written == sizeof(value), "cuBLAS layout attribute width");
  return value;
}
template <typename T>
T Descriptor(cublasLtMatmulDesc_t descriptor,
             cublasLtMatmulDescAttributes_t attribute) {
  T value{};
  size_t written = 0;
  Blas(cublasLtMatmulDescGetAttribute(descriptor, attribute, &value,
                                      sizeof(value), &written),
       "read cuBLAS operation");
  Require(written == sizeof(value), "cuBLAS descriptor attribute width");
  return value;
}
void CheckLayout(cublasLtMatrixLayout_t layout, uint64_t rows, uint64_t columns,
                 int64_t leading) {
  Require(
      Layout<uint64_t>(layout, CUBLASLT_MATRIX_LAYOUT_ROWS) == rows &&
          Layout<uint64_t>(layout, CUBLASLT_MATRIX_LAYOUT_COLS) == columns &&
          Layout<int64_t>(layout, CUBLASLT_MATRIX_LAYOUT_LD) == leading &&
          Layout<cudaDataType_t>(layout, CUBLASLT_MATRIX_LAYOUT_TYPE) ==
              CUDA_R_16BF &&
          Layout<cublasLtOrder_t>(layout, CUBLASLT_MATRIX_LAYOUT_ORDER) ==
              CUBLASLT_ORDER_COL &&
          Layout<int32_t>(layout, CUBLASLT_MATRIX_LAYOUT_BATCH_COUNT) == 1,
      "HC cuBLAS matrix layout differs from production BF16 projection");
}
void CheckDescriptor(cublasLtMatmulDesc_t descriptor) {
  Require(Descriptor<cublasComputeType_t>(descriptor,
                                          CUBLASLT_MATMUL_DESC_COMPUTE_TYPE) ==
                  CUBLAS_COMPUTE_32F &&
              Descriptor<cudaDataType_t>(
                  descriptor, CUBLASLT_MATMUL_DESC_SCALE_TYPE) == CUDA_R_32F &&
              Descriptor<cublasLtPointerMode_t>(
                  descriptor, CUBLASLT_MATMUL_DESC_POINTER_MODE) ==
                  CUBLASLT_POINTER_MODE_HOST &&
              Descriptor<cublasOperation_t>(
                  descriptor, CUBLASLT_MATMUL_DESC_TRANSA) == CUBLAS_OP_T &&
              Descriptor<cublasOperation_t>(
                  descriptor, CUBLASLT_MATMUL_DESC_TRANSB) == CUBLAS_OP_N &&
              Descriptor<cublasLtEpilogue_t>(descriptor,
                                             CUBLASLT_MATMUL_DESC_EPILOGUE) ==
                  CUBLASLT_EPILOGUE_DEFAULT,
          "HC cuBLAS compute/scalar/transposition contract changed");
}

bool CheckProjection(const Capture& capture, int projection) {
  const int stage = projection == 0 ? 1 : 3;
  const int inner = projection == 0 ? kTrunk : kRank;
  const auto& actual = capture.arrays[stage];
  const auto& expected = capture.expected[projection];
  int outside = 0;
  for (size_t n = 0; n < expected.size(); ++n) {
    if (dot::InEnvelope(actual[n], expected[n])) continue;
    ++outside;
    if (outside <= 8) {
      const auto& e = expected[n];
      std::printf(
          "HC_PROJECTION_OUTSIDE path=%s stage=%s column=%zu "
          "actual=%.17g reference=%.17g lower=%.17g upper=%.17g "
          "sum_abs_upper=%.17g fp32_error=%.17g fp64_error=%.17g\n",
          capture.name.c_str(), kStages[stage], n, dot::Value(actual[n]),
          e.reference, dot::Value(e.lower), dot::Value(e.upper),
          e.sum_abs_upper, e.fp32_error_upper, e.fp64_error_upper);
    }
  }
  std::printf(
      "HC_PROJECTION path=%s stage=%s checked_rows=1 N=%zu K=%d "
      "gamma32=%.17g gamma64=%.17g outside=%d a_priori=1\n",
      capture.name.c_str(), kStages[stage], expected.size(), inner,
      dot::GammaUpper(inner + 2, 24), dot::GammaUpper(inner, 53), outside);
  return outside == 0;
}
size_t Differences(const std::vector<U16>& a, const std::vector<U16>& b,
                   size_t count, const std::string& label) {
  Require(a.size() >= count && b.size() >= count, "HC comparison shape");
  size_t unequal = 0, first = count;
  double maximum = 0;
  for (size_t i = 0; i < count; ++i) {
    if (a[i] == b[i]) continue;
    if (first == count) first = i;
    ++unequal;
    maximum = std::max(maximum, std::abs(dot::Value(a[i]) - dot::Value(b[i])));
    if (unequal <= 8)
      std::printf("HC_DIFFERENCE label=%s index=%zu a=%.17g b=%.17g\n",
                  label.c_str(), i, dot::Value(a[i]), dot::Value(b[i]));
  }
  std::printf(
      "HC_COMPARE label=%s elements=%zu unequal=%zu first=%zu "
      "max_abs=%.17g\n",
      label.c_str(), count, unequal, first, maximum);
  return unequal;
}
}  // namespace

#define Q4T_HC_GEV "_ZN3q4t5model7Bf16GevEPKtS2_PtiifP11CUstream_st"
bool RealGev(const uint16_t*, const uint16_t*, uint16_t*, int, int, float,
             cudaStream_t) asm("__real_" Q4T_HC_GEV);
bool WrapGev(const uint16_t*, const uint16_t*, uint16_t*, int, int, float,
             cudaStream_t) asm("__wrap_" Q4T_HC_GEV);
bool WrapGev(const uint16_t* input, const uint16_t* weight, uint16_t* output,
             int outer, int inner, float alpha, cudaStream_t stream) {
  if (!active)
    return RealGev(input, weight, output, outer, inner, alpha, stream);
  Require(active->rows == 1 && alpha == 1.0f, "unexpected HC GEMV dispatch");
  ++active->gev_calls;
  active->Before(input, weight, output, outer, inner, stream);
  const bool success =
      RealGev(input, weight, output, outer, inner, alpha, stream);
  Require(success, "production HC GEMV failed; no alternate-path resampling");
  active->After(output, stream);
  return success;
}
extern "C" cublasStatus_t CUBLASWINAPI __real_cublasLtMatmul(
    cublasLtHandle_t, cublasLtMatmulDesc_t, const void*, const void*,
    cublasLtMatrixLayout_t, const void*, cublasLtMatrixLayout_t, const void*,
    const void*, cublasLtMatrixLayout_t, void*, cublasLtMatrixLayout_t,
    const cublasLtMatmulAlgo_t*, void*, size_t, cudaStream_t);
extern "C" cublasStatus_t CUBLASWINAPI __wrap_cublasLtMatmul(
    cublasLtHandle_t handle, cublasLtMatmulDesc_t descriptor, const void* alpha,
    const void* weight, cublasLtMatrixLayout_t weight_layout, const void* input,
    cublasLtMatrixLayout_t input_layout, const void* beta, const void* prior,
    cublasLtMatrixLayout_t prior_layout, void* output,
    cublasLtMatrixLayout_t output_layout, const cublasLtMatmulAlgo_t* algorithm,
    void* workspace, size_t workspace_bytes, cudaStream_t stream) {
  if (active) {
    Require(active->rows == 4 && alpha && beta &&
                workspace == active->workspace &&
                workspace_bytes == kGemmBytes && prior == output && algorithm,
            "unexpected HC cuBLAS workspace/dispatch");
    CheckDescriptor(descriptor);
    Require(*static_cast<const float*>(alpha) == 1.0f &&
                *static_cast<const float*>(beta) == 0.0f,
            "HC projection alpha/beta contract");
    const int outer = active->projections == 0 ? kRank : kTrunk;
    const int inner = active->projections == 0 ? kTrunk : kRank;
    CheckLayout(weight_layout, inner, outer, inner);
    CheckLayout(input_layout, inner, active->rows, inner);
    CheckLayout(prior_layout, outer, active->rows, outer);
    CheckLayout(output_layout, outer, active->rows, outer);
    ++active->lt_calls;
    active->Before(static_cast<const U16*>(input),
                   static_cast<const U16*>(weight), static_cast<U16*>(output),
                   outer, inner, stream);
  }
  const auto status = __real_cublasLtMatmul(
      handle, descriptor, alpha, weight, weight_layout, input, input_layout,
      beta, prior, prior_layout, output, output_layout, algorithm, workspace,
      workspace_bytes, stream);
  if (active && status == CUBLAS_STATUS_SUCCESS)
    active->After(static_cast<U16*>(output), stream);
  return status;
}

Q4T_TEST(mtp_hc_first_fork) {
  static_assert(std::endian::native == std::endian::little);
  Require(std::numeric_limits<float>::is_iec559 &&
              std::numeric_limits<double>::is_iec559 &&
              std::numeric_limits<float>::digits == 24 &&
              std::numeric_limits<double>::digits == 53 &&
              std::fegetround() == FE_TONEAREST,
          "requires IEEE nearest rounding");
  Require(dot::RoundBf16(1.0 + std::ldexp(1.0, -8)) == 0x3f80 &&
              dot::RoundBf16(1.0 + 3 * std::ldexp(1.0, -8)) == 0x3f82 &&
              dot::RoundBf16(std::ldexp(1.0, -134)) == 0 &&
              dot::RoundBf16(-std::ldexp(1.0, -134)) == 0x8000,
          "BF16 endpoint RNE self-check");
  const char* source = std::getenv("Q4T_MTP_HC_CAPTURE");
  const char* directory = std::getenv("Q4T_MTP_HC_DIR");
  Require(
      source && *source && directory && *directory,
      "set frozen triangle capture directory and a new HC output directory");
  for (const char* name : {"Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  const std::filesystem::path capture_dir(source);
  const std::array<int, 3> rows{1, 4, 4};
  const std::array<const char*, 3> names{"m1", "m4a", "m4b"};
  const std::array<const char*, 3> inputs{"layer_m36.trunk_in.bin",
                                          "layer_m52.trunk_in.bin",
                                          "layer_m60.trunk_in.bin"};
  const std::array<const char*, 3> outputs{"lin_m27.x.bin", "lin_m39.x.bin",
                                           "lin_m45.x.bin"};
  std::array<std::vector<U16>, 3> frozen_inputs, frozen_outputs;
  for (size_t i = 0; i < rows.size(); ++i) {
    frozen_inputs[i] = ReadFile(capture_dir / inputs[i], rows[i] * kTrunk);
    frozen_outputs[i] = ReadFile(capture_dir / outputs[i], rows[i] * kHidden);
  }
  Require(std::equal(frozen_inputs[0].begin(), frozen_inputs[0].end(),
                     frozen_inputs[1].begin()) &&
              std::equal(frozen_inputs[0].begin(), frozen_inputs[0].end(),
                         frozen_inputs[2].begin()),
          "three frozen HC first-row inputs differ");
  Require(!std::equal(frozen_inputs[1].begin() + kTrunk, frozen_inputs[1].end(),
                      frozen_inputs[2].begin() + kTrunk),
          "frozen M4 suffix perturbation disappeared");
  std::printf(
      "HC_SCOPE byte_order=little encoding=BF16 rows=1,4,4 "
      "widths=10240,320,320,10240,2560 hc_count=4 hidden_size=2560 "
      "lowrank=320 eps_f32_bits=%08x cudart_header=%d "
      "FP8_PROJ=unset FP8_HC=unset FP8_ALL=unset "
      "projection_first_row_only=1 nonlinear_contract=NOT_EVALUATED\n",
      static_cast<unsigned>(std::bit_cast<uint32_t>(1e-6f)), CUDART_VERSION);
  std::fflush(stdout);
  Evidence evidence(directory);
  Cuda(cudaSetDevice(0), "CUDA required, no SKIP");
  q4t::io::WeightIndex* raw_index = nullptr;
  Status(
      q4t::io::WeightIndex::Open(
          std::string(kModelDir) + "/model.safetensors.index.json", &raw_index),
      "open read-only weight metadata");
  std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
  q4t::io::WeightLoader* raw_loader = nullptr;
  Status(q4t::io::WeightLoader::Create(kModelDir, *index, 1, &raw_loader),
         "create read-only HC loader");
  std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
  const auto check_tensor = [&](const char* suffix,
                                const std::vector<int64_t>& dimensions,
                                bool allow_flattened) {
    const std::string name = std::string(kPrefix) + suffix;
    const auto* info = loader->FindTensor(name);
    size_t elements = 1;
    for (int64_t dimension : dimensions) elements *= dimension;
    Require(info && info->dtype == q4t::io::Dtype::kBF16 &&
                info->byte_size() == elements * sizeof(U16) &&
                (allow_flattened || info->shape == dimensions),
            "HC weight metadata shape/type: " + name);
  };
  check_tensor(".hc_norm.weight", {kHc, kHidden}, true);
  check_tensor(".input_mix_weight_down.weight", {kRank, kTrunk}, false);
  check_tensor(".input_mix_weight_up.weight", {kTrunk, kRank}, false);
  check_tensor(".block_inject_weight.weight", {kHc, kTrunk}, false);
  Weights weights;
  Status(q4t::model::LoadHyperConnection(*loader, kPrefix, kHc, kHidden, kRank,
                                         1e-6f, true, &weights.device, nullptr),
         "load only layer-0 attention HC");
  Cuda(cudaStreamSynchronize(nullptr), "HC load completion");
  // Read weights solely for arithmetic. Never write, hash or include these
  // payload bytes in evidence. No full model/PLE/draft weights are loaded.
  const auto down_weight = ReadDevice(
      weights.device.mix_down, static_cast<size_t>(kRank) * kTrunk, nullptr);
  const auto up_weight = ReadDevice(
      weights.device.mix_up, static_cast<size_t>(kTrunk) * kRank, nullptr);
  std::array<q4t::model::DecoderWorkspaceLayout, 3> layouts;
  size_t workspace_bytes = 0;
  for (size_t i = 0; i < rows.size(); ++i) {
    layouts[i] = q4t::model::MakeDecoderWorkspaceLayout(
        rows[i], false, false, kHc, kHidden, 512, 640, 640, 10, nullptr, kRank);
    workspace_bytes = std::max(workspace_bytes, layouts[i].total_bytes);
  }
  Device<uint8_t> workspace(workspace_bytes);
  Device<U16> device_input(4 * kTrunk);
  std::array<Capture, 3> captures;
  bool projections_pass = true, historical_pass = true, suffix_pass = true;
  for (size_t i = 0; i < rows.size(); ++i) {
    const auto& layout = layouts[i];
    auto* base = workspace.get();
    auto& capture = captures[i];
    capture.name = names[i];
    capture.rows = rows[i];
    capture.weights = &weights.device;
    capture.down_weight = &down_weight;
    capture.up_weight = &up_weight;
    capture.normed = reinterpret_cast<U16*>(base + layout.normed);
    capture.down = reinterpret_cast<U16*>(base + layout.hc_down);
    capture.up = reinterpret_cast<U16*>(base + layout.hc_up);
    capture.workspace = base + layout.hc_gemm;
    capture.evidence = &evidence;
    auto* mixed = reinterpret_cast<U16*>(base + layout.mixed);
    Require(((reinterpret_cast<uintptr_t>(capture.normed) |
              reinterpret_cast<uintptr_t>(capture.up) |
              reinterpret_cast<uintptr_t>(mixed)) &
             3u) == 0,
            "frozen paired MixGate alignment");
    const q4t::model::HyperConnectionMixScratch scratch{
        capture.down, capture.up, layout.hc_down_bytes, layout.hc_up_bytes};
    Cuda(cudaMemcpy(device_input.get(), frozen_inputs[i].data(),
                    frozen_inputs[i].size() * sizeof(U16),
                    cudaMemcpyHostToDevice),
         "copy frozen HC input");
    std::printf(
        "HC_CALL path=%s T=%d source=%s old_output=%s "
        "workspace=%zu hc_workspace=%zu normed_offset=%zu "
        "down_offset=%zu up_offset=%zu mixed_offset=%zu\n",
        names[i], rows[i], inputs[i], outputs[i], layout.total_bytes,
        kGemmBytes, layout.normed, layout.hc_down, layout.hc_up, layout.mixed);
    {
      ActiveCapture observe(&capture);
      Status(q4t::model::HyperConnectionMix(
                 weights.device, device_input.get(), mixed, capture.normed,
                 rows[i], capture.workspace, kGemmBytes, nullptr, &scratch),
             "one production HC mix");
    }
    Cuda(cudaStreamSynchronize(nullptr), "complete production HC mix");
    capture.Observe(4, mixed, nullptr);
    Require(capture.projections == 2 &&
                capture.gev_calls == (rows[i] == 1 ? 2 : 0) &&
                capture.lt_calls == (rows[i] == 1 ? 0 : 2),
            "production HC dispatch was not fully intercepted");
    projections_pass &= CheckProjection(capture, 0);
    projections_pass &= CheckProjection(capture, 1);
    historical_pass &= Differences(capture.arrays[4], frozen_outputs[i],
                                   frozen_outputs[i].size(),
                                   capture.name + ".historical_mixed") == 0;
  }
  for (size_t stage = 0; stage < kStages.size(); ++stage) {
    Differences(captures[0].arrays[stage], captures[1].arrays[stage],
                kWidths[stage], std::string(kStages[stage]) + ".M1_M4A");
    suffix_pass &= Differences(captures[1].arrays[stage],
                               captures[2].arrays[stage], kWidths[stage],
                               std::string(kStages[stage]) + ".M4A_M4B") == 0;
  }
  evidence.Complete();
  std::printf(
      "HC_FIRST_FORK_SUMMARY hc_calls=3 prefill=0 full_model=0 "
      "captured_arrays=15 captured_bytes=426240 first_row_projection=%s "
      "historical_mixed_exact=%s suffix_first_row_exact=%s "
      "nonlinear_contract=NOT_EVALUATED full_t4_admission=0\n",
      projections_pass ? "PASS" : "FAIL", historical_pass ? "PASS" : "FAIL",
      suffix_pass ? "PASS" : "FAIL");
  return projections_pass && historical_pass && suffix_pass;
}
