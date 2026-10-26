// Independent uniform-layout reference for packed long/short attention state.
// Run after the real HTTP gate; no CUDA/model skip is accepted.
#include "q4t/io/weight_loader.h"
#include "q4t/model/decoder_layer.h"
#include "q4t/model/full_attention.h"
#include "q4t/runtime/sequence_capacity.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <vector>

namespace {
void Check(bool ok, const char* label) {
  if (!ok) throw std::runtime_error(label);
}
void Cuda(cudaError_t e) { Check(e == cudaSuccess, cudaGetErrorString(e)); }
template <typename T>
struct Buffer {
  T* p = nullptr;
  explicit Buffer(size_t n) { Cuda(cudaMalloc(&p, n * sizeof(T))); }
  ~Buffer() {
    if (p) cudaFree(p);
  }
  Buffer(const Buffer&) = delete;
  Buffer& operator=(const Buffer&) = delete;
  void Put(const std::vector<T>& v, size_t off = 0) {
    Cuda(cudaMemcpy(p + off, v.data(), v.size() * sizeof(T),
                    cudaMemcpyHostToDevice));
  }
  std::vector<T> Get(size_t n, size_t off = 0) const {
    std::vector<T> v(n);
    Cuda(cudaMemcpy(v.data(), p + off, n * sizeof(T), cudaMemcpyDeviceToHost));
    return v;
  }
};
struct Weights {
  q4t::model::FullAttentionWeights w;
  ~Weights() { w.Free(); }
};
constexpr int kMax = 16384;
const std::vector<int> kCaps{16384, 12288, 10240, 8192};
struct State {
  bool packed;
  Buffer<uint16_t> kv, raw, comp;
  Buffer<int> pages, rope;
  explicit State(bool compact)
      : packed(compact),
        kv(Tokens() * 1024),
        raw(Tokens() * 128),
        comp(Tokens() * 128),
        pages(4u * kMax),
        rope(12u * kMax) {
    std::vector<int> pt(4u * kMax, -1), rp(12u * kMax);
    for (int slot = 0; slot < 4; ++slot) {
      for (int i = 0; i < kMax; ++i) {
        if (i < kCaps[slot])
          pt[size_t(slot) * kMax + i] =
              (packed ? Offset(slot) / 16 : 0) + i / 16;
        for (int axis = 0; axis < 3; ++axis)
          rp[(size_t(slot) * 3 + axis) * kMax + i] = i;
      }
      for (int which = 0; which < 3; ++which) {
        const int width = which ? 128 : 1024;
        std::vector<uint16_t> v(size_t(kCaps[slot]) * width);
        for (size_t i = 0; i < v.size(); ++i)
          v[i] = 0x3c80 + ((i * 11 + slot * 79 + which * 13) % 256);
        (which == 0   ? kv
         : which == 1 ? raw
                      : comp)
            .Put(v, Offset(slot) * width);
      }
    }
    pages.Put(pt);
    rope.Put(rp);
  }
  size_t Offset(int slot) const {
    size_t value = 0;
    for (int i = 0; i < slot; ++i) value += packed ? kCaps[i] : kMax;
    return value;
  }
  size_t Tokens() const { return packed ? 47104 : 4u * kMax; }
  void Equal(const State& other) const {
    for (int slot = 0; slot < 4; ++slot) {
      for (int which = 0; which < 3; ++which) {
        const int width = which ? 128 : 1024;
        const auto& a = which == 0 ? kv : which == 1 ? raw : comp;
        const auto& b = which == 0   ? other.kv
                        : which == 1 ? other.raw
                                     : other.comp;
        Check(
            a.Get(size_t(kCaps[slot]) * width, Offset(slot) * width) ==
                b.Get(size_t(kCaps[slot]) * width, other.Offset(slot) * width),
            "packed/uniform valid cache mismatch");
      }
    }
  }
};
}  // namespace

