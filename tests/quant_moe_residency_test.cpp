// Tests for per-layer tiered expert residency (moe_residency.h), against the
// real checkpoint. Verifies:
//   1. Init/Free lifecycle and slot-mode layout dimensions.
//   2. NUMERICAL CONTRACT: loading expert e into a slot produces device bytes
//      bit-identical to the direct per-expert reference (same reads, same
//      gate/up scale merge, same swizzle, same scalar scales) that
//      LoadMoEWeights uses, so resident and on-demand experts match the
//      all-experts-resident path bit-for-bit.
//   3. Resolve hit/miss/eviction semantics: hits are resident, misses load,
//      LRU victim selection never evicts an expert still needed by the call,
//      and slot_of maps each needed expert to the slot that holds it.
//   4. InitHot deduplicates and caps at C distinct experts.
//   5. Invalid inputs are rejected.
// Reported as SKIP when CUDA or the real model is absent.
#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_residency.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/quant/swizzle.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::quant::MoEResidency;
using q4t::quant::MoEWeightLayout;
using q4t::quant::SfBufferSize;
using q4t::quant::SwizzleSfInto;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

const int kE = 512;
const int kHs = 2560;
const int kMoeIs = 640;
const int kLayer = 2;  // a real MoE layer

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}

bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

struct Ctx {
  WeightIndex* idx = nullptr;
  WeightLoader* loader = nullptr;
};

bool OpenCtx(Ctx* c) {
  if (!FileExists(kIndex)) return false;
  if (!WeightIndex::Open(kIndex, &c->idx).ok()) return false;
  if (!WeightLoader::Create(kModelDir, *c->idx, 16, &c->loader).ok())
    return false;
  return true;
}

std::string Name(int e, const char* proj, const char* suf) {
  return "model.language_model.layers." + std::to_string(kLayer) +
         ".mlp.experts." + std::to_string(e) + "." + proj + "." + suf;
}

