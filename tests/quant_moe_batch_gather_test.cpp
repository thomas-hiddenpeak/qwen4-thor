// Three fixed legacy/batch pairs and one nonfatal submitted-work failure.
// Canonicalization observes atomic token-list order without changing routing.
#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_gemm_test.h"
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
#include <utility>
#include <vector>

namespace {
namespace quant = q4t::quant;
using Layout = quant::MoERoutedCaptureLayout;
constexpr int kTokens = 4, kTop = 10, kRoutes = 40;
constexpr int kExperts = 512, kHidden = 2560, kIntermediate = 640;
constexpr size_t kGemmBytes = 32u * 1024u * 1024u;
constexpr size_t kArtifactLimit = 32u * 1024u * 1024u;
constexpr size_t kSlotBytes = 25600;
constexpr std::array<size_t, 6> kRowBytes = {1280, 160, 2560, 320, 40, 5120};
constexpr std::array<const char*, 6> kStageNames = {
    "gu_packed", "gu_sf", "gu_bf16", "dn_packed", "dn_sf", "dn_bf16"};

void Require(bool value, const std::string& message) {
  if (!value) throw std::runtime_error(message);
}
void RequireStatus(const q4t::Status& status, const char* operation) {
  if (!status.ok())
    throw std::runtime_error(std::string(operation) + ": " + status.message());
}
void RequireCuda(cudaError_t status, const char* operation) {
  if (status != cudaSuccess)
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(status));
}
void FreeDevice(void* pointer) {
  if (pointer && cudaFree(pointer) != cudaSuccess) std::abort();
}
class Environment {
 public:
  explicit Environment(const char* name) : name_(name) {
    const char* value = std::getenv(name);
    present_ = value != nullptr;
    if (value) value_ = value;
  }
  ~Environment() {
    if ((present_ ? setenv(name_.c_str(), value_.c_str(), 1)
                  : unsetenv(name_.c_str())) != 0)
      std::abort();
  }
  void Set(const char* value) {
    Require((value ? setenv(name_.c_str(), value, 1)
                   : unsetenv(name_.c_str())) == 0,
            "set test environment");
  }

 private:
  std::string name_, value_;
  bool present_ = false;
};

