// Fixed real-MTP Full/Skip initialization comparison; no performance claim.
// Synthetic finite hidden inputs isolate the retained computation and state.
#include "q4t/model/model_head.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using q4t::model::LogitsRows;
using q4t::mtp::MtpForwardMode;
using q4t::mtp::MtpInitPolicy;
using q4t::mtp::MtpModel;
constexpr int kChunk = 8192, kMaxLen = 208896;
constexpr int kHs = 2560, kHc = 4, kVocab = 248320;
constexpr int kLargest = 16385;
constexpr uint16_t kUnwritten = 0x7fc1;
constexpr size_t kGuardBytes = 256;
constexpr char kModelDir[] =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}
void Cuda(cudaError_t error, const char* operation) {
  Require(error == cudaSuccess,
          std::string(operation) + ": " + cudaGetErrorString(error));
}
void Status(const q4t::Status& status, const char* operation) {
  Require(status.ok(), std::string(operation) + ": " + status.message());
}
template <typename T>
std::vector<T> Read(const T* source, size_t count) {
  std::vector<T> values(count);
  Cuda(cudaMemcpy(values.data(), source, count * sizeof(T),
                  cudaMemcpyDeviceToHost),
       "read observation");
  return values;
}
template <typename T>
void Exact(const std::vector<T>& a, const std::vector<T>& b,
           const char* label) {
  Require(a.size() == b.size(), std::string(label) + " shape mismatch");
  Require(std::memcmp(a.data(), b.data(), a.size() * sizeof(T)) == 0,
          std::string(label) + " byte mismatch");
}
void Finite(const std::vector<uint16_t>& values, const char* label) {
  for (uint16_t value : values)
    if ((value & 0x7f80u) == 0x7f80u)
      throw std::runtime_error(std::string(label) + " nonfinite");
}
int Argmax(const std::vector<uint16_t>& logits) {
  Require(logits.size() == kVocab, "argmax needs complete vocabulary");
  Finite(logits, "argmax input");
  int best = 0;
  const auto value = [](uint16_t bits) {
    return std::bit_cast<float>(static_cast<uint32_t>(bits) << 16);
  };
  for (int i = 1; i < kVocab; ++i)
    if (value(logits[i]) > value(logits[best])) best = i;
  return best;
}
template <typename T>
class Guarded {
 public:
  explicit Guarded(size_t count) : count_(count) {
    Cuda(cudaMalloc(reinterpret_cast<void**>(&base_),
                    count * sizeof(T) + 2 * kGuardBytes),
         "allocate guarded buffer");
    Poison();
  }
  ~Guarded() {
    if (cudaFree(base_) != cudaSuccess) std::abort();
  }
  Guarded(const Guarded&) = delete;
  Guarded& operator=(const Guarded&) = delete;
  T* data() const { return reinterpret_cast<T*>(base_ + kGuardBytes); }
  void Set(const std::vector<T>& values) {
    Require(values.size() == count_, "guarded input size");
    Cuda(cudaMemcpy(data(), values.data(), count_ * sizeof(T),
                    cudaMemcpyHostToDevice),
         "set guarded input");
  }
  void Poison() {
    Cuda(cudaMemset(base_, 0xa5, count_ * sizeof(T) + 2 * kGuardBytes),
         "set guards");
    std::vector<T> values(count_, static_cast<T>(kUnwritten));
    Set(values);
  }
  std::vector<T> Check() const {
    std::array<unsigned char, kGuardBytes> before{}, after{};
    Cuda(cudaMemcpy(before.data(), base_, kGuardBytes, cudaMemcpyDeviceToHost),
         "read front guard");
    Cuda(cudaMemcpy(after.data(), base_ + kGuardBytes + count_ * sizeof(T),
                    kGuardBytes, cudaMemcpyDeviceToHost),
         "read end guard");
    const auto canary = [](unsigned char value) { return value == 0xa5; };
    Require(std::all_of(before.begin(), before.end(), canary) &&
                std::all_of(after.begin(), after.end(), canary),
            "buffer guard changed");
    return Read(data(), count_);
  }
  void Unwritten() const {
    const auto values = Check();
    Require(std::all_of(
                values.begin(), values.end(),
                [](T value) { return value == static_cast<T>(kUnwritten); }),
            "skip/disabled output was written");
  }

 private:
  size_t count_;
  unsigned char* base_ = nullptr;
};

