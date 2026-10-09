// Four bounded, same-shape A/B checks of the real MTP attention block.
// Only max_position changes: -1 device readback versus the exact host max.
// This is not a CPU attention oracle or a whole-model equivalence claim.
#include "q4t/io/weight_loader.h"
#include "q4t/model/full_attention.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr char kMtpDir[] =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream/mtp";
constexpr char kPrefix[] = "mtp.layers.0.self_attn";
constexpr int kHs = 2560;
constexpr int kNq = 24;
constexpr int kNkv = 2;
constexpr int kHd = 256;
constexpr int kIdxHd = 128;
constexpr int kCompress = 4;
// One pooled slot; capacity includes the whole KV page containing both maxima.
constexpr int kMaxLen = 8208;
constexpr size_t kGuardBytes = 256;
constexpr unsigned char kGuard = 0xa5;
constexpr size_t kKvRowElements = kNkv * 2 * kHd;

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void RequireCuda(cudaError_t error, const char* operation) {
  if (error != cudaSuccess) {
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(error));
  }
}

void RequireStatus(const q4t::Status& status, const char* operation) {
  Require(status.ok(), std::string(operation) + ": " + status.message());
}

void CleanupCuda(cudaError_t error, const char* operation, bool* ok) noexcept {
  if (error != cudaSuccess) {
    *ok = false;
    std::fprintf(stderr, "MTP_KNOWN_POSITION_CLEANUP operation=%s error=%s\n",
                 operation, cudaGetErrorString(error));
  }
}

// Check CUDA releases explicitly, including partially loaded weights. The
// index/loader outlive this owner, so failed asynchronous loads drain before
// their host mappings are released. No production ownership is changed.
class AttentionOwner {
 public:
  explicit AttentionOwner(bool* cleanup_ok) : cleanup_ok_(cleanup_ok) {}
  ~AttentionOwner() {
    CleanupCuda(cudaStreamSynchronize(nullptr), "weight drain", cleanup_ok_);
    Release(weights.q_proj);
    Release(weights.k_proj);
    Release(weights.v_proj);
    Release(weights.o_proj);
    Release(weights.q_norm);
    Release(weights.k_norm);
    Release(weights.index_qk_proj);
    Release(weights.index_q_norm);
    Release(weights.index_k_norm);
    for (auto* shadow : {&weights.q_proj_fp8, &weights.k_proj_fp8,
                         &weights.v_proj_fp8, &weights.o_proj_fp8}) {
      Release(shadow->w);
      Release(shadow->scale);
    }
  }
  AttentionOwner(const AttentionOwner&) = delete;
  AttentionOwner& operator=(const AttentionOwner&) = delete;

  q4t::model::FullAttentionWeights weights;

 private:
  template <typename T>
  void Release(T*& pointer) noexcept {
    if (pointer) {
      CleanupCuda(cudaFree(pointer), "weight free", cleanup_ok_);
      pointer = nullptr;
    }
  }

  bool* cleanup_ok_;
};

template <typename T>
class GuardedBuffer {
 public:
  GuardedBuffer(const char* name, size_t count, bool* cleanup_ok)
      : name_(name), count_(count), cleanup_ok_(cleanup_ok) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&allocation_),
                           bytes() + 2 * kGuardBytes),
                name_);
  }
  ~GuardedBuffer() {
    if (allocation_) CleanupCuda(cudaFree(allocation_), name_, cleanup_ok_);
  }
  GuardedBuffer(const GuardedBuffer&) = delete;
  GuardedBuffer& operator=(const GuardedBuffer&) = delete;

  T* data() const { return reinterpret_cast<T*>(allocation_ + kGuardBytes); }
  size_t bytes() const { return count_ * sizeof(T); }

  void Reset(const std::vector<T>& values) {
    Require(values.size() == count_, std::string(name_) + " reset size");
    ResetGuards();
    RequireCuda(cudaMemcpyAsync(data(), values.data(), bytes(),
                                cudaMemcpyHostToDevice, nullptr),
                name_);
  }

  void ResetZero() {
    ResetGuards();
    RequireCuda(cudaMemsetAsync(data(), 0, bytes(), nullptr), name_);
  }

  std::vector<T> Read() const {
    std::vector<T> result(count_);
    RequireCuda(
        cudaMemcpy(result.data(), data(), bytes(), cudaMemcpyDeviceToHost),
        name_);
    return result;
  }

  void CheckGuards() const {
    std::array<unsigned char, kGuardBytes> before{}, after{};
    RequireCuda(cudaMemcpy(before.data(), allocation_, kGuardBytes,
                           cudaMemcpyDeviceToHost),
                name_);
    RequireCuda(cudaMemcpy(after.data(), allocation_ + kGuardBytes + bytes(),
                           kGuardBytes, cudaMemcpyDeviceToHost),
                name_);
    const auto intact = [](unsigned char value) { return value == kGuard; };
    Require(std::all_of(before.begin(), before.end(), intact) &&
                std::all_of(after.begin(), after.end(), intact),
            std::string(name_) + " guard modified");
  }

 private:
  void ResetGuards() {
    RequireCuda(cudaMemsetAsync(allocation_, kGuard, bytes() + 2 * kGuardBytes,
                                nullptr),
                name_);
  }

  const char* name_;
  size_t count_;
  bool* cleanup_ok_;
  unsigned char* allocation_ = nullptr;
};

