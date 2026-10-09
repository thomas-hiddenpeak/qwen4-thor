// A bounded layer-0 projection diagnostic, not an MTP/model acceptance test.
// Freeze the observed BF16 x row and the checkpoint's in_proj_qkv matrix.
// Compare Bf16Gemm M=1/M=4 against an analytic FP32 accumulation envelope;
// cross-shape BF16 bit equality is only reported. Changing M=4 suffix rows
// must leave the identical first row bit-exact for this stateless projection.
#include "q4t/io/weight_loader.h"
#include "q4t/model/linear.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <bit>
#include <cfenv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr int kN = 10240;
constexpr int kK = 2560;
// Matches DecoderLayerForward's linear AttnWs/kGemmWs, for both row counts.
constexpr size_t kWorkspaceBytes = 32 * 1024 * 1024;
constexpr char kModelDir[] =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
constexpr char kWeight[] =
    "model.language_model.layers.0.linear_attn.in_proj_qkv.weight";
constexpr char kInput[] =
    "/home/rm01/models/dev/qwen4-thor/.q4t-work/"
    "mtp-admission-20261007/linear-capture-01/lin_m3.x.bin";

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void RequireStatus(const q4t::Status& status, const char* operation) {
  Require(status.ok(), std::string(operation) + ": " + status.message());
}

void RequireCuda(cudaError_t status, const char* operation) {
  Require(status == cudaSuccess,
          std::string(operation) + ": " + cudaGetErrorString(status));
}

template <typename T>
class DeviceBuffer {
 public:
  explicit DeviceBuffer(size_t count) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_), count * sizeof(T)),
                "projection allocation");
  }
  ~DeviceBuffer() {
    if (cudaFree(data_) != cudaSuccess) std::abort();
  }
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  T* data() const { return data_; }

 private:
  T* data_ = nullptr;
};

double Value(uint16_t bits) {
  return std::bit_cast<float>(static_cast<uint32_t>(bits) << 16);
}

bool FiniteNormalOrZero(uint16_t bits) {
  const uint16_t magnitude = bits & 0x7fff;
  return magnitude == 0 || (magnitude >= 0x0080 && magnitude < 0x7f80);
}

