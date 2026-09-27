#include <cuda_runtime.h>

#include <cstdio>
#include <cstdlib>
#include <vector>

#include "q4t/model/model.h"

using namespace q4t::model;
void Require(bool ok, const char* message) {
  if (!ok) {
    std::fprintf(stderr, "%s\n", message);
    std::exit(2);
  }
}
void Check(q4t::Status s) { Require(s.ok(), s.message().c_str()); }
void Cuda(cudaError_t s) { Require(s == cudaSuccess, cudaGetErrorString(s)); }

// Persistent recurrent state of every linear/PLE layer, one slot only.
std::vector<unsigned char> State(const Model& m, int slot) {
  std::vector<unsigned char> result;
  auto append = [&](const void* p, size_t bytes) {
    if (!p) return;
    const size_t at = result.size();
    result.resize(at + bytes);
    Cuda(cudaMemcpy(result.data() + at,
                    static_cast<const char*>(p) + slot * bytes, bytes,
                    cudaMemcpyDeviceToHost));
  };
  for (const auto& l : m.layers) {
    append(l.ssm_state, size_t(48) * 128 * 128 * 4);
    append(l.conv_state, size_t(10240) * 3 * 2);
    append(l.ple_conv_state, size_t(10240) * 9 * 2);
    append(l.kv_cache, size_t((l.max_len + kKvPageSize - 1) / kKvPageSize) *
                           kKvPageSize * 2 * 2 * 256 * 2);
    append(l.idx_raw, size_t(l.max_len) * 128 * 2);
    append(l.idx_comp, size_t(l.max_len) * 128 * 2);
  }
  return result;
}
std::vector<uint16_t> Logits(const Model& m, uint16_t* p) {
  std::vector<uint16_t> out(m.cfg.vocab);
  Cuda(cudaMemcpy(out.data(), p, out.size() * 2, cudaMemcpyDeviceToHost));
  return out;
}
size_t Differences(const auto& a, const auto& b) {
  Require(a.size() == b.size(), "size mismatch");
  size_t count = 0;
  for (size_t i = 0; i < a.size(); ++i) count += a[i] != b[i];
  return count;
}
int main(int argc, char** argv) {
  Require(argc == 2, "model directory required");
  ModelConfig cfg;
  cfg.model_dir = argv[1];
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.max_len = 64;
  cfg.max_prefill = 32;
  cfg.max_seq = 2;
  cfg.ple_capacity_tokens = 32;
  Model m;
  Check(LoadModel(cfg, &m, nullptr));
  uint16_t* logits;
  Cuda(cudaMalloc(&logits, size_t(32) * m.cfg.vocab * 2));
  std::vector<int32_t> prompt(13), other(17);
  for (int i = 0; i < 13; ++i) prompt[i] = 42 + i * 17;
  for (int i = 0; i < 17; ++i) other[i] = 211 + i * 13;
  auto seed_other = [&] {
    ModelSequence seq;
    Check(ModelBeginSequence(m, &seq, nullptr, 0));
    Check(ModelPrefillTextChunk(m, &seq, other.data(), 17, 17, logits, nullptr,
                                nullptr, LogitsRows::kLastRow));
  };
  seed_other();
  const auto sentinel = State(m, 0);
  ModelSequence seq;
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  for (int n : {7, 5, 1})
    Check(ModelPrefillTextChunk(m, &seq, prompt.data(), 13, n, logits, nullptr,
                                nullptr, LogitsRows::kLastRow));
  const auto prefill = State(m, 1);
  seed_other();
  Require(State(m, 1) == prefill, "other-slot prefill changed live state");
  Check(ModelDecodeStepSeq(m, &seq, 97, logits, nullptr, nullptr, 1));
  const auto expected = State(m, 1);
  const auto expected_logits = Logits(m, logits);
  Require(State(m, 0) == sentinel, "explicit slot changed other sequence");
  ModelEndSequence(&seq);
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  Check(ModelPrefillTextChunk(m, &seq, other.data(), 17, 17, logits, nullptr,
                              nullptr, LogitsRows::kLastRow));
  ModelEndSequence(&seq);
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  for (int n : {7, 5, 1})
    Check(ModelPrefillTextChunk(m, &seq, prompt.data(), 13, n, logits, nullptr,
                                nullptr, LogitsRows::kLastRow));
  Require(State(m, 1) == prefill, "same-partition reset/replay differs");
  Check(ModelDecodeStepSeq(m, &seq, 97, logits, nullptr));
  const size_t decode_state = Differences(State(m, 1), expected);
  const size_t decode_logits = Differences(Logits(m, logits), expected_logits);
  const size_t pollution = Differences(State(m, 0), sentinel);
  std::printf(
      "decode default: state_bytes=%zu logits_bf16=%zu other_slot_bytes=%zu\n",
      decode_state, decode_logits, pollution);
  const auto before_reject = State(m, 1);
  const int position = seq.position;
  Require(!ModelDecodeStepSeq(m, &seq, 211, logits, nullptr, nullptr, 0).ok(),
          "explicit decode slot mismatch accepted");
  Require(seq.position == position && State(m, 1) == before_reject &&
              State(m, 0) == sentinel,
          "rejected decode mutated state");
  seed_other();
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  const auto reset_state = State(m, 1);
  for (int slot : {-1, 2})
    Require(!ModelBeginSequence(m, &seq, nullptr, slot).ok(),
            "invalid begin slot accepted");
  Require(!ModelPrefill(m, &seq, prompt.data(), 13, logits, nullptr, nullptr,
                        nullptr, 0)
               .ok(),
          "explicit prefill mismatch accepted");
  Require(seq.seq_id == 1 && seq.position == 0 && seq.history.empty() &&
              seq.stage == ModelSequence::Stage::kPrefill &&
              State(m, 1) == reset_state && State(m, 0) == sentinel,
          "rejected begin/prefill mutated state");
  Check(ModelPrefill(m, &seq, prompt.data(), 13, logits, nullptr, nullptr,
                     nullptr, 1, LogitsRows::kAllRows));
  const auto expected_prefill = State(m, 1);
  const auto expected_prefill_logits = Logits(m, logits + 12 * m.cfg.vocab);
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  Check(ModelPrefill(m, &seq, prompt.data(), 13, logits, nullptr));
  const size_t prefill_state = Differences(State(m, 1), expected_prefill);
  const size_t prefill_logits = Differences(
      Logits(m, logits + 12 * m.cfg.vocab), expected_prefill_logits);
  const size_t prefill_pollution = Differences(State(m, 0), sentinel);
  std::printf("prefill default: state_bytes=%zu other_slot_bytes=%zu\n",
              prefill_state, prefill_pollution);
  std::printf("prefill logits_bf16=%zu guard_checks=4 interleave/ABA=passed\n",
              prefill_logits);
  Cuda(cudaFree(logits));
  m.Free();
  return decode_state || decode_logits || pollution || prefill_logits ||
                 prefill_state || prefill_pollution
             ? 1
             : 0;
}
