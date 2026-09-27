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
std::vector<int> Rope(const Model& m, int slot) {
  std::vector<int> out(3 * m.cfg.max_len);
  Cuda(cudaMemcpy(out.data(), m.d_rope_pos + slot * out.size(),
                  out.size() * sizeof(int), cudaMemcpyDeviceToHost));
  return out;
}
void Boundary(const Model& m, int slot, int base, int count) {
  Cuda(cudaDeviceSynchronize());
  std::vector<int> positions(count);
  Cuda(cudaMemcpy(positions.data(), m.d_positions, count * sizeof(int),
                  cudaMemcpyDeviceToHost));
  for (int i = 0; i < count; ++i)
    Require(positions[i] == base + i, "absolute position mismatch");
  const auto rope = Rope(m, slot);
  Require(m.rope_delta[slot] == 0, "text rope delta not reset");
  for (int r = 0; r < 3; ++r)
    for (int i = 0; i < base + count; ++i)
      Require(rope[r * m.cfg.max_len + i] == i, "valid rope mismatch");
  auto zero_tail = [&](const uint16_t* p, int rows, int width, int stride) {
    if (!p) return;
    const int tail = stride - rows * width;
    std::vector<uint16_t> data(tail);
    Cuda(cudaMemcpy(data.data(), p + slot * stride + rows * width,
                    data.size() * 2, cudaMemcpyDeviceToHost));
    for (auto x : data) Require(x == 0, "write outside valid cache rows");
  };
  for (const auto& l : m.layers) {
    const int end = base + count;
    const int padded = (l.max_len + kKvPageSize - 1) / kKvPageSize *
                       kKvPageSize;
    zero_tail(l.kv_cache, end, 2 * 2 * 256, padded * 2 * 2 * 256);
    zero_tail(l.idx_raw, end, 128, l.max_len * 128);
    zero_tail(l.idx_comp, end / 4, 128, l.max_len * 128);
    if (l.page_table) {
      std::vector<int> pages(l.max_len);
      Cuda(cudaMemcpy(pages.data(), l.page_table + slot * l.max_len,
                      pages.size() * sizeof(int), cudaMemcpyDeviceToHost));
      for (int i = 0; i < l.max_len; ++i)
        Require(pages[i] == i / kKvPageSize, "page mapping changed");
    }
  }
}
int main(int argc, char** argv) {
  Require(argc == 2, "model directory required");
  ModelConfig cfg;
  cfg.model_dir = argv[1];
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.max_len = 272;
  cfg.max_prefill = 128;
  cfg.max_seq = 2;
  cfg.ple_capacity_tokens = 128;
  Model m;
  Check(LoadModel(cfg, &m, nullptr));
  uint16_t* logits;
  Cuda(cudaMalloc(&logits, size_t(m.cfg.vocab) * 2));
  std::vector<int32_t> prompt(257), other(17);
  for (int i = 0; i < 257; ++i) prompt[i] = 42 + i * 17;
  for (int i = 0; i < 17; ++i) other[i] = 211 + i * 13;
  auto interfere = [&] {
    ModelSequence seq;
    Check(ModelBeginSequence(m, &seq, nullptr, 0));
    Check(ModelPrefillTextChunk(m, &seq, other.data(), 17, 17, logits,
                                nullptr, nullptr, LogitsRows::kLastRow));
  };
  interfere();
  const auto sentinel = State(m, 0);
  const auto sentinel_rope = Rope(m, 0);
  std::vector<std::vector<unsigned char>> states;
  std::vector<std::vector<uint16_t>> outputs;
  ModelSequence seq;
  // Independent host bookkeeping, identical underlying arithmetic calls.
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  m.rope_delta[1] = 99;  // Fresh text prefill must replace stale text/MM delta.
  int base = 0;
  for (int count : {127, 1, 128, 1}) {
    if (base == 0) {
      Check(ModelPrefill(m, &seq, prompt.data(), count, logits, nullptr,
                         nullptr, nullptr, 1, LogitsRows::kLastRow));
    } else {
      Check(ModelDecodeBatch(m, prompt.data() + base, count, base,
                             prompt.data(), base, logits, nullptr, nullptr,
                             false, 1, LogitsRows::kLastRow));
    }
    Boundary(m, 1, base, count);
    states.push_back(State(m, 1));
    outputs.push_back(Logits(m, logits));
    base += count;
  }
  Check(ModelDecodeStep(m, 97, base, prompt.data(), logits, nullptr, nullptr,
                        1));
  Boundary(m, 1, base, 1);
  states.push_back(State(m, 1));
  outputs.push_back(Logits(m, logits));
  Check(ModelBeginSequence(m, &seq, nullptr, 1));
  m.rope_delta[1] = 77;
  base = 0;
  int index = 0;
  for (int count : {127, 1, 128, 1}) {
    Check(ModelPrefillTextChunk(m, &seq, prompt.data(), 257, count, logits,
                                nullptr, nullptr, LogitsRows::kLastRow));
    Boundary(m, 1, base, count);
    base += count;
    Require(seq.position == base && seq.seq_id == 1 &&
                seq.history == std::vector<int32_t>(prompt.begin(),
                                                    prompt.begin() + base),
            "chunk host handoff mismatch");
    Require(seq.stage == (base == 257 ? ModelSequence::Stage::kDecode
                                     : ModelSequence::Stage::kPrefill),
            "chunk stage mismatch");
    Require(State(m, 1) == states[index] &&
                Logits(m, logits) == outputs[index], "chunk replay mismatch");
    Require(State(m, 0) == sentinel && Rope(m, 0) == sentinel_rope,
            "chunk changed other slot");
    const auto current_rope = Rope(m, 1);
    interfere();
    Require(State(m, 1) == states[index] && Rope(m, 1) == current_rope,
            "interference changed active slot");
    std::printf("boundary=%d state/logits/history/rope/cache-tail=passed\n",
                base);
    ++index;
  }
  Check(ModelDecodeStepSeq(m, &seq, 97, logits, nullptr));
  Boundary(m, 1, 257, 1);
  prompt.push_back(97);
  Require(seq.position == 258 && seq.history == prompt &&
              State(m, 1) == states.back() &&
              Logits(m, logits) == outputs.back(), "decode handoff mismatch");
  Require(State(m, 0) == sentinel && Rope(m, 0) == sentinel_rope,
          "decode changed other slot");
  std::puts("decode boundary=258 passed; identical partition only");
  Cuda(cudaFree(logits));
  m.Free();
  return 0;
}
