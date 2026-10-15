// Bounded MoE arithmetic diagnostic for the MTP continuation stage.
// Four public calls separate grouped M=1, generic M_e=1, generic M_e=4,
// and a same-shape suffix perturbation. Cross-shape differences are reported,
// not accepted by a newly invented tolerance. Suffix independence and the
// independently reconstructed slot-order combine are exact contracts.
#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr int kExperts = 512;
constexpr int kHidden = 2560;
constexpr int kIntermediate = 640;
constexpr int kSlots = 10;
constexpr size_t kGemmBytes = 32u * 1024u * 1024u;
constexpr std::array<int32_t, kSlots> kFirstIds = {
    37, 3, 91, 7, 211, 19, 401, 23, 127, 53};
// Binary fractions make each BF16 * router-weight product exact in FP32.
constexpr std::array<float, kSlots> kRouterWeights = {
    0.25f, 0.125f, 0.125f, 0.125f, 0.0625f,
    0.0625f, 0.0625f, 0.0625f, 0.0625f, 0.0625f};

void Require(const q4t::Status& status, const char* operation) {
  if (!status.ok())
    throw std::runtime_error(std::string(operation) + ": " + status.message());
}

void RequireCuda(cudaError_t error, const char* operation) {
  if (error != cudaSuccess)
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(error));
}

void FreeDevice(void* pointer) {
  if (pointer && cudaFree(pointer) != cudaSuccess) std::abort();
}

template <typename T>
class DeviceBuffer {
 public:
  explicit DeviceBuffer(size_t count) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_), count * sizeof(T)),
                "allocate MoE diagnostic buffer");
  }
  ~DeviceBuffer() { FreeDevice(data_); }
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  T* data() const { return data_; }

 private:
  T* data_ = nullptr;
};

struct WeightsOwner {
  q4t::quant::MoEWeightLayout value;
  ~WeightsOwner() {
    if (cudaDeviceSynchronize() != cudaSuccess) std::abort();
    FreeDevice(value.gu_packed);
    FreeDevice(value.gu_sf);
    FreeDevice(value.dn_packed);
    FreeDevice(value.dn_sf);
    FreeDevice(value.gu_w_scale2);
    FreeDevice(value.gu_input_scale);
    FreeDevice(value.dn_w_scale2);
    FreeDevice(value.dn_input_scale);
  }
};

float Value(uint16_t bits) {
  return std::bit_cast<float>(static_cast<uint32_t>(bits) << 16);
}
float Value(float value) { return value; }

uint16_t Bf16(float value) {
  uint32_t bits = std::bit_cast<uint32_t>(value);
  bits += 0x7fffu + ((bits >> 16) & 1u);
  return static_cast<uint16_t>(bits >> 16);
}

template <typename T>
std::vector<T> Read(const T* device, size_t count) {
  std::vector<T> values(count);
  RequireCuda(cudaMemcpy(values.data(), device, count * sizeof(T),
                         cudaMemcpyDeviceToHost),
              "read MoE diagnostic observation");
  return values;
}

template <typename T>
void Save(const std::filesystem::path& dir, const std::string& name,
          const std::vector<T>& values) {
  std::ofstream file(dir / name, std::ios::binary | std::ios::out);
  file.exceptions(std::ios::failbit | std::ios::badbit);
  file.write(reinterpret_cast<const char*>(values.data()),
             static_cast<std::streamsize>(values.size() * sizeof(T)));
  file.close();
}

template <typename T>
bool Report(const char* label, const std::vector<T>& actual,
            const std::vector<T>& reference) {
  if (actual.size() != reference.size())
    throw std::runtime_error("MoE diagnostic comparison size mismatch");
  size_t unequal = 0;
  double max_abs = 0.0, squared = 0.0, ref_squared = 0.0;
  bool finite = true;
  for (size_t i = 0; i < actual.size(); ++i) {
    const double a = Value(actual[i]), b = Value(reference[i]);
    finite &= std::isfinite(a) && std::isfinite(b);
    if (std::memcmp(&actual[i], &reference[i], sizeof(T)) != 0) {
      if (unequal < 4)
        std::printf("  moe_diff=%s index=%zu actual=%.9g reference=%.9g\n",
                    label, i, a, b);
      ++unequal;
    }
    max_abs = std::max(max_abs, std::abs(a - b));
    squared += (a - b) * (a - b);
    ref_squared += b * b;
  }
  const double rel_l2 =
      std::sqrt(ref_squared > 0.0 ? squared / ref_squared : squared);
  std::printf(
      "  moe_compare=%s elements=%zu unequal=%zu finite=%d "
      "max_abs=%.9g relative_l2=%.9g\n",
      label, actual.size(), unequal, finite, max_abs, rel_l2);
  if (!finite) throw std::runtime_error("nonfinite MoE observation");
  return unequal == 0;
}