// Nonzero, finite, signed dyadic values with exact BF16 representations.
// Independent seeds populate every cache element, including unobserved tail
// capacity. These are injected cache states, not a model-prefill reference.
std::vector<uint16_t> Pattern(size_t count, uint32_t seed) {
  std::vector<uint16_t> values(count);
  uint32_t state = seed;
  for (auto& value : values) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    const int magnitude = 1 + static_cast<int>(state % 127);
    const int numerator = (state & 0x80000000u) ? -magnitude : magnitude;
    const float number = static_cast<float>(numerator) / 256.0f;
    const uint32_t bits = std::bit_cast<uint32_t>(number);
    if ((bits & 0xffffu) != 0)
      throw std::runtime_error("pattern must be exact BF16");
    value = static_cast<uint16_t>(bits >> 16);
  }
  return values;
}

template <typename T>
void Exact(const std::vector<T>& first, const std::vector<T>& second,
           const char* name) {
  Require(first.size() == second.size(), std::string(name) + " size mismatch");
  if (std::memcmp(first.data(), second.data(), first.size() * sizeof(T)) != 0) {
    size_t element = 0;
    while (element < first.size() && first[element] == second[element])
      ++element;
    throw std::runtime_error(std::string(name) + " byte mismatch at element " +
                             std::to_string(element));
  }
}

void Finite(const std::vector<uint16_t>& values, const char* name) {
  for (size_t i = 0; i < values.size(); ++i) {
    if ((values[i] & 0x7f80u) == 0x7f80u) {
      throw std::runtime_error(std::string(name) + " nonfinite at " +
                               std::to_string(i));
    }
  }
}

struct Snapshot {
  std::vector<uint16_t> input, output, kv, raw, comp;
  std::vector<int> positions, rope, page, seq;
};

Snapshot Initial(int rows, int max_position) {
  Snapshot initial;
  initial.input = Pattern(static_cast<size_t>(rows) * kHs, 0x31516u);
  initial.output = Pattern(static_cast<size_t>(rows) * kHs, 0x53118u);
  initial.kv = Pattern(static_cast<size_t>(kMaxLen) * kKvRowElements, 0x19237u);
  initial.raw = Pattern(static_cast<size_t>(kMaxLen) * kIdxHd, 0x71841u);
  initial.comp = Pattern(static_cast<size_t>(kMaxLen) * kIdxHd, 0x12377u);
  initial.positions.resize(rows);
  initial.seq.assign(rows, 0);  // Non-null d_seq_id exercises pooled slot 0.
  for (int row = 0; row < rows; ++row)
    initial.positions[row] = max_position - rows + 1 + row;
  initial.rope.resize(static_cast<size_t>(3) * kMaxLen);
  initial.page.resize(kMaxLen);
  for (int position = 0; position < kMaxLen; ++position) {
    initial.page[position] = position / q4t::model::kKvPageSize;
    for (int axis = 0; axis < 3; ++axis)
      initial.rope[static_cast<size_t>(axis) * kMaxLen + position] = position;
  }
  return initial;
}