// Direct binary64 -> BF16 round-to-nearest, ties-to-even. Do not first cast
// to float: double rounding would make interval endpoints unsound at ties.
// All finite BF16 values and their midpoints are exactly representable in
// binary64, including subnormals and the finite/infinity transition.
uint16_t RoundBf16(double value) {
  Require(std::isfinite(value), "non-finite BF16 rounding endpoint");
  const uint16_t sign = std::signbit(value) ? 0x8000 : 0;
  const double magnitude = std::abs(value);
  constexpr uint16_t kMax = 0x7f7f;
  if (magnitude > Value(kMax)) {
    const double threshold = std::ldexp(1.0, 128) - std::ldexp(1.0, 119);
    return sign | (magnitude >= threshold ? 0x7f80 : kMax);
  }
  int low = 0, high = kMax;
  while (low < high) {
    const int middle = low + (high - low) / 2;
    if (Value(static_cast<uint16_t>(middle)) < magnitude) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  if (low == 0 || Value(static_cast<uint16_t>(low)) == magnitude)
    return sign | static_cast<uint16_t>(low);
  const double midpoint =
      (Value(static_cast<uint16_t>(low - 1)) +
       Value(static_cast<uint16_t>(low))) * 0.5;
  if (magnitude < midpoint || (magnitude == midpoint && (low & 1)))
    --low;
  return sign | static_cast<uint16_t>(low);
}

double Up(double value) {
  return std::nextafter(value, std::numeric_limits<double>::infinity());
}

double GammaUpper(int operations, int precision) {
  const double nu = operations * std::ldexp(1.0, -precision);
  Require(nu < 1.0, "invalid analytic gamma domain");
  // For these operation counts, nu and 1-nu are exact in binary64.
  return Up(nu / (1.0 - nu));
}

struct Envelope {
  double reference = 0;
  double sum_abs_upper = 0;
  double fp32_error_upper = 0;
  double fp64_error_upper = 0;
  uint16_t lower = 0;
  uint16_t upper = 0;
};

Envelope DotEnvelope(const uint16_t* x, const uint16_t* weight) {
  Envelope result;
  for (int k = 0; k < kK; ++k) {
    Require(FiniteNormalOrZero(x[k]) && FiniteNormalOrZero(weight[k]),
            "unsupported non-finite/subnormal BF16 operand");
    // A BF16 product has <=16 significant bits, so binary64 is exact here.
    const double product = Value(x[k]) * Value(weight[k]);
    if (product == 0) continue;
    // Conservatively require every product's bit grid to be >=2^-126.
    // Therefore any FP32 partial sum is zero or normal, for any reduction
    // order; no relative-error assumption is made about flushed subnormals.
    Require(std::isfinite(product) && std::ilogb(std::abs(product)) >= -111,
            "unsupported product grid: possible FP32 underflow");
    result.reference += product;
    result.sum_abs_upper = Up(result.sum_abs_upper + std::abs(product));
  }
  if (result.sum_abs_upper == 0) return result;  // Accept either signed zero.

  // FP32 accumulation: gamma_(K+2) * sum |x[k]*w[k]|, u=2^-24.
  // alpha=1 and beta=0, with BF16 output rounded once after accumulation.
  // This is independent of observed GPU differences, and deliberately
  // allows different FP32 reduction orders. It is not a tight error claim.
  result.fp32_error_upper =
      Up(GammaUpper(kK + 2, 24) * result.sum_abs_upper);
  // The binary64 reference is not treated as exact. Enclose its own sum
  // error using gamma_K, u=2^-53, with the same outward-rounded sum_abs.
  result.fp64_error_upper =
      Up(GammaUpper(kK, 53) * result.sum_abs_upper);
  const double error = Up(result.fp32_error_upper + result.fp64_error_upper);
  Require(std::isfinite(result.reference) &&
              result.sum_abs_upper + error <
                  static_cast<double>(std::numeric_limits<float>::max()),
          "unsupported sum: possible FP32 overflow");
  const double lower = std::nextafter(
      result.reference - error, -std::numeric_limits<double>::infinity());
  const double upper = Up(result.reference + error);
  result.lower = RoundBf16(lower);
  result.upper = RoundBf16(upper);
  Require(std::isfinite(Value(result.lower)) &&
              std::isfinite(Value(result.upper)),
          "unsupported envelope: possible BF16 overflow");
  return result;
}

bool InEnvelope(uint16_t actual, const Envelope& expected) {
  const double value = Value(actual);
  return std::isfinite(value) && value >= Value(expected.lower) &&
         value <= Value(expected.upper);
}

std::vector<uint16_t> Project(const std::vector<uint16_t>& input, int rows,
                              uint16_t* d_input, const uint16_t* d_weight,
                              uint16_t* d_output, void* workspace) {
  Require(input.size() == static_cast<size_t>(rows) * kK,
          "wrong projection input shape");
  RequireCuda(cudaMemcpy(d_input, input.data(), input.size() * sizeof(uint16_t),
                         cudaMemcpyHostToDevice),
              "projection input copy");
  const auto status = q4t::model::Bf16Gemm(
      d_input, d_weight, d_output, rows, kN, kK, 1.0f, 0.0f, workspace,
      kWorkspaceBytes, nullptr);
  Require(status.status == CUBLAS_STATUS_SUCCESS && status.has_algo,
          "Bf16Gemm failed");
  RequireCuda(cudaStreamSynchronize(nullptr), "projection completion");
  std::vector<uint16_t> output(kN);
  RequireCuda(cudaMemcpy(output.data(), d_output, kN * sizeof(uint16_t),
                         cudaMemcpyDeviceToHost),
              "projection first-row read");
  return output;
}

}  // namespace