struct Observation {
  std::vector<float> first_output;
  std::vector<uint16_t> first_down;
};

Observation Run(const q4t::quant::MoEWeightLayout& weights,
                const std::filesystem::path& dir, const char* name, int rows,
                bool shared_routes, bool changed_suffix) {
  std::vector<uint16_t> x(static_cast<size_t>(rows) * kHidden);
  std::vector<int32_t> ids(static_cast<size_t>(rows) * kSlots);
  std::vector<float> router(ids.size());
  for (int row = 0; row < rows; ++row) {
    for (int c = 0; c < kHidden; ++c) {
      // Fixed, implementation-independent, finite BF16 fixture. No RNG,
      // checkpoint sampling, or archived activation file is needed.
      const int seed = (c * 37 + row * 113 + 17) % 509 - 254;
      const float original = static_cast<float>(seed) / 128.0f;
      const float value = changed_suffix && row > 0
                              ? -original * 4.0f + static_cast<float>(row)
                              : original;
      x[static_cast<size_t>(row) * kHidden + c] = Bf16(value);
    }
    for (int slot = 0; slot < kSlots; ++slot) {
      const size_t at = static_cast<size_t>(row) * kSlots + slot;
      ids[at] = row == 0 || shared_routes ? kFirstIds[slot]
                                         : 260 + row * kSlots + slot;
      router[at] = kRouterWeights[slot];
    }
  }
  Save(dir, std::string(name) + ".x.bf16", x);
  Save(dir, std::string(name) + ".ids.i32", ids);
  Save(dir, std::string(name) + ".router.f32", router);

  DeviceBuffer<uint16_t> d_x(x.size());
  DeviceBuffer<int32_t> d_ids(ids.size());
  DeviceBuffer<float> d_router(router.size());
  DeviceBuffer<float> d_y(x.size());
  const size_t bytes = q4t::quant::MoEWorkspace::RequiredBytes(
      rows, kSlots, kHidden, kIntermediate);
  DeviceBuffer<uint8_t> d_workspace(bytes);
  DeviceBuffer<uint8_t> d_gemm(kGemmBytes);
  RequireCuda(cudaMemcpy(d_x.data(), x.data(), x.size() * sizeof(uint16_t),
                         cudaMemcpyHostToDevice), "copy MoE x");
  RequireCuda(cudaMemcpy(d_ids.data(), ids.data(), ids.size() * sizeof(int32_t),
                         cudaMemcpyHostToDevice), "copy MoE expert ids");
  RequireCuda(cudaMemcpy(d_router.data(), router.data(),
                         router.size() * sizeof(float), cudaMemcpyHostToDevice),
              "copy MoE router weights");
  RequireCuda(cudaMemset(d_y.data(), 0, x.size() * sizeof(float)),
              "zero MoE y");
  Require(q4t::quant::MoERoutedForward(
              d_x.data(), d_ids.data(), d_router.data(), d_y.data(), weights,
              d_workspace.data(), d_gemm.data(), kGemmBytes, rows, kSlots,
              nullptr),
          "public MoERoutedForward");
  RequireCuda(cudaDeviceSynchronize(), "complete MoE diagnostic call");

  q4t::quant::MoEWorkspace layout;
  const size_t slots = static_cast<size_t>(rows) * kSlots;
  layout.a_packed_bytes = slots * (kHidden / 2);
  layout.a_sf_bytes = q4t::quant::SfBufferSize(rows * kSlots, kHidden);
  layout.gu_out_bytes = slots * 2 * kIntermediate * sizeof(uint16_t);
  layout.dn_out_bytes = slots * kHidden * sizeof(uint16_t);
  if (layout.TotalBytes() != bytes)
    throw std::runtime_error("MoE workspace layout mismatch");
  layout.Init(d_workspace.data());
  const auto all_output = Read(d_y.data(), x.size());
  const auto all_down = Read(layout.dn_out, slots * kHidden);
  Save(dir, std::string(name) + ".output.f32", all_output);
  Save(dir, std::string(name) + ".down.bf16", all_down);
  for (float value : all_output)
    if (!std::isfinite(value)) throw std::runtime_error("nonfinite MoE output");
  for (uint16_t value : all_down)
    if (!std::isfinite(Value(value)))
      throw std::runtime_error("nonfinite MoE down output");
  Observation result;
  result.first_output.assign(all_output.begin(), all_output.begin() + kHidden);
  if (rows == 1 || !shared_routes) {
    // Grouped M=1 stores down in slot order. The disjoint generic case has
    // exactly one row per expert, so its offset is the count of smaller ids.
    // With shared routes the internal atomic token-list order is not public;
    // retain the complete down matrix without inventing a first-row mapping.
    result.first_down.resize(static_cast<size_t>(kSlots) * kHidden);
    for (int slot = 0; slot < kSlots; ++slot) {
      const int offset = rows == 1
                             ? slot
                             : static_cast<int>(std::count_if(
                                   ids.begin(), ids.end(), [&](int32_t id) {
                                     return id < kFirstIds[slot];
                                   }));
      std::copy_n(all_down.begin() + static_cast<size_t>(offset) * kHidden,
                  kHidden,
                  result.first_down.begin() +
                      static_cast<size_t>(slot) * kHidden);
    }
    Save(dir, std::string(name) + ".first_down_slots.bf16", result.first_down);
    std::vector<float> reference(kHidden, 0.0f);
    for (int c = 0; c < kHidden; ++c) {
      float sum = 0.0f;
      for (int slot = 0; slot < kSlots; ++slot)
        sum = std::fma(kRouterWeights[slot],
                       Value(result.first_down[slot * kHidden + c]), sum);
      reference[c] += sum;  // Both production kernels add into the zeroed y.
    }
    Save(dir, std::string(name) + ".combine_reference.f32", reference);
    const std::string label = std::string(name) + "_slot_combine";
    if (!Report(label.c_str(), result.first_output, reference))
      throw std::runtime_error("MoE slot-order combine differs");
  }
  std::printf("  moe_case=%s M=%d first_expert_rows=%d suffix_changed=%d\n",
              name, rows, shared_routes ? rows : 1, changed_suffix);
  return result;
}

}  // namespace