// Cache writes are limited to the supplied logical positions and the groups
// completed by those positions. This bounds assertion uses no attention math.
void CheckUntouchedRows(const std::vector<uint16_t>& actual,
                        const std::vector<uint16_t>& initial, size_t width,
                        const std::vector<int>& writable_rows,
                        const char* name) {
  Require(actual.size() == initial.size() && actual.size() % width == 0,
          std::string(name) + " row sizes");
  for (size_t row = 0; row < actual.size() / width; ++row) {
    if (std::find(writable_rows.begin(), writable_rows.end(),
                  static_cast<int>(row)) != writable_rows.end())
      continue;
    Require(
        std::memcmp(actual.data() + row * width, initial.data() + row * width,
                    width * sizeof(uint16_t)) == 0,
        std::string(name) + " unexpected write to row " + std::to_string(row));
  }
}

void CheckSnapshot(const Snapshot& actual, const Snapshot& initial) {
  Finite(actual.input, "input");
  Finite(actual.output, "output");
  Finite(actual.kv, "KV");
  Finite(actual.raw, "raw index");
  Finite(actual.comp, "compressed index");
  Exact(actual.input, initial.input, "read-only input");
  Exact(actual.positions, initial.positions, "read-only positions");
  Exact(actual.rope, initial.rope, "read-only RoPE");
  Exact(actual.page, initial.page, "read-only page table");
  Exact(actual.seq, initial.seq, "read-only pooled slot ids");
  CheckUntouchedRows(actual.kv, initial.kv, kKvRowElements, initial.positions,
                     "KV");
  CheckUntouchedRows(actual.raw, initial.raw, kIdxHd, initial.positions,
                     "raw index");
  std::vector<int> completed;
  for (int position : initial.positions)
    if ((position + 1) % kCompress == 0)
      completed.push_back(position / kCompress);
  CheckUntouchedRows(actual.comp, initial.comp, kIdxHd, completed,
                     "compressed index");
  Require(actual.output != initial.output, "output was not written");
  Require(actual.kv != initial.kv, "KV was not written");
  Require(actual.raw != initial.raw, "raw index was not written");
  if (!completed.empty())
    Require(actual.comp != initial.comp, "completed index was not written");
}

void Compare(const Snapshot& first, const Snapshot& second) {
  Exact(first.input, second.input, "A/B input");
  Exact(first.output, second.output, "A/B output");
  Exact(first.kv, second.kv, "A/B full KV capacity");
  Exact(first.raw, second.raw, "A/B full raw-index capacity");
  Exact(first.comp, second.comp, "A/B full compressed-index capacity");
  Exact(first.positions, second.positions, "A/B positions");
  Exact(first.rope, second.rope, "A/B full RoPE capacity");
  Exact(first.page, second.page, "A/B full page table");
  Exact(first.seq, second.seq, "A/B pooled slot ids");
}

