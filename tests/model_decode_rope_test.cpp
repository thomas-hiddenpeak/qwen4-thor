// The scheduler's packed decode must publish each slot's current MRoPE rows.
// Run the actual four-layer forward (including full attention), then inspect
// the device table. This is a metadata regression, not a logits equivalence
// or multimodal-quality test. No MTP weights or test runtime hook is needed.
#include "q4t/model/model_owner.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void Require(const q4t::Status& status, const char* operation) {
  if (!status.ok()) {
    throw std::runtime_error(std::string(operation) + ": " + status.message());
  }
}

void RequireCuda(cudaError_t error, const char* operation) {
  if (error != cudaSuccess) {
    throw std::runtime_error(std::string(operation) + ": " +
                             cudaGetErrorString(error));
  }
}

class LogitsBuffer {
 public:
  explicit LogitsBuffer(size_t count) {
    RequireCuda(cudaMalloc(reinterpret_cast<void**>(&data_),
                           count * sizeof(uint16_t)),
                "allocate logits observation");
  }
  ~LogitsBuffer() {
    const cudaError_t error = cudaFree(data_);
    if (error != cudaSuccess) {
      std::fprintf(stderr, "logits cleanup: %s\n", cudaGetErrorString(error));
      std::abort();
    }
  }
  LogitsBuffer(const LogitsBuffer&) = delete;
  LogitsBuffer& operator=(const LogitsBuffer&) = delete;
  uint16_t* data() const { return data_; }

 private:
  uint16_t* data_ = nullptr;
};

std::vector<int> ReadRope(const q4t::model::Model& model) {
  std::vector<int> result(static_cast<size_t>(model.cfg.max_seq) * 3u *
                          model.cfg.max_len);
  RequireCuda(cudaMemcpy(result.data(), model.d_rope_pos,
                         result.size() * sizeof(int), cudaMemcpyDeviceToHost),
              "observe pooled MRoPE table");
  return result;
}

}  // namespace

Q4T_TEST(model_decode_batch_multi_rope_position) {
  int devices = 0;
  RequireCuda(cudaGetDeviceCount(&devices), "find CUDA device");
  Q4T_CHECK(devices > 0);

  q4t::model::ModelConfig cfg;
  cfg.model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.num_layers = 4;
  cfg.max_len = 32;
  cfg.max_prefill = 16;
  cfg.max_seq = 2;
  cfg.ple_capacity_tokens = 16;
  q4t::model::ModelOwner owner;
  Require(owner.Load(cfg, nullptr), "load four-layer model");
  auto& model = owner.Get();
  Q4T_CHECK(model.layers.size() == 4);
  Q4T_CHECK(model.layers[3].is_full_attention);
  LogitsBuffer logits(static_cast<size_t>(cfg.vocab));

  // Slot 0 is never executed. Its nonuniform guard detects writes using the
  // packed row number instead of seq_id. Prefill overwrites all of slot 1's
  // table with valid prompt coordinates and zero future entries.
  std::vector<int> guard(static_cast<size_t>(cfg.max_seq) * 3u * cfg.max_len);
  for (size_t i = 0; i < guard.size(); ++i) {
    guard[i] = 1000 + static_cast<int>(i);
  }
  RequireCuda(cudaMemcpy(model.d_rope_pos, guard.data(),
                         guard.size() * sizeof(int), cudaMemcpyHostToDevice),
              "initialize MRoPE guards");

  constexpr int kSlot = 1;
  const int32_t prompt[] = {846, 25, 1203, 321, 44, 1024, 55,
                            9001, 700, 42, 11, 22, 33};
  constexpr int kPromptLength = 13;
  q4t::model::ModelSequence seq;
  Require(q4t::model::ModelBeginSequence(model, &seq, nullptr, kSlot),
          "begin slot 1");
  Require(q4t::model::ModelPrefill(
              model, &seq, prompt, kPromptLength, logits.data(), nullptr,
              nullptr, nullptr, kSlot, q4t::model::LogitsRows::kLastRow),
          "actual thirteen-token prefill");
  Q4T_CHECK(seq.position == kPromptLength);
  Q4T_CHECK(model.rope_delta[kSlot] == 0);

  bool passed = true;
  const int32_t decode_tokens[] = {42, 57};
  const int deltas[] = {0, 5};
  for (int step = 0; step < 2; ++step) {
    const int position = seq.position;
    // A nonzero delta directly exercises the documented incremental table
    // formula. It does not create or claim a real multimodal prompt.
    model.rope_delta[kSlot] = deltas[step];
    const std::vector<int> before = ReadRope(model);
    std::vector<int> expected = before;
    for (int row = 0; row < 3; ++row) {
      expected[(static_cast<size_t>(kSlot) * 3u + row) * cfg.max_len +
               position] = position + deltas[step];
    }

    const int history_width = model.ple_hash.ngram_size - 1;
    Q4T_CHECK(history_width > 0);
    Q4T_CHECK(seq.history.size() >= static_cast<size_t>(history_width));
    const auto begin = seq.history.end() - history_width;
    const std::vector<int32_t> history(begin, seq.history.end());
    const int32_t token = decode_tokens[step];
    Require(seq.Submit({&token, 1},
                       q4t::model::ModelSequence::Stage::kDecode, cfg.max_len),
            "submit packed decode state");
    const q4t::Status submitted = q4t::model::ModelDecodeBatchMulti(
        model, &token, &position, &kSlot, history.data(), 1, logits.data(),
        nullptr);
    Require(q4t::model::ModelCompleteSequence(&seq, nullptr, submitted),
            "actual packed decode and checked GPU completion");
    Q4T_CHECK(seq.position == position + 1);

    const std::vector<int> after = ReadRope(model);
    size_t target_mismatches = 0;
    size_t other_writes = 0;
    for (size_t i = 0; i < after.size(); ++i) {
      const size_t slot = i / (3u * cfg.max_len);
      const int local_position = static_cast<int>(i % cfg.max_len);
      if (slot == kSlot && local_position == position) {
        target_mismatches += after[i] != expected[i];
      } else {
        other_writes += after[i] != before[i];
      }
    }
    std::vector<uint16_t> observed_logits(static_cast<size_t>(cfg.vocab));
    RequireCuda(cudaMemcpy(observed_logits.data(), logits.data(),
                           observed_logits.size() * sizeof(uint16_t),
                           cudaMemcpyDeviceToHost),
                "observe real forward logits");
    size_t nonfinite = 0;
    for (const uint16_t value : observed_logits) {
      nonfinite += (value & 0x7f80u) == 0x7f80u;
    }
    const size_t base = static_cast<size_t>(kSlot) * 3u * cfg.max_len;
    std::printf("  position=%d delta=%d rope=[%d,%d,%d] expected=%d "
                "target_mismatches=%zu other_writes=%zu nonfinite_logits=%zu\n",
                position, deltas[step], after[base + position],
                after[base + cfg.max_len + position],
                after[base + 2u * cfg.max_len + position],
                position + deltas[step], target_mismatches, other_writes,
                nonfinite);
    passed &= target_mismatches == 0 && other_writes == 0 && nonfinite == 0;
  }
  q4t::model::ModelEndSequence(&seq);
  return passed;
}