// Build the host reference bytes for one expert exactly as LoadMoEWeights
// does (reads + gate/up scale merge + swizzle + scalar scales), then compare
// against the device slot bytes. Returns true when bit-identical.
bool SlotMatchesReference(const Ctx& c, MoEResidency* res, int expert,
                          int slot) {
  const int hs = kHs, moe_is = kMoeIs;
  const size_t gu_w_bytes = static_cast<size_t>(moe_is) * (hs / 2);
  const size_t gu_s_bytes = static_cast<size_t>(moe_is) * (hs / 16);
  const size_t dn_w_bytes = static_cast<size_t>(hs) * (moe_is / 2);
  const size_t dn_s_bytes = static_cast<size_t>(hs) * (moe_is / 16);
  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);

  std::vector<uint8_t> gu_w(gu_w_bytes), up_w(gu_w_bytes), dn_w(dn_w_bytes);
  std::vector<uint8_t> gate_s(gu_s_bytes), up_s(gu_s_bytes), dn_s(dn_s_bytes);
  float scal[4];
  if (!c.loader->ReadTensor(Name(expert, "gate_proj", "weight"),
                            gu_w.data()).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "up_proj", "weight"),
                            up_w.data()).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "down_proj", "weight"),
                            dn_w.data()).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "gate_proj", "weight_scale"),
                            gate_s.data()).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "up_proj", "weight_scale"),
                            up_s.data()).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "down_proj", "weight_scale"),
                            dn_s.data()).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "gate_proj", "weight_scale_2"),
                            &scal[0]).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "gate_proj", "input_scale"),
                            &scal[1]).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "down_proj", "weight_scale_2"),
                            &scal[2]).ok())
    return false;
  if (!c.loader->ReadTensor(Name(expert, "down_proj", "input_scale"),
                            &scal[3]).ok())
    return false;

  std::vector<uint8_t> gu_s_merged(2 * gu_s_bytes);
  std::memcpy(gu_s_merged.data(), gate_s.data(), gu_s_bytes);
  std::memcpy(gu_s_merged.data() + gu_s_bytes, up_s.data(), gu_s_bytes);
  std::vector<uint8_t> gu_sw(gu_sf_block), dn_sw(dn_sf_block);
  SwizzleSfInto(gu_s_merged.data(), 2 * moe_is, hs, gu_sw.data());
  SwizzleSfInto(dn_s.data(), hs, moe_is, dn_sw.data());

  const MoEWeightLayout& L = res->Layout();
  auto cmp = [&](const uint8_t* dev, const uint8_t* host, size_t bytes,
                 const char* what) -> bool {
    std::vector<uint8_t> dev_host(bytes);
    if (cudaMemcpy(dev_host.data(), dev, bytes, cudaMemcpyDeviceToHost) !=
        cudaSuccess) {
      std::printf("  D2H failed: %s\n", what);
      return false;
    }
    if (std::memcmp(dev_host.data(), host, bytes) != 0) {
      std::printf("  bytes mismatch: %s (expert %d slot %d)\n", what, expert,
                  slot);
      return false;
    }
    return true;
  };
  const uint8_t* gu_dst =
      static_cast<const uint8_t*>(L.gu_packed) +
      static_cast<size_t>(slot) * moe_is * hs;
  if (!cmp(gu_dst, gu_w.data(), gu_w_bytes, "gate")) return false;
  if (!cmp(gu_dst + moe_is * (hs / 2), up_w.data(), gu_w_bytes, "up"))
    return false;
  const uint8_t* dn_dst = static_cast<const uint8_t*>(L.dn_packed) +
                          static_cast<size_t>(slot) * hs * (moe_is / 2);
  if (!cmp(dn_dst, dn_w.data(), dn_w_bytes, "down")) return false;
  if (!cmp(static_cast<const uint8_t*>(L.gu_sf) +
               static_cast<size_t>(slot) * gu_sf_block,
           gu_sw.data(), gu_sf_block, "gu_sf"))
    return false;
  if (!cmp(static_cast<const uint8_t*>(L.dn_sf) +
               static_cast<size_t>(slot) * dn_sf_block,
           dn_sw.data(), dn_sf_block, "dn_sf"))
    return false;
  float dev_scal[4];
  if (cudaMemcpy(dev_scal, L.gu_w_scale2 + slot, sizeof(float),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return false;
  if (cudaMemcpy(dev_scal + 1, L.gu_input_scale + slot, sizeof(float),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return false;
  if (cudaMemcpy(dev_scal + 2, L.dn_w_scale2 + slot, sizeof(float),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return false;
  if (cudaMemcpy(dev_scal + 3, L.dn_input_scale + slot, sizeof(float),
                 cudaMemcpyDeviceToHost) != cudaSuccess)
    return false;
  for (int i = 0; i < 4; ++i) {
    if (dev_scal[i] != scal[i]) {
      std::printf("  scalar mismatch idx %d (expert %d slot %d)\n", i, expert,
                  slot);
      return false;
    }
  }
  // Host scale vectors must match too (the GEMM wrappers read them).
  if (L.gu_w_scale2_h[slot] != scal[0] || L.gu_input_scale_h[slot] != scal[1] ||
      L.dn_w_scale2_h[slot] != scal[2] || L.dn_input_scale_h[slot] != scal[3]) {
    std::printf("  host scale vector mismatch (expert %d slot %d)\n", expert,
                slot);
    return false;
  }
  return true;
}

}  // namespace

Q4T_TEST(residency_init_free_lifecycle) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    Q4T_SKIP("(skipped: real model not present)");
  }
  const int C = 8;
  MoEResidency res;
  Status s = res.Init(*c.loader, kLayer, kE, kHs, kMoeIs, C, 0);
  if (!s.ok()) {
    std::printf("  init failed: %s\n", s.message().c_str());
    delete c.loader;
    delete c.idx;
    return false;
  }
  const MoEWeightLayout& L = res.Layout();
  if (L.E != C || !L.slot_mode || L.hs != kHs || L.moe_is != kMoeIs) {
    std::printf("  layout dims wrong: E=%d slot_mode=%d\n", L.E,
                L.slot_mode);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  if (res.Slots() != C || res.Experts() != kE || res.ResidentCount() != 0) {
    std::printf("  slot/expert/resident counts wrong\n");
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  // Expected device bytes: 5 buffers sized by C slots.
  const size_t gu_sf_block = SfBufferSize(2 * kMoeIs, kHs);
  const size_t dn_sf_block = SfBufferSize(kHs, kMoeIs);
  const size_t expected =
      static_cast<size_t>(2 * C * kMoeIs) * (kHs / 2) +
      static_cast<size_t>(C) * gu_sf_block +
      static_cast<size_t>(C * kHs) * (kMoeIs / 2) +
      static_cast<size_t>(C) * dn_sf_block + 4 * static_cast<size_t>(C) *
          sizeof(float);
  if (res.DeviceBytes() != expected) {
    std::printf("  device bytes %zu != expected %zu\n", res.DeviceBytes(),
                expected);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  res.Free();
  res.Free();  // idempotent
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(residency_load_matches_reference) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    Q4T_SKIP("(skipped: real model not present)");
  }
  const int C = 4;
  MoEResidency res;
  if (!res.Init(*c.loader, kLayer, kE, kHs, kMoeIs, C, 0).ok()) {
    delete c.loader;
    delete c.idx;
    return false;
  }
  // Load three distinct experts into distinct slots via the hot list, then
  // verify each slot is bit-identical to the direct per-expert reference.
  const std::vector<int> experts = {0, 128, 511};
  if (!res.InitHot(experts, 0).ok()) {
    std::printf("  init hot failed\n");
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  cudaStreamSynchronize(0);
  for (int slot = 0; slot < 3; ++slot) {
    if (!SlotMatchesReference(c, &res, experts[slot], slot)) {
      res.Free();
      delete c.loader;
      delete c.idx;
      return false;
    }
  }
  const MoEResidency::Stats st = res.GetStats();
  if (st.loads != 3 || st.misses != 0 || st.evictions != 0) {
    std::printf("  stats wrong: loads=%llu misses=%llu evictions=%llu\n",
                (unsigned long long)st.loads, (unsigned long long)st.misses,
                (unsigned long long)st.evictions);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  res.Free();
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(residency_resolve_hit_miss_evict) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    Q4T_SKIP("(skipped: real model not present)");
  }
  const int C = 4;
  MoEResidency res;
  if (!res.Init(*c.loader, kLayer, kE, kHs, kMoeIs, C, 0).ok()) {
    delete c.loader;
    delete c.idx;
    return false;
  }
  auto resolve = [&](const std::vector<int>& needed,
                     std::vector<int>* slot_of) -> bool {
    std::vector<int32_t> in(needed.begin(), needed.end());
    slot_of->assign(needed.size(), -1);
    bool loaded = false;
    Status s = res.Resolve(in.data(), static_cast<int>(in.size()),
                           slot_of->data(), 0, &loaded);
    if (!s.ok()) {
      std::printf("  resolve failed: %s\n", s.message().c_str());
      return false;
    }
    return true;
  };
  auto fail = [&](const char* msg) {
    std::printf("  %s\n", msg);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  };

  // Step A: fill all slots with 0..3 (all misses into empty slots).
  {
    std::vector<int> so;
    if (!resolve({0, 1, 2, 3}, &so)) return fail("fill resolve failed");
    for (int i = 0; i < 4; ++i)
      if (so[i] != i) return fail("fill slot_of wrong");
  }
  // Step B: touch experts 0..2 (hits); expert 3 keeps its older tick and is
  // now the least recently used.
  {
    std::vector<int> so;
    if (!resolve({0, 1, 2}, &so)) return fail("touch resolve failed");
    for (int i = 0; i < 3; ++i)
      if (so[i] != i) return fail("touch slot_of wrong");
  }
  // Step C: resolve {3,4,5,6}. Expert 3 is needed and is the LRU, but it must
  // keep its slot; the misses evict the non-needed experts 0,1,2 (equal
  // ticks, lowest slot index first).
  {
    std::vector<int> so;
    if (!resolve({3, 4, 5, 6}, &so)) return fail("evict resolve failed");
    // needed[0]=3 -> slot 3 (kept); needed[1]=4 -> slot 0 (evicted 0);
    // needed[2]=5 -> slot 1 (evicted 1); needed[3]=6 -> slot 2 (evicted 2).
    const int expect[4] = {3, 0, 1, 2};
    for (int i = 0; i < 4; ++i)
      if (so[i] != expect[i]) return fail("evict slot_of wrong");
  }
  // Step D: expert 0 was evicted; resolving it misses and evicts the LRU
  // non-needed expert (all remaining ticks equal -> lowest slot index).
  {
    std::vector<int> so;
    if (!resolve({0}, &so)) return fail("reload resolve failed");
    if (so[0] != 0) return fail("reload slot_of wrong");
  }
  cudaStreamSynchronize(0);
  const MoEResidency::Stats st = res.GetStats();
  // loads: 4 (fill) + 3 (step C) + 1 (step D) = 8; evictions (occupied
  // overwrites): 3 (step C) + 1 (step D) = 4; misses (loads incl. fill):
  // 4 + 3 + 1 = 8; hits: 3 (step B) + 1 (step C) = 4.
  if (st.loads != 8 || st.evictions != 4 || st.misses != 8 || st.hits != 4) {
    std::printf("  stats wrong: loads=%llu misses=%llu evictions=%llu "
                "hits=%llu\n",
                (unsigned long long)st.loads,
                (unsigned long long)st.misses,
                (unsigned long long)st.evictions,
                (unsigned long long)st.hits);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  res.Free();
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(residency_hot_list_dedup_cap) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    Q4T_SKIP("(skipped: real model not present)");
  }
  const int C = 3;
  MoEResidency res;
  if (!res.Init(*c.loader, kLayer, kE, kHs, kMoeIs, C, 0).ok()) {
    delete c.loader;
    delete c.idx;
    return false;
  }
  // Duplicate + out-of-range entries are ignored; at most C distinct load.
  std::vector<int> hot = {10, 10, 20, 9999, 30, 40, 50};
  if (!res.InitHot(hot, 0).ok()) {
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  cudaStreamSynchronize(0);
  if (res.ResidentCount() != C) {
    std::printf("  resident=%d expected %d\n", res.ResidentCount(), C);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  const MoEResidency::Stats st = res.GetStats();
  if (st.loads != C) {
    std::printf("  loads=%llu expected %d\n", (unsigned long long)st.loads,
                C);
    res.Free();
    delete c.loader;
    delete c.idx;
    return false;
  }
  res.Free();
  delete c.loader;
  delete c.idx;
  return true;
}

Q4T_TEST(residency_invalid_inputs) {
  if (!CudaAvailable()) {
    Q4T_SKIP("(skipped: no CUDA device)");
  }
  Ctx c;
  if (!OpenCtx(&c)) {
    Q4T_SKIP("(skipped: real model not present)");
  }
  MoEResidency res;
  if (res.Init(*c.loader, kLayer, kE, kHs, kMoeIs, 0, 0).ok()) {
    std::printf("  C=0 should be rejected\n");
    delete c.loader;
    delete c.idx;
    return false;
  }
  if (res.Init(*c.loader, kLayer, kE, kHs, kMoeIs, kE + 1, 0).ok()) {
    std::printf("  C>E should be rejected\n");
    delete c.loader;
    delete c.idx;
    return false;
  }
  delete c.loader;
  delete c.idx;
  return true;
}