Q4T_TEST(mtp_projection_path) {
  Require(std::numeric_limits<float>::is_iec559 &&
              std::numeric_limits<double>::is_iec559 &&
              std::numeric_limits<float>::digits == 24 &&
              std::numeric_limits<double>::digits == 53 &&
              std::fegetround() == FE_TONEAREST,
          "requires IEEE binary32/binary64 with round-to-nearest");
  Require(RoundBf16(1.0 + std::ldexp(1.0, -8)) == 0x3f80 &&
              RoundBf16(1.0 + 3 * std::ldexp(1.0, -8)) == 0x3f82 &&
              RoundBf16(std::ldexp(1.0, -134)) == 0 &&
              RoundBf16(-std::ldexp(1.0, -134)) == 0x8000,
          "BF16 endpoint tie handling failed");
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  const char* input_path = std::getenv("Q4T_MTP_PROJECTION_X");
  if (!input_path) input_path = kInput;
  std::ifstream capture(input_path, std::ios::binary | std::ios::ate);
  Require(capture.good() && capture.tellg() == kK * sizeof(uint16_t),
          "frozen projection x must exist and contain exactly 2560 BF16s");
  capture.seekg(0);
  std::vector<uint16_t> input(kK);
  capture.read(reinterpret_cast<char*>(input.data()),
               static_cast<std::streamsize>(input.size() * sizeof(uint16_t)));
  Require(capture.good(), "failed to read frozen projection x");

  q4t::io::WeightIndex* raw_index = nullptr;
  RequireStatus(q4t::io::WeightIndex::Open(
                    std::string(kModelDir) + "/model.safetensors.index.json",
                    &raw_index),
                "open weight index");
  std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
  q4t::io::WeightLoader* raw_loader = nullptr;
  RequireStatus(
      q4t::io::WeightLoader::Create(kModelDir, *index, 1, &raw_loader),
      "create weight loader");
  std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
  const auto* info = loader->FindTensor(kWeight);
  Require(info && info->dtype == q4t::io::Dtype::kBF16 &&
              info->shape == std::vector<int64_t>{kN, kK} &&
              info->byte_size() == static_cast<size_t>(kN) * kK * 2,
          "unexpected frozen projection weight metadata");
  std::vector<uint16_t> weight(static_cast<size_t>(kN) * kK);
  RequireStatus(loader->ReadTensor(kWeight, weight.data()),
                "read only the layer-0 projection matrix");

  // Establish every row's analytic envelope before observing any GPU output.
  std::vector<Envelope> expected(kN);
  for (int n = 0; n < kN; ++n)
    expected[n] = DotEnvelope(input.data(), weight.data() + n * kK);
  std::printf(
      "MTP_PROJECTION_INPUT path=%s weight=%s N=%d K=%d workspace=%zu "
      "fp32_gamma=%.17g fp64_gamma=%.17g\n",
      input_path, kWeight, kN, kK, kWorkspaceBytes, GammaUpper(kK + 2, 24),
      GammaUpper(kK, 53));

  DeviceBuffer<uint16_t> d_weight(weight.size()), d_input(4 * kK),
      d_output(4 * kN);
  DeviceBuffer<uint8_t> workspace(kWorkspaceBytes);
  RequireCuda(cudaMemcpy(d_weight.data(), weight.data(), weight.size() * 2,
                         cudaMemcpyHostToDevice),
              "projection weight copy");
  const auto single = Project(input, 1, d_input.data(), d_weight.data(),
                              d_output.data(), workspace.data());
  std::vector<uint16_t> packed(4 * kK);
  std::copy(input.begin(), input.end(), packed.begin());
  // Frozen, finite suffixes derived only by permutation and sign changes.
  for (int row = 1; row < 4; ++row)
    for (int k = 0; k < kK; ++k)
      packed[row * kK + k] = input[(k + 17 * row) % kK] ^
                            ((row & 1) ? 0x8000 : 0);
  const auto batch = Project(packed, 4, d_input.data(), d_weight.data(),
                             d_output.data(), workspace.data());
  for (int row = 1; row < 4; ++row)
    for (int k = 0; k < kK; ++k)
      packed[row * kK + k] = input[(kK - 1 - k + 29 * row) % kK] ^
                            ((row & 1) ? 0 : 0x8000);
  const auto changed = Project(packed, 4, d_input.data(), d_weight.data(),
                               d_output.data(), workspace.data());

  int single_bad = 0, batch_bad = 0, changed_bad = 0;
  int cross_shape_unequal = 0, suffix_unequal = 0;
  double max_cross_shape_abs = 0;
  for (int n = 0; n < kN; ++n) {
    single_bad += !InEnvelope(single[n], expected[n]);
    batch_bad += !InEnvelope(batch[n], expected[n]);
    changed_bad += !InEnvelope(changed[n], expected[n]);
    suffix_unequal += batch[n] != changed[n];
    if (single[n] != batch[n]) {
      ++cross_shape_unequal;
      max_cross_shape_abs =
          std::max(max_cross_shape_abs, std::abs(Value(single[n]) -
                                                Value(batch[n])));
      if (cross_shape_unequal <= 8)
        std::printf("  cross_shape n=%d M1=%.9g M4=%.9g\n", n,
                    Value(single[n]), Value(batch[n]));
    }
    if ((!InEnvelope(single[n], expected[n]) ||
         !InEnvelope(batch[n], expected[n]) ||
         !InEnvelope(changed[n], expected[n])) &&
        single_bad + batch_bad + changed_bad <= 24) {
      const auto& e = expected[n];
      std::printf(
          "  envelope_failure n=%d ref=%.17g sum_abs_upper=%.17g "
          "fp32_error=%.17g fp64_error=%.17g lower=%.9g upper=%.9g "
          "M1=%.9g M4=%.9g changed=%.9g\n",
          n, e.reference, e.sum_abs_upper, e.fp32_error_upper,
          e.fp64_error_upper, Value(e.lower), Value(e.upper), Value(single[n]),
          Value(batch[n]), Value(changed[n]));
    }
  }
  std::printf(
      "MTP_PROJECTION rows=%d M1_outside=%d M4_outside=%d "
      "changed_outside=%d suffix_unequal=%d cross_shape_unequal=%d "
      "max_cross_shape_abs=%.9g local_projection_only=1\n",
      kN, single_bad, batch_bad, changed_bad, suffix_unequal,
      cross_shape_unequal, max_cross_shape_abs);
  return single_bad == 0 && batch_bad == 0 && changed_bad == 0 &&
         suffix_unequal == 0;
}
