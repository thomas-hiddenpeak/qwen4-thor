// Standalone CUDA ownership contract. Link with --wrap=cudaMalloc,
// --wrap=cudaFree and --wrap=cudaStreamSynchronize. No model generation.
#include "q4t/mtp/mtp.h"

#include <cuda_runtime.h>

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <unordered_set>

namespace {

std::unordered_set<void*> live;
std::unordered_set<void*> borrowed;
int allocations = 0;
int fail_at = 0;
int syncs = 0;
bool require_drain = false;

void Check(bool ok, const char* message) {
  if (!ok) {
    std::fprintf(stderr, "FAIL: %s\n", message);
    std::exit(1);
  }
}

void CheckCuda(cudaError_t error, const char* operation) {
  if (error != cudaSuccess) {
    std::fprintf(stderr, "%s: %s\n", operation, cudaGetErrorString(error));
    std::exit(1);
  }
}

void ResetCounters(int failure = 0) {
  allocations = 0;
  fail_at = failure;
  syncs = 0;
}

void CheckEmpty(const q4t::mtp::MtpModel& m) {
  Check(live == borrowed, "MTP allocations survived cleanup");
  Check(!m.fc_embedding && !m.fc_hidden && !m.pre_fc_norm_embedding &&
            !m.pre_fc_norm_hidden && !m.embed_tokens && !m.lm_head &&
            !m.attn_hc.hc_norm && !m.mlp_hc.hc_norm && !m.full_attn.q_proj &&
            !m.moe.gu && !m.moe.down && !m.moe_extra.gate && !m.mixer.hc_norm &&
            !m.kv_cache && !m.page_table && !m.idx_raw && !m.idx_comp &&
            !m.d_rope_pos && !m.d_ws && !m.d_sample && !m.d_trunk &&
            !m.d_logits && !m.d_ids_scratch && !m.d_ms_drafts && m.k_max == 0 &&
            m.ws_bytes == 0 && m.kv_bytes == 0 && m.idx_bytes == 0,
        "MTP cleanup left stale ownership metadata");
}

}  // namespace

extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);

extern "C" cudaError_t __wrap_cudaMalloc(void** pointer, size_t bytes) {
  ++allocations;
  if (fail_at > 0 && allocations == fail_at) return cudaErrorMemoryAllocation;
  const cudaError_t error = __real_cudaMalloc(pointer, bytes);
  if (error == cudaSuccess)
    Check(live.insert(*pointer).second, "duplicate live allocation");
  return error;
}

extern "C" cudaError_t __wrap_cudaFree(void* pointer) {
  if (pointer) {
    Check(!borrowed.contains(pointer), "MTP freed borrowed embedding/head");
    Check(!require_drain || syncs > 0,
          "partial load freed before stream drain");
    Check(live.erase(pointer) == 1, "free without ownership or double free");
  }
  const cudaError_t error = __real_cudaFree(pointer);
  CheckCuda(error, "cudaFree");
  return error;
}

extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t stream) {
  ++syncs;
  return __real_cudaStreamSynchronize(stream);
}