struct HeadOwner {
  q4t::model::ModelHeadWeights model;
  ~HeadOwner() { model.Free(); }
};
struct DraftOwner {
  MtpModel model;
  ~DraftOwner() { model.Free(); }
};
struct Cache {
  std::vector<uint16_t> kv, raw, comp;
  std::vector<int> page, rope;
};
Cache Capture(const MtpModel& model) {
  Require(model.max_seq == 1, "S1 cache contract");
  Cache state{Read(model.kv_cache, model.kv_bytes / 2),
              Read(model.idx_raw, model.idx_bytes / 2),
              Read(model.idx_comp, model.idx_bytes / 2),
              Read(model.page_table, model.cfg.max_len),
              Read(model.d_rope_pos, 3u * model.cfg.max_len)};
  Finite(state.kv, "KV");
  Finite(state.raw, "raw index");
  Finite(state.comp, "compressed index");
  return state;
}
void ExactCache(const Cache& a, const Cache& b) {
  Exact(a.kv, b.kv, "full KV capacity");
  Exact(a.raw, b.raw, "full raw-index capacity");
  Exact(a.comp, b.comp, "full compressed-index capacity");
  Exact(a.page, b.page, "full page table");
  Exact(a.rope, b.rope, "full RoPE table");
}
struct Observation {
  Cache cache;
  std::vector<uint16_t> sample, multi, logits;
};

Observation Manual(const MtpModel& model, const std::vector<int32_t>& ids,
                   const std::vector<int>& positions, const uint16_t* hidden,
                   int rows, bool skip) {
  Status(q4t::mtp::MtpResetState(model, nullptr, 0), "reset manual state");
  Observation result;
  for (int base = 0; base < rows; base += kChunk) {
    const int count = std::min(kChunk, rows - base);
    const bool final = base + count == rows;
    const bool skip_chunk = skip && !final;
    Guarded<int32_t> input(count);
    std::vector<int32_t> chunk_ids(ids.begin() + base,
                                   ids.begin() + base + count);
    input.Set(chunk_ids);
    Guarded<uint16_t> sample(static_cast<size_t>(count) * kHs);
    Guarded<uint16_t> multi(static_cast<size_t>(count) * kHs * kHc);
    Guarded<uint16_t> logits(kVocab);
    // Full+!compute_logits must retain hidden generation. Skip uses actual
    // sentinel outputs, whereas the production wrapper may pass nulls.
    Status(q4t::mtp::MtpForward(model, input.data(), positions.data() + base,
                                hidden + static_cast<size_t>(base) * kHs * kHc,
                                sample.data(), multi.data(), logits.data(),
                                count, nullptr, nullptr, final,
                                LogitsRows::kLastRow, nullptr, -1,
                                skip_chunk ? MtpForwardMode::kSkipUnusedTail
                                           : MtpForwardMode::kFull),
           "manual Full/Skip forward");
    Cuda(cudaStreamSynchronize(nullptr), "manual completion");
    Exact(input.Check(), chunk_ids, "read-only device token input");
    if (skip_chunk) {
      sample.Unwritten();
      multi.Unwritten();
      logits.Unwritten();
    } else {
      auto observed_sample = sample.Check();
      auto observed_multi = multi.Check();
      Finite(observed_sample, "sample hidden");
      Finite(observed_multi, "multi hidden");
      if (final) {
        result.sample = std::move(observed_sample);
        result.multi = std::move(observed_multi);
        result.logits = logits.Check();
        Finite(result.logits, "last logits");
      } else {
        logits.Unwritten();
      }
    }
  }
  result.cache = Capture(model);
  return result;
}

void Wrapper(const MtpModel& model, const std::vector<int32_t>& ids,
             const std::vector<int>& positions, const uint16_t* hidden,
             int rows, const Observation& expected) {
  Status(q4t::mtp::MtpResetState(model, nullptr, 0), "reset wrapper state");
  Guarded<uint16_t> g(kHs * kHc);
  int32_t seed = -1;
  Status(q4t::mtp::MtpDraftExtend(model, ids.data(), hidden, positions.data(),
                                  rows, &seed, g.data(), nullptr, 0,
                                  LogitsRows::kLastRow, nullptr,
                                  MtpInitPolicy::kSkipUnusedTail),
         "wrapper Skip policy");
  Cuda(cudaStreamSynchronize(nullptr), "wrapper completion");
  const auto out = g.Check();
  Finite(out, "wrapper g");
  Exact(out,
        std::vector<uint16_t>(expected.multi.end() - kHs * kHc,
                              expected.multi.end()),
        "wrapper final g");
  Require(seed == Argmax(expected.logits), "wrapper independent argmax");
  ExactCache(Capture(model), expected.cache);
}