Q4T_TEST(mtp_moe_path_diagnostic) {
  const char* evidence_dir = std::getenv("Q4T_MTP_MOE_DIAG_DIR");
  if (!evidence_dir || !*evidence_dir)
    throw std::runtime_error("set Q4T_MTP_MOE_DIAG_DIR to a new evidence dir");
  const std::filesystem::path dir(evidence_dir);
  if (!std::filesystem::create_directory(dir))
    throw std::runtime_error("MoE diagnostic evidence directory exists");
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  Q4T_CHECK(std::getenv("Q4T_MOE_STREAMS") == nullptr);
  const std::string model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  q4t::io::WeightIndex* index_raw = nullptr;
  Require(q4t::io::WeightIndex::Open(
              model_dir + "/model.safetensors.index.json", &index_raw),
          "open model index");
  std::unique_ptr<q4t::io::WeightIndex> index(index_raw);
  q4t::io::WeightLoader* loader_raw = nullptr;
  Require(q4t::io::WeightLoader::Create(model_dir, *index, 16, &loader_raw),
          "create read-only model loader");
  std::unique_ptr<q4t::io::WeightLoader> loader(loader_raw);
  WeightsOwner owner;
  Require(q4t::quant::LoadMoEWeights(*loader, 0, kExperts, kHidden,
                                     kIntermediate, &owner.value, nullptr),
          "load actual layer-zero MoE");
  const auto a = Run(owner.value, dir, "A_grouped", 1, false, false);
  const auto b = Run(owner.value, dir, "B_disjoint", 4, false, false);
  const auto c = Run(owner.value, dir, "C_shared", 4, true, false);
  const auto d = Run(owner.value, dir, "D_suffix", 4, true, true);
  const bool ab = Report("B_vs_A_output", b.first_output, a.first_output);
  const bool ac = Report("C_vs_A_output", c.first_output, a.first_output);
  const bool bc = Report("C_vs_B_output", c.first_output, b.first_output);
  const bool down = Report("B_vs_A_down", b.first_down, a.first_down);
  const bool suffix = Report("D_vs_C_suffix", d.first_output, c.first_output);
  std::printf(
      "MTP_MOE_PATH cases=4 layer=0 k=10 default_moe_streams=4 "
      "ab_equal=%d ac_equal=%d bc_equal=%d ab_down_equal=%d "
      "suffix_exact=%d cross_shape_gate=none\n",
      ab, ac, bc, down, suffix);
  return suffix;
}