int main(int argc, char** argv) {
  Check(argc == 3,
        "requires new fixture directory and read-only model directory");
  CheckCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  for (const char* name : {"Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL"})
    Check(std::getenv(name) == nullptr, "experimental FP8 environment is set");

  uint16_t* embed = nullptr;
  uint16_t* head = nullptr;
  CheckCuda(cudaMalloc(reinterpret_cast<void**>(&embed), 2), "borrowed embed");
  CheckCuda(cudaMalloc(reinterpret_cast<void**>(&head), 2), "borrowed head");
  borrowed = live;
  q4t::mtp::MtpModel m;
  m.Free();
  CheckEmpty(m);

  // Missing metadata reaches the first four real allocations, then fails at
  // ReadTensor. The fixture is generated outside the read-only model tree.
  const std::filesystem::path fixture(argv[1]);
  Check(std::filesystem::create_directory(fixture), "fixture must be new");
  {
    std::ofstream index(fixture / "model.safetensors.index.json");
    index << "{\"weight_map\":{}}\n";
    index.close();
    Check(index.good(), "write fixture index");
  }
  q4t::mtp::MtpConfig tiny;
  tiny.mtp_dir = fixture.string();
  tiny.hs = tiny.hc = tiny.vocab = 1;
  tiny.max_prefill = tiny.max_len = 1;
  cudaStream_t stream = nullptr;
  CheckCuda(cudaStreamCreate(&stream), "stream create");
  require_drain = true;
  for (cudaStream_t selected : {cudaStream_t(nullptr), stream}) {
    for (int failure : {0, 1, 2, 3, 4}) {
      ResetCounters(failure);
      Check(!q4t::mtp::LoadMtp(tiny, embed, head, &m, selected).ok(),
            "partial load unexpectedly succeeded");
      Check(syncs > 0, "failed load did not drain stream");
      CheckEmpty(m);
      m.Free();
      CheckEmpty(m);
    }
  }
  require_drain = false;
  fail_at = 0;
  CheckCuda(cudaStreamDestroy(stream), "stream destroy");
  std::puts(
      "partial load: missing tensor, allocation failures 1..4, retry, "
      "both streams, borrowed ownership: PASS");

  // Scratch uses small dimensions, so every allocator boundary is tested
  // without exhausting physical memory or loading a model repeatedly.
  m.cfg = tiny;
  m.embed_tokens = embed;
  m.lm_head = head;
  CheckCuda(cudaMalloc(reinterpret_cast<void**>(&m.fc_embedding), 2),
            "owned marker weight");
  const std::unordered_set<void*> weights = live;
  ResetCounters();
  Check(q4t::mtp::MtpReserveScratch(m, 2).ok(), "initial scratch reserve");
  const int scratch_allocations = allocations;
  Check(scratch_allocations > 1 && m.d_ms_drafts, "multi scratch missing");
  const size_t expected_live = live.size();
  ResetCounters();
  Check(q4t::mtp::MtpReserveScratch(m, 2).ok() && allocations == 0,
        "idempotent reserve allocated");
  Check(!q4t::mtp::LoadMtp(tiny, embed, head, &m, nullptr).ok() &&
            live.size() == expected_live,
        "live reload must reject without losing resources");
  for (int failure = 1; failure <= scratch_allocations; ++failure) {
    ResetCounters(failure);
    Check(!q4t::mtp::MtpReserveScratch(m, 3).ok(),
          "injected scratch grow unexpectedly succeeded");
    Check(m.k_max == 0 && live == weights && !m.d_ms_drafts &&
              m.embed_tokens == embed && m.lm_head == head,
          "failed grow left partial scratch or released weights");
    ResetCounters();
    Check(q4t::mtp::MtpReserveScratch(m, 2).ok(), "scratch retry failed");
    Check(live.size() == expected_live, "scratch retry changed live count");
  }
  ResetCounters();
  Check(q4t::mtp::MtpReserveScratch(m, 4).ok(), "scratch grow failed");
  Check(live.size() == expected_live && m.k_max == 4,
        "successful grow leaked replaced draft matrix");
  m.Free();
  CheckEmpty(m);
  std::printf("scratch: %d failure boundaries, grow/retry/idempotence: PASS\n",
              scratch_allocations);

  // Main-model verify checkpoints have the same partial-grow contract.
  q4t::model::Model checkpoints;
  checkpoints.layers.resize(1);
  auto& layer = checkpoints.layers.front();
  layer.linear.nv = layer.linear.nkh = layer.linear.kd = layer.linear.vd = 1;
  layer.linear.conv_k = 2;
  layer.has_ple = true;
  layer.hc_dim = 1;
  layer.ple.conv_kernel = 2;
  layer.ple.conv_dilation = 1;
  ResetCounters();
  Check(q4t::model::ModelReserveVerifyCheckpoints(checkpoints, 1).ok(),
        "initial checkpoints reserve");
  Check(allocations == 3, "checkpoint fixture must exercise all pools");
  for (int failure : {1, 2, 3}) {
    ResetCounters(failure);
    Check(!q4t::model::ModelReserveVerifyCheckpoints(checkpoints, 3).ok(),
          "injected checkpoint grow unexpectedly succeeded");
    Check(checkpoints.verify_ckpt_cap == 0 && live == borrowed &&
              !checkpoints.d_verify_ssm_ckpt &&
              !checkpoints.d_verify_conv_ckpt &&
              !checkpoints.d_verify_ple_conv_ckpt,
          "checkpoint grow left partial ownership/capacity");
    ResetCounters();
    Check(q4t::model::ModelReserveVerifyCheckpoints(checkpoints, 1).ok(),
          "checkpoint retry failed");
  }
  ResetCounters();
  Check(q4t::model::ModelReserveVerifyCheckpoints(checkpoints, 3).ok(),
        "checkpoint grow failed");
  Check(live.size() == borrowed.size() + 3, "checkpoint grow leaked");
  checkpoints.Free();
  Check(live == borrowed, "checkpoint destruction leaked");
  std::puts("checkpoints: failures 1..3, capacity reset, retry/grow: PASS");

  // Real nested loaders: an early HC allocation failure and a failure at the
  // final LoadMtp allocation exercise ownership beyond the four projections.
  q4t::mtp::MtpConfig cfg;
  cfg.mtp_dir = std::string(argv[2]) + "/mtp";
  cfg.max_len = 64;
  cfg.max_prefill = 16;
  ResetCounters(8);
  require_drain = true;
  Check(!q4t::mtp::LoadMtp(cfg, embed, head, &m, nullptr).ok(),
        "nested allocation failure unexpectedly succeeded");
  Check(allocations == 8 && syncs > 0, "nested failure did not reach target");
  CheckEmpty(m);
  require_drain = false;
  ResetCounters();
  const auto loaded = q4t::mtp::LoadMtp(cfg, embed, head, &m, nullptr);
  if (!loaded.ok()) std::fprintf(stderr, "%s\n", loaded.message().c_str());
  Check(loaded.ok(), "real MTP load failed");
  const int load_allocations = allocations;
  Check(load_allocations > 8 && syncs > 0, "real load did not complete");
  const auto owned = live;
  Check(!q4t::mtp::LoadMtp(cfg, embed, head, &m, nullptr).ok() && live == owned,
        "loaded MTP was overwritten");
  m.Free();
  CheckEmpty(m);
  ResetCounters(load_allocations);
  require_drain = true;
  Check(!q4t::mtp::LoadMtp(cfg, embed, head, &m, nullptr).ok(),
        "last allocation failure unexpectedly succeeded");
  Check(allocations == load_allocations, "last failure did not reach target");
  CheckEmpty(m);
  require_drain = false;
  fail_at = 0;
  m.Free();
  CheckEmpty(m);
  std::printf(
      "real load: success, reload rejection, failure at 8/%d, "
      "nested cleanup: PASS\n",
      load_allocations);

  borrowed.clear();
  CheckCuda(cudaFree(embed), "owner frees borrowed embed");
  CheckCuda(cudaFree(head), "owner frees borrowed head");
  Check(live.empty(), "lifecycle contract leaked memory");
  std::puts("MTP lifecycle: PASS");
}