std::array<int32_t, kRoutes> Routes(int fixture) {
  constexpr std::array<int32_t, kRoutes> disjoint = {
      511, 0,  37,  91,  3,   211, 127, 401, 53,  7,   5,   11, 13, 17,
      19,  23, 29,  31,  41,  43,  47,  59,  61,  67,  71,  73, 79, 83,
      89,  97, 101, 103, 107, 109, 113, 131, 137, 139, 149, 151};
  constexpr std::array<int32_t, kRoutes> mixed = {
      511, 5,  37, 3,   7,   0,  11, 401, 13,  17,  19, 37,  0,   3,
      23,  91, 29, 511, 211, 31, 0,  41,  211, 37,  43, 511, 127, 47,
      91,  53, 59, 127, 511, 61, 91, 67,  0,   401, 71, 73};
  if (fixture == 0) return disjoint;
  if (fixture == 2) return mixed;
  auto shared = disjoint;
  for (int flat = 0; flat < kRoutes; ++flat)
    shared[flat] = disjoint[flat % kTop];
  return shared;
}
std::array<int32_t, kExperts> Counts(const std::array<int32_t, kRoutes>& ids) {
  std::array<int32_t, kExperts> counts{};
  for (int t = 0; t < kTokens; ++t) {
    std::array<bool, kExperts> seen{};
    for (int slot = 0; slot < kTop; ++slot) {
      const int e = ids[t * kTop + slot];
      Require(e >= 0 && e < kExperts && !seen[e], "invalid fixed route");
      seen[e] = true;
      ++counts[e];
    }
  }
  return counts;
}
void CheckPlan(const std::array<int32_t, kExperts>& counts) {
  quant::MoEBatchGatherPlan plan;
  Require(quant::MakeMoEBatchGatherPlan(counts.data(), kExperts, &plan),
          "legal descriptor plan rejected");
  int ordinal = 0, total = 0;
  std::array<int, 4> used{};
  for (int e = 0; e < kExperts; ++e) {
    if (counts[e] == 0) continue;
    const int stream = ordinal % 4;
    for (int row = 0; row < counts[e]; ++row) {
      Require(used[stream] < kRoutes, "descriptor capacity before indexing");
      const auto& actual = plan.streams[stream].rows[used[stream]++];
      Require(actual.expert == e && actual.local_row == row &&
                  actual.active_ordinal == ordinal,
              "descriptor mapping");
      ++total;
    }
    ++ordinal;
  }
  Require(total == kRoutes && plan.active_experts == ordinal,
          "descriptor conservation");
  for (int stream = 0; stream < 4; ++stream)
    Require(plan.streams[stream].count == used[stream],
            "descriptor stream count");
}
void ContractPassed(const char* name) {
  std::printf("MOE_BATCH_GATHER_CONTRACT case=%s passed=1\n", name);
}
float Value(uint16_t bits) {
  return std::bit_cast<float>(static_cast<uint32_t>(bits) << 16);
}
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
              "read observation");
  return values;
}
void Sentinel(const std::vector<uint8_t>& bytes, size_t begin, size_t end,
              const std::string& label) {
  Require(begin <= end && end <= bytes.size(), label + " guard bounds");
  for (size_t i = begin; i < end; ++i)
    if (bytes[i] != Layout::kSentinel)
      throw std::runtime_error(label + " sentinel changed at " +
                               std::to_string(i));
}
template <typename T>
void Exact(const std::vector<T>& a, const std::vector<T>& b,
           const std::string& label) {
  Require(a.size() == b.size(), label + " shape");
  size_t unequal = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    if (std::memcmp(&a[i], &b[i], sizeof(T)) == 0) continue;
    if (unequal < 4)
      std::printf("MOE_BATCH_GATHER_DIFF label=%s index=%zu\n", label.c_str(),
                  i);
    ++unequal;
  }
  if (unequal)
    throw std::runtime_error(label + " unequal=" + std::to_string(unequal));
}
class Guarded {
 public:
  explicit Guarded(size_t bytes) : bytes_(bytes) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&base_),
                           bytes + 2 * Layout::kGuard),
                "allocate guarded");
    Poison();
  }
  ~Guarded() { FreeDevice(base_); }
  Guarded(const Guarded&) = delete;
  Guarded& operator=(const Guarded&) = delete;
  template <typename T = uint8_t>
  T* data() const {
    return reinterpret_cast<T*>(base_ + Layout::kGuard);
  }
  void Poison() {
    RequireCuda(
        cudaMemset(base_, Layout::kSentinel, bytes_ + 2 * Layout::kGuard),
        "poison guarded");
  }
  template <typename T>
  void Upload(const std::vector<T>& values) {
    Require(values.size() * sizeof(T) == bytes_, "upload byte size");
    RequireCuda(
        cudaMemcpy(data(), values.data(), bytes_, cudaMemcpyHostToDevice),
        "upload data");
  }
  void Check(const char* label) const {
    const auto before = Read(base_, Layout::kGuard);
    const auto after = Read(base_ + Layout::kGuard + bytes_, Layout::kGuard);
    Sentinel(before, 0, before.size(), label);
    Sentinel(after, 0, after.size(), label);
  }

 private:
  uint8_t* base_ = nullptr;
  size_t bytes_;
};
struct WeightsOwner {
  quant::MoEWeightLayout value;
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
class Evidence {
 public:
  explicit Evidence(const char* path) : dir_(path) {
    Require(!std::filesystem::exists(dir_), "evidence path already exists");
    Require(std::filesystem::create_directories(dir_), "create evidence dir");
    manifest_.open(dir_ / "manifest.tsv", std::ios::out);
    manifest_.exceptions(std::ios::failbit | std::ios::badbit);
    manifest_ << "file\tbytes\n";
  }
  template <typename T>
  void Save(const std::string& name, const std::vector<T>& values) {
    const size_t bytes = values.size() * sizeof(T);
    Require(bytes <= kArtifactLimit - bytes_, "artifact byte limit");
    std::ofstream file(dir_ / name, std::ios::out | std::ios::binary);
    file.exceptions(std::ios::failbit | std::ios::badbit);
    file.write(reinterpret_cast<const char*>(values.data()),
               static_cast<std::streamsize>(bytes));
    file.close();
    manifest_ << name << '\t' << bytes << '\n';
    manifest_.flush();
    bytes_ += bytes;
  }
  size_t bytes() const { return bytes_; }