Q4T_TEST(agent_attention_layout) {
  Cuda(cudaSetDevice(0));
  const std::string dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  q4t::io::WeightIndex* index_raw = nullptr;
  auto status = q4t::io::WeightIndex::Open(
      dir + "/model.safetensors.index.json", &index_raw);
  Check(status.ok(), status.message().c_str());
  std::unique_ptr<q4t::io::WeightIndex> index(index_raw);
  q4t::io::WeightLoader* loader_raw = nullptr;
  status = q4t::io::WeightLoader::Create(dir, *index, 1, &loader_raw);
  Check(status.ok(), status.message().c_str());
  std::unique_ptr<q4t::io::WeightLoader> loader(loader_raw);
  Weights owner;
  status = q4t::model::LoadFullAttention(
      *loader, "model.language_model.layers.3.self_attn", 2560, 24, 2, 256, 64,
      1e7f, 1e-6f, 4, 1, 128, 2048, 4, &owner.w, nullptr);
  Check(status.ok(), status.message().c_str());
  owner.w.max_len = kMax;
  auto uniform = owner.w;  // non-owning reference, shared immutable weights
  status = q4t::model::ConfigureAttentionSlots(&owner.w, kCaps);
  Check(status.ok(), status.message().c_str());
  State packed(true), reference(false);
  Buffer<uint16_t> x(8u * 2560), out(8u * 2560);
  Buffer<int> pos(8), seq(8);
  size_t workspace_bytes = 0;
  for (int rows : {1, 4, 8})
    workspace_bytes =
        std::max(workspace_bytes,
                 q4t::model::FullAttentionWorkspaceBytes(owner.w, rows));
  Buffer<uint8_t> ws(workspace_bytes);
  const auto run = [&](std::vector<int> positions, std::vector<int> slots,
                       int host_slot = -1) {
    const int n = positions.size();
    std::vector<uint16_t> input(size_t(n) * 2560);
    for (size_t i = 0; i < input.size(); ++i)
      input[i] = 0x3e00 + (i * 31 % 128);
    x.Put(input);
    pos.Put(positions);
    seq.Put(slots);
    std::vector<uint16_t> expected;
    for (int compact = 0; compact < 2; ++compact) {
      auto& st = compact ? packed : reference;
      const auto& w = compact ? owner.w : uniform;
      const size_t offset = host_slot >= 0 ? st.Offset(host_slot) : 0;
      status = q4t::model::FullAttentionForward(
          w, x.p, out.p, pos.p,
          st.rope.p + (host_slot >= 0 ? size_t(host_slot) * 3 * kMax : 0),
          st.kv.p + (!compact ? offset * 1024 : 0),
          st.pages.p + (host_slot >= 0 ? size_t(host_slot) * kMax : 0),
          st.raw.p + offset * 128, st.comp.p + offset * 128, n, ws.p,
          workspace_bytes, nullptr, host_slot >= 0 ? nullptr : seq.p,
          *std::max_element(positions.begin(), positions.end()));
      Check(status.ok(), status.message().c_str());
      const auto result = out.Get(size_t(n) * 2560);
      if (!compact)
        expected = result;
      else
        Check(result == expected, "packed/uniform output mismatch");
    }
    packed.Equal(reference);
    std::printf("AGENT_ATTN rows=%d host_slot=%d max_pos=%d exact=1\n", n,
                host_slot,
                *std::max_element(positions.begin(), positions.end()));
  };
  run({12287, 8191, 9001, 4095}, {0, 1, 2, 3});  // uneven long one-pass
  run({4096}, {3});                              // nonzero dedicated decode
  run({10000, 10001, 10002, 10003, 10004, 10005, 10006, 10007},
      {2, 2, 2, 2, 2, 2, 2, 2}, 2);  // host prefill streamed top-k
  run({8191}, {3});                  // last legal short-slot token
  // Decoder reset must zero only its compact slice, including after reuse.
  q4t::model::DecoderLayer layer;
  layer.is_full_attention = true;
  layer.max_len = kMax;
  layer.max_seq = 4;
  layer.seq_capacities = kCaps;
  layer.kv_cache = packed.kv.p;
  layer.idx_raw = packed.raw.p;
  layer.idx_comp = packed.comp.p;
  layer.ResetState(1, nullptr);
  Cuda(cudaDeviceSynchronize());
  for (int which = 0; which < 3; ++which) {
    const int width = which ? 128 : 1024;
    auto& a = which == 0 ? packed.kv : which == 1 ? packed.raw : packed.comp;
    auto& b = which == 0   ? reference.kv
              : which == 1 ? reference.raw
                           : reference.comp;
    for (int slot = 0; slot < 4; ++slot) {
      const auto actual =
          a.Get(size_t(kCaps[slot]) * width, packed.Offset(slot) * width);
      if (slot == 1)
        Check(std::all_of(actual.begin(), actual.end(),
                          [](uint16_t v) { return v == 0; }),
              "slot reset");
      else
        Check(actual == b.Get(size_t(kCaps[slot]) * width,
                              reference.Offset(slot) * width),
              "peer reset isolation");
    }
  }
  return true;
}