void RunCase(const q4t::model::FullAttentionWeights& weights, int rows,
             int max_position, bool* cleanup_ok) {
  const Snapshot initial = Initial(rows, max_position);
  const int actual_max =
      *std::max_element(initial.positions.begin(), initial.positions.end());
  Require(actual_max == max_position, "incorrect host maximum");
  const size_t workspace_bytes =
      q4t::model::FullAttentionWorkspaceBytes(weights, rows);
  GuardedBuffer<uint16_t> input("input", initial.input.size(), cleanup_ok);
  GuardedBuffer<uint16_t> output("output", initial.output.size(), cleanup_ok);
  GuardedBuffer<uint16_t> kv("KV", initial.kv.size(), cleanup_ok);
  GuardedBuffer<uint16_t> raw("raw index", initial.raw.size(), cleanup_ok);
  GuardedBuffer<uint16_t> comp("compressed index", initial.comp.size(),
                               cleanup_ok);
  GuardedBuffer<int> positions("positions", initial.positions.size(),
                               cleanup_ok);
  GuardedBuffer<int> rope("RoPE", initial.rope.size(), cleanup_ok);
  GuardedBuffer<int> page("page table", initial.page.size(), cleanup_ok);
  GuardedBuffer<int> seq("slot ids", initial.seq.size(), cleanup_ok);
  GuardedBuffer<unsigned char> workspace("workspace", workspace_bytes,
                                         cleanup_ok);

  // Same addresses, same complete initial payloads, same default stream. The
  // reset copies stay ordered before each forward; do not add an interior
  // synchronization to replace the readback that the candidate removes.
  auto run = [&](int supplied_max) {
    input.Reset(initial.input);
    output.Reset(initial.output);
    kv.Reset(initial.kv);
    raw.Reset(initial.raw);
    comp.Reset(initial.comp);
    positions.Reset(initial.positions);
    rope.Reset(initial.rope);
    page.Reset(initial.page);
    seq.Reset(initial.seq);
    workspace.ResetZero();
    RequireStatus(q4t::model::FullAttentionForward(
                      weights, input.data(), output.data(), positions.data(),
                      rope.data(), kv.data(), page.data(), raw.data(),
                      comp.data(), rows, workspace.data(), workspace_bytes,
                      nullptr, seq.data(), supplied_max),
                  "real MTP attention");
    RequireCuda(cudaGetLastError(), "attention launch status");
    RequireCuda(cudaStreamSynchronize(nullptr), "attention completion");
    input.CheckGuards();
    output.CheckGuards();
    kv.CheckGuards();
    raw.CheckGuards();
    comp.CheckGuards();
    positions.CheckGuards();
    rope.CheckGuards();
    page.CheckGuards();
    seq.CheckGuards();
    workspace.CheckGuards();
    Snapshot snapshot{input.Read(), output.Read(), kv.Read(),
                      raw.Read(),   comp.Read(),   positions.Read(),
                      rope.Read(),  page.Read(),   seq.Read()};
    CheckSnapshot(snapshot, initial);
    return snapshot;
  };

  const Snapshot readback = run(-1);
  const Snapshot known = run(actual_max);
  Compare(readback, known);
  const int groups = (actual_max + 1) / kCompress;
  std::printf(
      "MTP_KNOWN_POSITION_CASE T=%d max_position=%d groups=%d "
      "path=%s runs=2 exact=1 finite=1 guards=1\n",
      rows, actual_max, groups, groups <= 2048 ? "short" : "one_pass");
}

}  // namespace

Q4T_TEST(mtp_known_position_boundary) {
  int device_count = 0;
  const cudaError_t availability = cudaGetDeviceCount(&device_count);
  if (availability == cudaErrorNoDevice ||
      availability == cudaErrorInsufficientDriver ||
      (availability == cudaSuccess && device_count == 0))
    Q4T_SKIP("no CUDA device");
  RequireCuda(availability, "CUDA availability");
  const std::string index_path =
      std::string(kMtpDir) + "/model.safetensors.index.json";
  if (!std::filesystem::exists(index_path)) Q4T_SKIP("MTP index not found");

  bool cleanup_ok = true;
  {
    q4t::io::WeightIndex* raw_index = nullptr;
    RequireStatus(q4t::io::WeightIndex::Open(index_path, &raw_index),
                  "MTP index open");
    std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
    q4t::io::WeightLoader* raw_loader = nullptr;
    RequireStatus(
        q4t::io::WeightLoader::Create(kMtpDir, *index, 8, &raw_loader),
        "MTP loader create");
    std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
    AttentionOwner attention(&cleanup_ok);
    RequireStatus(q4t::model::LoadFullAttention(
                      *loader, kPrefix, kHs, kNq, kNkv, kHd, 64, 1e7f, 1e-6f, 4,
                      1, kIdxHd, 2048, kCompress, &attention.weights, nullptr),
                  "load actual MTP attention weights");
    attention.weights.max_len = kMaxLen;
    RequireCuda(cudaStreamSynchronize(nullptr), "MTP weight load completion");
    std::printf(
        "MTP_KNOWN_POSITION_SCOPE prefix=%s slot=0 max_len=%d "
        "stream=default injected_nonzero_cache=1 cpu_oracle=0 "
        "fp8_q=%d fp8_k=%d fp8_v=%d fp8_o=%d\n",
        kPrefix, kMaxLen, attention.weights.q_proj_fp8.w != nullptr,
        attention.weights.k_proj_fp8.w != nullptr,
        attention.weights.v_proj_fp8.w != nullptr,
        attention.weights.o_proj_fp8.w != nullptr);
    for (int rows : {1, 4})
      for (int max_position : {8194, 8195})
        RunCase(attention.weights, rows, max_position, &cleanup_ok);
  }
  Require(cleanup_ok, "CUDA resource cleanup failed");
  std::printf("MTP_KNOWN_POSITION_SUMMARY cases=4 forwards=8 passed=1\n");
  return true;
}