 private:
  std::filesystem::path dir_;
  std::ofstream manifest_;
  size_t bytes_ = 0;
};
struct Fixture {
  std::vector<uint16_t> x;
  std::vector<int32_t> ids;
  std::vector<float> router, initial_y;
  explicit Fixture(int fixture) {
    const auto routes = Routes(fixture);
    ids.assign(routes.begin(), routes.end());
    x.resize(kTokens * kHidden);
    initial_y.resize(x.size());
    router.resize(kRoutes);
    for (int t = 0; t < kTokens; ++t) {
      for (int c = 0; c < kHidden; ++c) {
        const size_t at = static_cast<size_t>(t) * kHidden + c;
        const float v =
            static_cast<float>((37 * c + 113 * t + 17) % 509 - 254) / 128.0f;
        x[at] = Bf16(c < 16 ? 0.0f : v);
        initial_y[at] =
            static_cast<float>((13 * c + 29 * t) % 127 - 63) / 512.0f;
      }
      for (int slot = 0; slot < kTop; ++slot)
        router[t * kTop + slot] =
            static_cast<float>((slot * 7 + t * 3) % 10 + 1) / 55.0f;
    }
  }
};
struct Buffers {
  Guarded x{kTokens * kHidden * sizeof(uint16_t)};
  Guarded ids{kRoutes * sizeof(int32_t)};
  Guarded router{kRoutes * sizeof(float)};
  Guarded y{kTokens * kHidden * sizeof(float)};
  Guarded workspace{quant::MoEWorkspace::RequiredBytes(kTokens, kTop, kHidden,
                                                       kIntermediate)};
  Guarded gemm{kGemmBytes};
  Guarded capture{Layout::kBytes};
  void Prepare(const Fixture& fixture) {
    x.Upload(fixture.x);
    ids.Upload(fixture.ids);
    router.Upload(fixture.router);
    y.Upload(fixture.initial_y);
    workspace.Poison();
    gemm.Poison();
    capture.Poison();
  }
  void Check(const Fixture& fixture) const {
    x.Check("x");
    ids.Check("ids");
    router.Check("router");
    y.Check("y");
    workspace.Check("workspace");
    gemm.Check("gemm");
    capture.Check("capture");
    Exact(Read(x.data<uint16_t>(), fixture.x.size()), fixture.x, "input x");
    Exact(Read(ids.data<int32_t>(), fixture.ids.size()), fixture.ids,
          "input ids");
    Exact(Read(router.data<float>(), fixture.router.size()), fixture.router,
          "input router");
  }
};
struct Observation {
  quant::MoERoutedCapture metadata;
  std::vector<uint8_t> raw;
  std::array<std::vector<uint8_t>, 6> canonical;
  std::vector<float> output;
};
template <typename T>
T At(const std::vector<uint8_t>& bytes, size_t offset) {
  Require(offset <= bytes.size() && sizeof(T) <= bytes.size() - offset,
          "observation bounds");
  T result;
  std::memcpy(&result, bytes.data() + offset, sizeof(T));
  return result;
}
void RegionGuards(const std::vector<uint8_t>& raw, size_t offset, size_t bytes,
                  const char* name) {
  Sentinel(raw, offset - Layout::kGuard, offset, name);
  Sentinel(raw, offset + bytes, offset + bytes + Layout::kGuard, name);
}
void CheckGemm(const quant::MoECapturedGemm& actual, int rows, bool gu,
               float alpha) {
  const quant::LtPlanKey key{rows, gu ? 1280 : 2560, gu ? 2560 : 640,
                             kGemmBytes, 16};
  Require(actual.has_algo && actual.key == key &&
              actual.alpha_bits == std::bit_cast<uint32_t>(alpha),
          "actual GEMM key/alpha/workspace/BF16 contract");
}
void Validate(Observation& observed, const Fixture& fixture,
              const quant::MoEWeightLayout& weights, bool batch) {
  const auto& m = observed.metadata;
  Require(m.batch_applied == batch && m.streams == 4 && m.cleanup_joined &&
              m.cleanup_freed && !m.injection_triggered,
          "actual path/cleanup contract");
  std::array<int32_t, kRoutes> ids;
  std::copy(fixture.ids.begin(), fixture.ids.end(), ids.begin());
  const auto counts = Counts(ids);
  Require(m.counts == counts && m.offsets[0] == 0, "routing counts");
  int active = 0;
  for (int e = 0; e < kExperts; ++e) {
    Require(m.offsets[e + 1] == m.offsets[e] + counts[e], "routing offsets");
    if (counts[e]) ++active;
  }
  Require(m.offsets[kExperts] == kRoutes && m.active_experts == active,
          "route conservation");
  std::array<bool, kRoutes> visited{};
  std::vector<bool> arena_written(quant::MoEBatchGatherExtraBytes(), false);
  for (int stage = 0; stage < 6; ++stage)
    observed.canonical[stage].resize(kRoutes * kRowBytes[stage]);
  int ordinal = 0;
  for (int e = 0; e < kExperts; ++e) {
    if (counts[e] == 0) continue;
    const auto& expert = m.experts[ordinal];
    Require(expert.expert == e && expert.rows == counts[e] &&
                expert.active_ordinal == ordinal &&
                expert.stream_index == ordinal % 4 &&
                expert.grouped_offset == m.offsets[e],
            "expert metadata");
    CheckGemm(expert.gu, counts[e], true,
              weights.gu_w_scale2_h[e] * weights.gu_input_scale_h[e]);
    CheckGemm(expert.dn, counts[e], false,
              weights.dn_w_scale2_h[e] * weights.dn_input_scale_h[e]);
    for (int local = 0; local < counts[e]; ++local) {
      const int flat =
          At<int32_t>(observed.raw,
                      Layout::kTokenListOffset +
                          static_cast<size_t>(4 * e + local) * sizeof(int32_t));
      Require(flat >= 0 && flat < kRoutes && !visited[flat] &&
                  fixture.ids[flat] == e,
              "token-list canonical bijection");
      visited[flat] = true;
      Require(At<int32_t>(observed.raw,
                          Layout::kRowMapOffset +
                              static_cast<size_t>(flat) * sizeof(int32_t)) ==
                  m.offsets[e] + local,
              "row-of-flat inverse");
      for (int stage = 0; stage < 6; ++stage) {
        const size_t base = Layout::StageOffset(ordinal, stage);
        auto* destination = observed.canonical[stage].data() +
                            static_cast<size_t>(flat) * kRowBytes[stage];
        if (stage == 1 || stage == 4) {
          const int reduction = stage == 1 ? kHidden : kIntermediate;
          for (int group = 0; group < reduction / 16; ++group)
            destination[group] =
                observed
                    .raw[base + quant::SfOffset(local, group,
                                                quant::SfNumGtiles(reduction))];
        } else {
          std::memcpy(destination,
                      observed.raw.data() + base +
                          static_cast<size_t>(local) * kRowBytes[stage],
                      kRowBytes[stage]);
        }
      }
      const size_t slot = static_cast<size_t>(ordinal) * kSlotBytes;
      for (int c = 0; c < kHidden / 2; ++c) {
        const size_t at = slot + static_cast<size_t>(local) * (kHidden / 2) + c;
        arena_written[at] = true;
        if (batch)
          Require(
              observed.raw[Layout::kArenaOffset + Layout::kGuard + at] ==
                  observed.canonical[0][static_cast<size_t>(flat) * 1280 + c],
              "arena packed source equals GEMM capture");
      }
      for (int group = 0; group < kHidden / 16; ++group) {
        const size_t at =
            slot + 5120 +
            quant::SfOffset(local, group, quant::SfNumGtiles(kHidden));
        arena_written[at] = true;
        if (batch)
          Require(
              observed.raw[Layout::kArenaOffset + Layout::kGuard + at] ==
                  observed
                      .canonical[1][static_cast<size_t>(flat) * 160 + group],
              "arena SF source equals GEMM capture");
      }
    }
    ++ordinal;
  }
  Require(std::all_of(visited.begin(), visited.end(), [](bool v) { return v; }),
          "canonical route coverage");
  for (int ord = 0; ord < kRoutes; ++ord)
    for (int stage = 0; stage < 6; ++stage) {
      const size_t offset = Layout::StageOffset(ord, stage);
      RegionGuards(observed.raw, offset, Layout::kStageBytes[stage], "stage");
      if (ord >= active)
        Sentinel(observed.raw, offset, offset + Layout::kStageBytes[stage],
                 "unused expert capture");
      else if (stage != 1 && stage != 4)
        Sentinel(observed.raw, offset + m.experts[ord].rows * kRowBytes[stage],
                 offset + Layout::kStageBytes[stage], "unused capture rows");
      // Legacy SF padding is intentionally not a numerical oracle.
    }
  RegionGuards(observed.raw, Layout::kTokenListOffset, Layout::kTokenListBytes,
               "token list");
  RegionGuards(observed.raw, Layout::kRowMapOffset, Layout::kRowMapBytes,
               "row map");
  RegionGuards(observed.raw, Layout::kArenaOffset, Layout::kArenaBytes,
               "arena");
  if (batch) {
    Sentinel(observed.raw, Layout::kArenaOffset,
             Layout::kArenaOffset + Layout::kGuard, "actual arena prefix");
    const size_t arena = Layout::kArenaOffset + Layout::kGuard;
    for (size_t i = 0; i < arena_written.size(); ++i)
      if (!arena_written[i] && observed.raw[arena + i] != Layout::kSentinel)
        throw std::runtime_error("actual arena padding/unused row overwritten");
    Sentinel(observed.raw, arena + arena_written.size(),
             Layout::kArenaOffset + Layout::kArenaBytes, "actual arena suffix");
  } else {
    Sentinel(observed.raw, Layout::kArenaOffset,
             Layout::kArenaOffset + Layout::kArenaBytes, "legacy no arena");
  }
  for (int stage : {2, 5})
    for (size_t i = 0; i < observed.canonical[stage].size(); i += 2)
      if (!std::isfinite(Value(At<uint16_t>(observed.canonical[stage], i))))
        throw std::runtime_error("nonfinite captured BF16");
  std::vector<float> combined(fixture.initial_y.size());
  for (int t = 0; t < kTokens; ++t)
    for (int c = 0; c < kHidden; ++c) {
      float acc = 0.0f;
      for (int slot = 0; slot < kTop; ++slot) {
        const int flat = t * kTop + slot;
        const float down = Value(At<uint16_t>(
            observed.canonical[5],
            (static_cast<size_t>(flat) * kHidden + c) * sizeof(uint16_t)));
        acc = std::fma(fixture.router[flat], down, acc);
      }
      const size_t at = static_cast<size_t>(t) * kHidden + c;
      combined[at] = fixture.initial_y[at] + acc;
      if (!std::isfinite(observed.output[at]))
        throw std::runtime_error("nonfinite routed output");
    }
  Exact(observed.output, combined, "independent slot-order combine");
}
void SaveMetadata(Evidence& evidence, const std::string& label,
                  const Observation& result) {
  const auto& m = result.metadata;
  Require(m.active_experts >= 0 && m.active_experts <= kRoutes,
          "metadata expert capacity");
  std::vector<int32_t> meta{m.active_experts, m.streams, m.batch_applied};
  meta.insert(meta.end(), m.counts.begin(), m.counts.end());
  meta.insert(meta.end(), m.offsets.begin(), m.offsets.end());
  std::vector<uint8_t> algorithms;
  for (int ord = 0; ord < m.active_experts; ++ord) {
    const auto& e = m.experts[ord];
    meta.insert(meta.end(), {e.expert, e.rows, e.active_ordinal, e.stream_index,
                             e.grouped_offset});
    for (const auto* gemm : {&e.gu, &e.dn}) {
      meta.insert(meta.end(),
                  {gemm->key.M, gemm->key.N, gemm->key.K,
                   static_cast<int32_t>(gemm->key.ws), gemm->key.out_bits,
                   std::bit_cast<int32_t>(gemm->alpha_bits), gemm->has_algo});
      algorithms.insert(algorithms.end(), gemm->algo.begin(), gemm->algo.end());
    }
  }
  evidence.Save(label + ".metadata.i32", meta);
  evidence.Save(label + ".algorithms.u8", algorithms);
}
Observation Run(Buffers& buffers, const Fixture& fixture,
                const quant::MoEWeightLayout& weights, bool batch, bool inject,
                Evidence& evidence, const std::string& label) {
  buffers.Prepare(fixture);
  Observation result;
  auto& capture = result.metadata;
  capture.device_buffer = buffers.capture.data();
  capture.device_bytes = Layout::kBytes;
  capture.inject_after_first_extra_batch = inject;
  const auto status = quant::MoERoutedForwardForTest(
      buffers.x.data<uint16_t>(), buffers.ids.data<int32_t>(),
      buffers.router.data<float>(), buffers.y.data<float>(), weights,
      buffers.workspace.data(), buffers.gemm.data(), kGemmBytes, kTokens, kTop,
      nullptr, batch, &capture);
  RequireCuda(cudaStreamSynchronize(nullptr), "complete captured call");
  result.raw = Read(buffers.capture.data(), Layout::kBytes);
  result.output = Read(buffers.y.data<float>(), fixture.initial_y.size());
  // Persist observations before assertions, retaining any first failure.
  evidence.Save(label + ".capture.u8", result.raw);
  evidence.Save(label + ".output.f32", result.output);
  SaveMetadata(evidence, label, result);
  buffers.Check(fixture);
  if (inject) {
    Require(!status.ok() && capture.injection_triggered &&
                capture.cleanup_joined && capture.cleanup_freed &&
                status.message() == "injected after first extra batch gather",
            "nonfatal submitted-work failure cleanup contract");
    std::printf(
        "MOE_BATCH_GATHER_INJECTION case=mixed attempts=1 triggered=1 "
        "joined=1 freed=1 status_fail=1\n");
    return result;
  }
  RequireStatus(status, "captured routed forward");
  Validate(result, fixture, weights, batch);
  for (int stage = 0; stage < 6; ++stage)
    evidence.Save(label + "." + kStageNames[stage] + ".canonical.u8",
                  result.canonical[stage]);
  return result;
}
}  // namespace