void InvalidCalls(MtpModel& model) {
  // Every invalid call must return this early policy error, without touching
  // any CUDA pointer. Source review checks the guard precedes GPU submission.
  Guarded<uint16_t> sample(1), multi(1), logits(1);
  const auto before = Capture(model);
  for (int which = 0; which < 6; ++which) {
    const int rows = which == 2 ? 0 : (which == 3 ? kChunk - 1 : kChunk);
    const auto mode = which == 0 ? static_cast<MtpForwardMode>(-1)
                                 : MtpForwardMode::kSkipUnusedTail;
    model.max_seq = which == 4 ? 2 : 1;
    const int* sequence = which == 5 ? model.page_table : nullptr;
    Cuda(cudaGetLastError(), "clear invalid-call CUDA status");
    const q4t::Status status = q4t::mtp::MtpForward(
        model, nullptr, nullptr, nullptr, sample.data(), multi.data(),
        logits.data(), rows, nullptr, sequence, which == 1,
        LogitsRows::kLastRow, nullptr, -1, mode);
    model.max_seq = 1;
    Require(!status.ok() &&
                status.message() ==
                    (which == 0
                         ? "MtpForward: invalid forward mode"
                         : "MtpForward: invalid unused-tail skip combination"),
            "illegal Skip did not fail at the pre-submission policy guard");
    Cuda(cudaGetLastError(), "invalid-call CUDA status");
    sample.Unwritten();
    multi.Unwritten();
    logits.Unwritten();
  }
  ExactCache(Capture(model), before);
}
}  // namespace

Q4T_TEST(mtp_init_tail_direct) {
  for (const char* name :
       {"Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL", "Q4T_MOE_STREAMS",
        "Q4T_MTP_INIT_TIMING", "Q4T_MTP_CYCLE_TIMING"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  Cuda(cudaSetDevice(0), "CUDA required; no SKIP");
  q4t::io::WeightIndex* raw_index = nullptr;
  Status(
      q4t::io::WeightIndex::Open(
          std::string(kModelDir) + "/model.safetensors.index.json", &raw_index),
      "main head index");
  std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
  q4t::io::WeightLoader* raw_loader = nullptr;
  Status(q4t::io::WeightLoader::Create(kModelDir, *index, 8, &raw_loader),
         "main head loader");
  std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
  HeadOwner head;
  Status(q4t::model::LoadModelHead(*loader, kVocab, kHs, kHc, 320, 1e-6f,
                                   &head.model, nullptr),
         "actual embedding/head");
  DraftOwner owner;
  q4t::mtp::MtpConfig cfg;
  cfg.mtp_dir = std::string(kModelDir) + "/mtp";
  cfg.max_len = kMaxLen;
  cfg.max_prefill = kChunk;
  cfg.max_seq = 1;
  Status(q4t::mtp::LoadMtp(cfg, head.model.embed_tokens, head.model.lm_head,
                           &owner.model, nullptr),
         "actual MTP weights");
  auto& model = owner.model;
  Cuda(cudaStreamSynchronize(nullptr), "weight-load completion");
  std::vector<int32_t> ids(kLargest);
  std::vector<int> positions(kLargest);
  for (int i = 0; i < kLargest; ++i) {
    ids[i] = 97 + (131 * i) % (kVocab - 100);
    positions[i] = i;
  }
  const auto original_ids = ids;
  const auto original_positions = positions;
  std::vector<uint16_t> values(static_cast<size_t>(kLargest) * kHs * kHc);
  uint32_t state = 0x71327u;
  for (auto& value : values) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    const int magnitude = 1 + static_cast<int>(state % 127);
    const float number =
        static_cast<float>((state & 0x80000000u) ? -magnitude : magnitude) /
        256.0f;
    const uint32_t bits = std::bit_cast<uint32_t>(number);
    if ((bits & 0xffffu) != 0)
      throw std::runtime_error("exact BF16 synthetic input");
    value = static_cast<uint16_t>(bits >> 16);
  }
  Guarded<uint16_t> hidden(values.size());
  hidden.Set(values);
  std::printf(
      "MTP_INIT_TAIL_SCOPE C=8192 max_len=208896 S1=1 slot=0 "
      "synthetic_hidden=1 actual_mtp_weights=1 last_row_head=1 "
      "same_candidate_full_control=1 performance_evidence=0\n");
  for (int rows : {8192, 8193, 8196, 16384, 16385}) {
    const Observation full =
        Manual(model, ids, positions, hidden.data(), rows, false);
    const Observation skip =
        Manual(model, ids, positions, hidden.data(), rows, true);
    Exact(full.sample, skip.sample, "last-chunk sample hidden");
    Exact(full.multi, skip.multi, "last-chunk multi hidden");
    Exact(full.logits, skip.logits, "last-chunk compact logits");
    ExactCache(full.cache, skip.cache);
    Require(Argmax(full.logits) == Argmax(skip.logits), "A/B final seed");
    Wrapper(model, ids, positions, hidden.data(), rows, full);
    Exact(hidden.Check(), values, "borrowed main trunk unchanged");
    Exact(ids, original_ids, "host input ids unchanged");
    Exact(positions, original_positions, "host positions unchanged");
    std::printf(
        "MTP_INIT_TAIL_CASE T=%d chunks=%d manual_full=1 "
        "manual_skip=1 wrapper_skip=1 exact=1 finite=1 guards=1\n",
        rows, (rows + kChunk - 1) / kChunk);
    std::fflush(stdout);
  }
  InvalidCalls(model);
  std::printf(
      "MTP_INIT_TAIL_SUMMARY cases=5 forwards=30 "
      "invalid_cases=6 passed=1\n");
  return true;
}