Q4T_TEST(quant_moe_batch_gather_contract) {
  Environment gate("Q4T_MOE_BATCH_GATHER");
  for (const char* value :
       {static_cast<const char*>(nullptr), "", "0", "true", "01"}) {
    gate.Set(value);
    Require(!quant::MoEBatchGatherEnabled(), "non-1 env must disable");
  }
  gate.Set("1");
  Require(quant::MoEBatchGatherEnabled(), "1 env must enable");
  ContractPassed("environment");
  std::array<int, 6> shape{4, 10, 512, 2560, 640, 4};
  auto supported = [](const std::array<int, 6>& s) {
    return quant::MoEBatchGatherShapeSupported(s[0], s[1], s[2], s[3], s[4],
                                               s[5]);
  };
  Require(supported(shape), "fixed shape supported");
  for (int dim = 0; dim < 6; ++dim) {
    auto changed = shape;
    --changed[dim];
    Require(!supported(changed), "changed dimension must use legacy");
  }
  ContractPassed("shape");
  for (int fixture = 0; fixture < 3; ++fixture) {
    CheckPlan(Counts(Routes(fixture)));
    ContractPassed(
        std::array{"disjoint_plan", "shared_plan", "mixed_plan"}[fixture]);
  }
  auto counts = Counts(Routes(0));
  quant::MoEBatchGatherPlan plan;
  counts[0] = -1;
  Require(!quant::MakeMoEBatchGatherPlan(counts.data(), kExperts, &plan),
          "negative raw count rejected");
  counts[0] = 5;
  Require(!quant::MakeMoEBatchGatherPlan(counts.data(), kExperts, &plan),
          "overflow raw count rejected");
  ContractPassed("count_range");
  counts = Counts(Routes(0));
  --counts[0];
  Require(!quant::MakeMoEBatchGatherPlan(counts.data(), kExperts, &plan),
          "missing route rejected");
  counts[0] += 2;
  Require(!quant::MakeMoEBatchGatherPlan(counts.data(), kExperts, &plan),
          "extra route rejected");
  ContractPassed("count_sum");
  Require(
      !quant::MakeMoEBatchGatherPlan(nullptr, kExperts, &plan) &&
          !quant::MakeMoEBatchGatherPlan(counts.data(), kExperts, nullptr) &&
          !quant::MakeMoEBatchGatherPlan(counts.data(), 511, &plan),
      "null/shape plan rejected");
  ContractPassed("plan_arguments");
  for (int reduction : {kHidden, kIntermediate})
    for (int rows = 1; rows <= 4; ++rows) {
      std::vector<bool> seen(quant::SfBufferSize(rows, reduction));
      Require(seen.size() == (reduction == kHidden ? 20480u : 5120u),
              "independent SF atom size");
      for (int row = 0; row < rows; ++row)
        for (int group = 0; group < reduction / 16; ++group) {
          const size_t at =
              quant::SfOffset(row, group, quant::SfNumGtiles(reduction));
          Require(at < seen.size() && !seen[at], "logical SF collision/bounds");
          seen[at] = true;
        }
    }
  ContractPassed("sf_layout");
  size_t previous_end = 0;
  for (int ord = 0; ord < kRoutes; ++ord)
    for (int stage = 0; stage < 6; ++stage) {
      const size_t at = Layout::StageOffset(ord, stage);
      Require(at % 256 == 0 && at - Layout::kGuard == previous_end,
              "capture stage alignment/nonoverlap");
      previous_end = at + Layout::kStageBytes[stage] + Layout::kGuard;
    }
  Require(previous_end == Layout::kExpertsBytes &&
              Layout::kTokenListOffset == previous_end + Layout::kGuard &&
              Layout::kRowMapOffset == Layout::kTokenListOffset +
                                           Layout::kTokenListBytes +
                                           2 * Layout::kGuard &&
              Layout::kArenaOffset == Layout::kRowMapOffset +
                                          Layout::kRowMapBytes +
                                          2 * Layout::kGuard &&
              Layout::kBytes ==
                  Layout::kArenaOffset + Layout::kArenaBytes + Layout::kGuard &&
              quant::MoEBatchGatherExtraBytes() == kRoutes * kSlotBytes,
          "capture/arena capacity");
  ContractPassed("capture_layout");
  std::printf("MOE_BATCH_GATHER_CONTRACT_SUMMARY cases=10 passed=1\n");
  return true;
}

Q4T_TEST(quant_moe_batch_gather_exact) {
  const char* path = std::getenv("Q4T_MOE_BATCH_GATHER_TEST_DIR");
  Require(path && *path, "set new Q4T_MOE_BATCH_GATHER_TEST_DIR");
  for (const char* name :
       {"Q4T_MOE_STREAMS", "Q4T_MTP_VERIFY_MOE_TIMING", "Q4T_MTP_CYCLE_TIMING",
        "Q4T_MTP_INIT_TIMING", "Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  Evidence evidence(path);
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  const std::string model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  q4t::io::WeightIndex* raw_index = nullptr;
  RequireStatus(q4t::io::WeightIndex::Open(
                    model_dir + "/model.safetensors.index.json", &raw_index),
                "open index");
  std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
  q4t::io::WeightLoader* raw_loader = nullptr;
  RequireStatus(
      q4t::io::WeightLoader::Create(model_dir, *index, 16, &raw_loader),
      "create read-only loader");
  std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
  WeightsOwner weights;
  RequireStatus(quant::LoadMoEWeights(*loader, 2, kExperts, kHidden,
                                      kIntermediate, &weights.value, nullptr),
                "load actual layer 2 weights");
  Buffers buffers;
  std::printf(
      "MOE_BATCH_GATHER_SCOPE layer=2 M=4 k=10 E=512 hs=2560 "
      "is=640 streams=4 successful_forwards=6 failure_attempts=1 "
      "logical_sf_only=1 canonical_token_slot=1 performance_evidence=0\n");
  for (int fixture_id = 0; fixture_id < 3; ++fixture_id) {
    const std::string name =
        std::array{"disjoint", "shared", "mixed"}[fixture_id];
    const Fixture fixture(fixture_id);
    evidence.Save(name + ".x.bf16", fixture.x);
    evidence.Save(name + ".ids.i32", fixture.ids);
    evidence.Save(name + ".router.f32", fixture.router);
    evidence.Save(name + ".initial_y.f32", fixture.initial_y);
    const auto old = Run(buffers, fixture, weights.value, false, false,
                         evidence, name + ".legacy");
    if (fixture_id == 2)
      Run(buffers, fixture, weights.value, true, true, evidence,
          name + ".injected");
    const auto batch = Run(buffers, fixture, weights.value, true, false,
                           evidence, name + ".batch");
    for (int stage = 0; stage < 6; ++stage)
      Exact(batch.canonical[stage], old.canonical[stage],
            name + "." + kStageNames[stage]);
    Exact(batch.output, old.output, name + ".public_output");
    Require(batch.metadata.active_experts == old.metadata.active_experts,
            "same active expert count");
    for (int ord = 0; ord < old.metadata.active_experts; ++ord) {
      const auto& a = old.metadata.experts[ord];
      const auto& b = batch.metadata.experts[ord];
      for (const auto& pair :
           {std::pair{&a.gu, &b.gu}, std::pair{&a.dn, &b.dn}})
        Require(pair.first->key == pair.second->key &&
                    pair.first->alpha_bits == pair.second->alpha_bits &&
                    pair.first->algo == pair.second->algo &&
                    pair.first->has_algo && pair.second->has_algo,
                "same actual GEMM algorithm/key/alpha");
    }
    std::array<int, 5> histogram{};
    for (int count : old.metadata.counts) ++histogram[count];
    const std::array<std::array<int, 4>, 3> expected{
        {{40, 0, 0, 0}, {0, 0, 0, 10}, {18, 4, 2, 2}}};
    for (int rows = 1; rows <= 4; ++rows)
      Require(histogram[rows] == expected[fixture_id][rows - 1],
              "fixed M_e histogram");
    std::printf(
        "MOE_BATCH_GATHER_PAIR case=%s forwards=2 active=%d "
        "h1=%d h2=%d h3=%d h4=%d canonical_exact=1 "
        "packed_sf_exact=1 gu_dn_exact=1 output_exact=1 "
        "combine_exact=1 gemm_exact=1 guards=1 finite=1\n",
        name.c_str(), old.metadata.active_experts, histogram[1], histogram[2],
        histogram[3], histogram[4]);
    std::fflush(stdout);
  }
  std::printf("MOE_BATCH_GATHER_ARTIFACT bytes=%zu limit=33554432\n",
              evidence.bytes());
  std::printf(
      "MOE_BATCH_GATHER_SUMMARY pairs=3 successful_forwards=6 "
      "injected_failures=1 attempts=7 passed=1\n");
  return true;
}
