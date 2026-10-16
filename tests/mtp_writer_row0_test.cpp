// step26 independent writer-row0 capture -> same-run production restore
// comparison (2026-10-11, coordination-thread task).
//
// Fixed-prefix replay (original arithmetic construction): prefill the
// original prompt_tokens (1024), then replay prefix_tokens[1024:1050]
// (26 saved tokens) as individual B1 decode steps, then run the T4 batch
// {b=55404, 42, 42, 42} with save_checkpoints=true. The 26 replay tokens are
// the SAVED fixture tokens (not re-greedy-selected); a single 1050-token
// prefill is deliberately avoided because it changes the initial-state
// arithmetic (prefill vs decode path).
//
// Captures (writer side, same T4 run):
//   - independent S_smem row-0 (GatedDeltaNetKernel ssm_capture, t==0),
//     separate from the checkpoint write path;
//   - row-0 of the conv input (qkv_raw) and row-0 of the PLE conv input
//     (gated_n): combined with the pre-batch live conv/PLE state these
//     reconstruct the conv / PLE-conv checkpoint row-0 on the host
//     (independent reference, separate from the checkpoint write path);
//   - checkpoint buffer rows 0/1/2 (num_ckpt=T-1=3) of SSM/conv/PLE-conv;
//   - full T4 output (all 4 logits rows);
//   - PRE-batch append-only reference at the committed boundary P (=1050):
//     per full-attention layer page_table[0..P-1] / KV slots [0..P-1]
//     (addressed via the pre-batch page table) / idx_raw[0..P-1] / idx_comp
//     valid groups, plus rope_pos[0..P-1];
//   - POST-batch live state at boundary P+1 (=1051): host metadata (the
//     batch is uncommitted: position/history stay at P), rope_pos[0..P],
//     per full-attention layer page_table[0..P] / KV slots [0..P] /
//     idx_raw[0..P] / idx_comp valid groups;
//   - T4 KV/indexer rows at positions P..P+3 (the 4 rows the batch wrote).
// Then runs the production restore (ModelRestoreCheckpoint, ckpt_idx=0) and
// captures the restored live recurrent state (SSM/conv/PLE) + re-reads the
// restored KV region [0..P] (row P = token b's KV, consistent with the
// post-b recurrent state; rows P+1..P+3 are stale batch data) plus
// page_table/idx_raw/idx_comp/rope.
//
// Checks (logged as identical/DIFF; any DIFF fails the test):
//   writer_check   ssm : independent S_smem capture vs ckpt SSM row-0
//   writer_check   conv: host reconstruction vs ckpt conv row-0
//   writer_check   ple : host reconstruction vs ckpt PLE row-0
//   restore_check  ssm : ckpt SSM row-0 vs restored live SSM
//   restore_check  conv: ckpt conv row-0 vs restored live conv
//   restore_check  ple : ckpt PLE row-0 vs restored live PLE
//   restore_xcheck ssm : independent S_smem capture vs restored live SSM
//   host           history: live (uncommitted) history vs fixture prefix;
//                        expected committed history (prefix + b) recorded
//                        separately from the fixture, never from live memory
//   appendonly     *     : pre-batch vs post-batch old region [0..P-1]
//                        (page_table/KV/idx_raw/idx_comp/rope_pos) - the
//                        batch must not disturb committed entries
//   restore_check  kv  : post-restore KV region [0..P] (consistent with the
//                        post-b recurrent state) vs pre-restore reference
//   restore_check  page_table/idx_raw/idx_comp/rope_pos : post-restore vs
//                        pre-restore (the production restore is
//                        recurrent-only and must not disturb them)
//   restore_check  host  : position/history unchanged by the restore
//
// Non-interference negative control: the capture is gated by
// Q4T_WRITER_ROW0_CAPTURE (unset by default). Checkpoints are ALWAYS on
// (save_checkpoints=true in both modes). The two runs (capture on/off) must
// produce bit-identical full T4 output AND all valid checkpoint values
// (rows 0/1/2) AND all reference-state files; the capture must not perturb
// the forward. The on/off comparison is done by the wrapper (shared file
// names in both runs).
//
// Valid domains (P = prefix_len = 1050 here):
//   - pre-batch committed state: positions 0..P-1 (host position/history at
//     P; the T4 batch {b,42,42,42} is SPECULATIVE - ModelDecodeBatch does
//     not commit, so position/history stay at P after the batch);
//   - post-batch live state: rows written at positions P..P+3, of which only
//     row P (pos 1050, token b) is captured in the boundary-P+1 reference;
//   - restored state (ckpt_idx=0 = state AFTER token b): the GDN t==0
//     checkpoint is written after the b update, and the conv/PLE row-0
//     reconstruction [old_state[1..], input_row0] is likewise the post-b
//     recurrent state, so the recurrent boundary is P+1. Valid region
//     [0..P] for KV/page_table/idx_raw/rope_pos (row P = token b's KV,
//     consistent with the post-b recurrent state) and groups
//     [0..floor((P-compress)/compress)] for idx_comp; rows P+1..P+3 are
//     stale speculative data (draft tokens, checked for non-disturbance,
//     not validity). The uncommitted host state stays at P (position/
//     history are not advanced by the test); that host boundary P is
//     distinct from the recurrent boundary P+1 and does NOT mean the
//     restored recurrent state is pre-b.
// The idx_comp group at index floor(P/compress) is completed by the batch
// (its last position P+1 is processed by it) and is OUTSIDE the captured
// valid range; it is stale after the restore but always rewritten before
// use (the group's last position is processed after any restore point).
//
// Explicit limitations (reported, not silent):
//   - the old-prefix KV/indexer (pos 0..P-1) reference is the live cache;
//     append-only (the batch does not disturb it) is now VERIFIED by the
//     pre-batch vs post-batch comparison, but the reference is not
//     independently re-derived (the same cache produced the divergent
//     output);
//   - the production restore (ModelRestoreCheckpoint) restores recurrent
//     state only; host position/history rollback and KV invalidation are the
//     caller's responsibility in the MTP flow. In this test the batch is
//     uncommitted, so no host rollback is needed and the restored state is
//     consistent with the unchanged host state (asserted).
#include "q4t/model/model.h"
#include "q4t/test.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>
#include <string>
#include <vector>

namespace {

using q4t::Status;
using q4t::model::LoadModel;
using q4t::model::Model;
using q4t::model::ModelConfig;
using q4t::model::ModelSequence;
using q4t::model::ModelBeginSequence;
using q4t::model::ModelPrefill;
using q4t::model::ModelDecodeStepSeq;
using q4t::model::ModelDecodeBatch;
using q4t::model::ModelEndSequence;
using q4t::model::ModelReserveVerifyCheckpoints;
using q4t::model::ModelRestoreCheckpoint;
using q4t::model::kKvPageSize;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";
const char* kDefaultFixturePath =
    ".q4t-work/mtp-repro-20261011/fixture.txt";
const char* kDefaultWorkDir = ".q4t-work/mtp-writer-row0-20261011";

bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

// Retry cudaMalloc for test-only capture buffers. On the Thor unified-
// memory device the first allocation after a long forward pass can
// transiently fail with cudaErrorInvalidValue even with ample free memory
// (observed 2026-10-11 writer-row0 ON run: 20.63 GB free, immediate retry
// succeeded). The buffers are test scratch; retrying does not touch
// production behavior.
bool CudaMallocRetry(void** p, size_t bytes, const char* what,
                     int attempts = 6, int sleep_ms = 250) {
  for (int i = 0; i < attempts; ++i) {
    *p = nullptr;
    if (cudaMalloc(p, bytes) == cudaSuccess) return true;
    const cudaError_t err = cudaGetLastError();
    std::printf("  %s cudaMalloc attempt %d/%d req=%zu bytes: %s\n", what,
                i + 1, attempts, bytes, cudaGetErrorString(err));
    if (i + 1 < attempts)
      std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
  }
  return false;
}

float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}

bool AllFiniteBf16(const uint16_t* lg, int n) {
  for (int i = 0; i < n; ++i)
    if (!std::isfinite(Bf16ToFloat(lg[i]))) return false;
  return true;
}

std::string ReadFileText(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return std::string();
  return std::string((std::istreambuf_iterator<char>(in)),
                     std::istreambuf_iterator<char>());
}

bool WriteAndVerifyText(const std::string& path, const std::string& text) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out) return false;
  out.write(text.data(), static_cast<std::streamsize>(text.size()));
  out.flush();
  if (!out.good()) return false;
  out.close();
  if (out.fail()) return false;
  return ReadFileText(path) == text;
}

bool WriteAndVerifyBytes(const std::string& path, const void* data,
                         size_t bytes) {
  const char* src = static_cast<const char*>(data);
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out) return false;
  out.write(src, static_cast<std::streamsize>(bytes));
  out.flush();
  if (!out.good()) return false;
  out.close();
  if (out.fail()) return false;
  std::ifstream in(path, std::ios::binary);
  if (!in) return false;
  in.seekg(0, std::ios::end);
  const std::streamoff size = in.tellg();
  if (size != static_cast<std::streamoff>(bytes)) return false;
  in.seekg(0, std::ios::beg);
  std::vector<char> back(bytes);
  in.read(back.data(), static_cast<std::streamsize>(bytes));
  if (!in) return false;
  return std::memcmp(back.data(), src, bytes) == 0;
}

// Parse the repro fixture: prompt_tokens, prefix_tokens, input_token_b,
// filler. Returns false on any parse error.
struct Fixture {
  std::vector<int32_t> prompt_tokens;
  std::vector<int32_t> prefix_tokens;
  int32_t b = 0;
  std::vector<int32_t> filler;
};

bool LoadFixture(const std::string& path, Fixture* fx) {
  const std::string text = ReadFileText(path);
  if (text.empty()) return false;
  auto field = [&](const char* key) -> std::string {
    const std::string k = std::string(key) + " ";
    std::istringstream ss(text);
    std::string line;
    while (std::getline(ss, line)) {
      if (line.rfind(k, 0) == 0) return line.substr(k.size());
    }
    return std::string();
  };
  auto parse_ints = [](const std::string& s, std::vector<int32_t>* out) {
    out->clear();
    std::istringstream ss(s);
    std::string tok;
    while (std::getline(ss, tok, ',')) {
      if (tok.empty()) continue;
      out->push_back(static_cast<int32_t>(std::stol(tok)));
    }
    return !out->empty();
  };
  if (!parse_ints(field("prompt_tokens"), &fx->prompt_tokens)) return false;
  if (!parse_ints(field("prefix_tokens"), &fx->prefix_tokens)) return false;
  const std::string b = field("input_token_b");
  if (b.empty()) return false;
  fx->b = static_cast<int32_t>(std::stol(b));
  if (!parse_ints(field("filler"), &fx->filler)) return false;
  return true;
}

struct Setup {
  Model m;
  ModelSequence seq;
  int vocab = 0;
  uint16_t* d_plain = nullptr;
  uint16_t* d_batch = nullptr;
  uint16_t* d_prefill = nullptr;
  cudaStream_t stream = nullptr;
};

Status SetupModel(Setup* st) {
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.ple_sidecar = kPleSidecar;
  cfg.max_prefill = 8192;
  cfg.max_len = 8192;
  cfg.max_seq = 1;
  Status s = LoadModel(cfg, &st->m, nullptr);
  if (!s.ok()) return s;
  st->vocab = st->m.cfg.vocab;
  st->stream = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&st->d_plain),
                 static_cast<size_t>(st->vocab) * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&st->d_batch),
                 static_cast<size_t>(4) * st->vocab * 2) !=
          cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&st->d_prefill),
                 static_cast<size_t>(8192) * st->vocab * 2) !=
          cudaSuccess) {
    st->m.Free();
    return Status::Fail("cudaMalloc failed");
  }
  s = ModelBeginSequence(st->m, &st->seq, st->stream);
  if (!s.ok()) return s;
  return Status();
}

void TeardownModel(Setup* st) {
  if (st->d_plain) cudaFree(st->d_plain);
  if (st->d_batch) cudaFree(st->d_batch);
  if (st->d_prefill) cudaFree(st->d_prefill);
  st->m.Free();
}

// Collect per-linear-layer SSM/conv element counts (+ conv input width and
// history length) and per-PLE-layer ple_conv element counts (+ state length
// and gated_n row width), in layer order (matching RunLayers/restore).
struct CkptDims {
  std::vector<size_t> ssm_elems;
  std::vector<size_t> conv_elems;
  std::vector<size_t> in_qkv;      // per linear layer (qkv_raw row length)
  std::vector<size_t> conv_hist;   // per linear layer (conv_k - 1)
  std::vector<size_t> ple_conv_elems;
  std::vector<size_t> ple_state_len;  // per PLE layer
  std::vector<size_t> ple_hc_dim;     // per PLE layer (gated_n row length)
  int num_lin = 0;
  int num_ple = 0;
};

CkptDims CollectCkptDims(const Model& m) {
  CkptDims d;
  for (const auto& l : m.layers) {
    if (!l.is_full_attention) {
      const auto& lin = l.linear;
      d.ssm_elems.push_back(
          static_cast<size_t>(lin.nv) * lin.kd * lin.vd);
      d.conv_elems.push_back(
          static_cast<size_t>(lin.in_qkv()) * (lin.conv_k - 1));
      d.in_qkv.push_back(static_cast<size_t>(lin.in_qkv()));
      d.conv_hist.push_back(static_cast<size_t>(lin.conv_k - 1));
      d.num_lin++;
    }
    if (l.has_ple) {
      d.ple_conv_elems.push_back(
          static_cast<size_t>(l.hc_dim) * (l.ple.conv_kernel - 1) *
          l.ple.conv_dilation);
      d.ple_state_len.push_back(
          static_cast<size_t>(l.ple.conv_kernel - 1) * l.ple.conv_dilation);
      d.ple_hc_dim.push_back(static_cast<size_t>(l.hc_dim));
      d.num_ple++;
    }
  }
  return d;
}

// Full-attention layer info (in layer order).
struct FullAttnInfo {
  int layer_idx = -1;
  int nkv = 0;
  int hd = 0;
  int idx_hd = 0;
  int compress = 0;
};

std::vector<FullAttnInfo> CollectFullAttn(const Model& m) {
  std::vector<FullAttnInfo> v;
  for (int l = 0; l < m.cfg.num_layers; ++l) {
    if (!m.layers[l].is_full_attention) continue;
    FullAttnInfo fi;
    fi.layer_idx = l;
    fi.nkv = m.layers[l].full.nkv;
    fi.hd = m.layers[l].full.hd;
    fi.idx_hd = m.layers[l].full.idx_head_dim;
    fi.compress = m.layers[l].full.idx_compress;
    v.push_back(fi);
  }
  return v;
}

// Restored-state valid idx_comp group count: group g covers positions
// [g*compress, g*compress+compress-1] and is valid only when its last
// position is committed (<= P-1), i.e. g <= (P-compress)/compress.
int ValidIdxCompGroups(int P, int compress) {
  return P >= compress ? (P - compress) / compress + 1 : 0;
}

}  // namespace

Q4T_TEST(mtp_writer_row0) {
  if (!CudaAvailable()) Q4T_SKIP("(skipped: no CUDA device)");
  const bool capture_on =
      std::getenv("Q4T_WRITER_ROW0_CAPTURE") != nullptr;
  const char* fixture_env = std::getenv("Q4T_WRITER_FIXTURE");
  const std::string fixture_path =
      fixture_env ? fixture_env : kDefaultFixturePath;
  const char* work_env = std::getenv("Q4T_REPRO_DIR");
  const std::string work_dir = work_env ? work_env : kDefaultWorkDir;
  if (!std::filesystem::create_directories(work_dir) &&
      !std::filesystem::exists(work_dir)) {
    std::printf("  cannot create work dir %s\n", work_dir.c_str());
    return false;
  }

  Fixture fx;
  if (!LoadFixture(fixture_path, &fx)) {
    std::printf("  fixture load failed: %s\n", fixture_path.c_str());
    return false;
  }
  const int prompt_len = static_cast<int>(fx.prompt_tokens.size());
  const int prefix_len = static_cast<int>(fx.prefix_tokens.size());
  const int replay_len = prefix_len - prompt_len;  // 26 B1 steps
  const int kT = 4;
  const int num_ckpt = kT - 1;  // 3 valid checkpoint rows (0/1/2)
  const int boundary = prefix_len + 1;  // P+1 = 1051

  Setup st;
  Status s = SetupModel(&st);
  if (!s.ok()) {
    std::printf("  setup failed: %s\n", s.message().c_str());
    return false;
  }
  const CkptDims dims = CollectCkptDims(st.m);
  const std::vector<FullAttnInfo> full_attn = CollectFullAttn(st.m);

  // 1. Prefill the original prompt_tokens (1024) — original arithmetic.
  s = ModelPrefill(st.m, &st.seq, fx.prompt_tokens.data(), prompt_len,
                   st.d_prefill, st.stream);
  if (!s.ok()) {
    std::printf("  prefill failed: %s\n", s.message().c_str());
    TeardownModel(&st);
    return false;
  }

  // 2. Replay prefix_tokens[1024:1050] as 26 fixed B1 decode steps (NOT
  //    greedy; the saved fixture tokens preserve the original step26 state).
  for (int i = 0; i < replay_len; ++i) {
    const int32_t tok = fx.prefix_tokens[prompt_len + i];
    Status sp = ModelDecodeStepSeq(st.m, &st.seq, tok, st.d_plain, st.stream);
    if (!sp.ok()) {
      std::printf("  B1 replay step %d failed: %s\n", i, sp.message().c_str());
      TeardownModel(&st);
      return false;
    }
  }

  const int base = st.seq.position;
  if (base != prefix_len) {
    std::printf("  P mismatch: position=%d prefix_len=%d (expected equal)\n",
                base, prefix_len);
    TeardownModel(&st);
    return false;
  }

  // The prefill logits buffer (8192 x vocab x 2B ~= 2.5 GB) and the B1
  // buffer are no longer needed; free them so the capture buffers (ON run)
  // have headroom on the unified-memory device.
  if (st.d_prefill) {
    cudaFree(st.d_prefill);
    st.d_prefill = nullptr;
  }
  if (st.d_plain) {
    cudaFree(st.d_plain);
    st.d_plain = nullptr;
  }

  // 3. Reserve verify checkpoints (stride cap = num_ckpt = T-1).
  s = ModelReserveVerifyCheckpoints(st.m, num_ckpt);
  if (!s.ok()) {
    std::printf("  reserve ckpt failed: %s\n", s.message().c_str());
    TeardownModel(&st);
    return false;
  }
  if (st.m.verify_ckpt_cap != num_ckpt) {
    std::printf("  verify_ckpt_cap=%d != num_ckpt=%d\n", st.m.verify_ckpt_cap,
                num_ckpt);
    TeardownModel(&st);
    return false;
  }
  const int cap = st.m.verify_ckpt_cap;
  std::printf("  step26: P=%d b=%d capture=%d num_ckpt=%d cap=%d\n", base,
              fx.b, capture_on ? 1 : 0, num_ckpt, cap);

  // 4. Pre-batch live conv/PLE state (old_state for the row-0
  //    reconstruction). Always captured (both on/off runs).
  std::vector<std::vector<uint16_t>> pre_conv(dims.num_lin);
  std::vector<std::vector<uint16_t>> pre_ple(dims.num_ple);
  {
    int li = 0;
    int pi = 0;
    for (const auto& l : st.m.layers) {
      if (!l.is_full_attention) {
        pre_conv[li].resize(dims.conv_elems[li]);
        if (cudaMemcpy(pre_conv[li].data(), l.conv_state,
                       dims.conv_elems[li] * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  pre_conv D2H failed (lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        if (!WriteAndVerifyBytes(work_dir + "/pre_conv_lin" +
                                    std::to_string(li) + ".bin",
                                 pre_conv[li].data(),
                                 pre_conv[li].size() * sizeof(uint16_t))) {
          std::printf("  pre_conv write failed (lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        li++;
      }
      if (l.has_ple) {
        pre_ple[pi].resize(dims.ple_conv_elems[pi]);
        if (cudaMemcpy(pre_ple[pi].data(), l.ple_conv_state,
                       dims.ple_conv_elems[pi] * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  pre_ple D2H failed (ple %d)\n", pi);
          TeardownModel(&st);
          return false;
        }
        if (!WriteAndVerifyBytes(work_dir + "/pre_ple_ple" +
                                    std::to_string(pi) + ".bin",
                                 pre_ple[pi].data(),
                                 pre_ple[pi].size() * sizeof(uint16_t))) {
          std::printf("  pre_ple write failed (ple %d)\n", pi);
          TeardownModel(&st);
          return false;
        }
        pi++;
      }
    }
  }

  // 4b. Pre-batch append-only reference at the committed boundary P
  //     (always): per full-attention layer page_table[0..P-1] / KV slots
  //     [0..P-1] (addressed via the pre-batch page table) / idx_raw[0..P-1]
  //     / idx_comp valid groups, plus rope_pos[0..P-1]. Compared against the
  //     post-batch live state (section 10b) to verify the T4 batch does not
  //     disturb committed entries (append-only addressing).
  std::vector<std::vector<int>> pre_pt;  // per full-attn layer
  std::vector<std::vector<uint16_t>> pre_kv;
  std::vector<std::vector<uint16_t>> pre_idxraw;
  std::vector<std::vector<uint16_t>> pre_idxcomp;
  std::vector<int> pre_rope(static_cast<size_t>(3) * prefix_len);
  {
    const int* basep = st.m.d_rope_pos;  // seq_id 0
    for (int axis = 0; axis < 3; ++axis) {
      if (cudaMemcpy(&pre_rope[static_cast<size_t>(axis) * prefix_len],
                     basep +
                         static_cast<size_t>(axis) * st.m.cfg.max_len,
                     static_cast<size_t>(prefix_len) * sizeof(int),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  prestate rope_pos D2H failed (axis %d)\n", axis);
        TeardownModel(&st);
        return false;
      }
    }
    if (!WriteAndVerifyBytes(work_dir + "/prestate_rope_pos.bin",
                             pre_rope.data(), pre_rope.size() * sizeof(int))) {
      std::printf("  prestate rope_pos write failed\n");
      TeardownModel(&st);
      return false;
    }
    int fa = 0;
    for (const auto& fi : full_attn) {
      const auto& L = st.m.layers[fi.layer_idx];
      pre_pt.emplace_back(prefix_len);
      if (cudaMemcpy(pre_pt[fa].data(), L.page_table,
                     static_cast<size_t>(prefix_len) * sizeof(int),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  prestate page_table D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/prestate_pt_fa" + std::to_string(fa) + ".bin",
              pre_pt[fa].data(), pre_pt[fa].size() * sizeof(int))) {
        std::printf("  prestate page_table write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      const size_t slot = static_cast<size_t>(fi.nkv) * 2 * fi.hd;
      pre_kv.emplace_back(static_cast<size_t>(prefix_len) * slot);
      for (int p = 0; p < prefix_len; ++p) {
        const int off = pre_pt[fa][p] * kKvPageSize + (p % kKvPageSize);
        if (cudaMemcpy(&pre_kv[fa][static_cast<size_t>(p) * slot],
                       L.kv_cache + static_cast<size_t>(off) * slot,
                       slot * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  prestate KV D2H failed (fa %d pos %d)\n", fa, p);
          TeardownModel(&st);
          return false;
        }
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/prestate_kv_fa" + std::to_string(fa) + ".bin",
              pre_kv[fa].data(), pre_kv[fa].size() * sizeof(uint16_t))) {
        std::printf("  prestate KV write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      pre_idxraw.emplace_back(static_cast<size_t>(prefix_len) * fi.idx_hd);
      if (cudaMemcpy(pre_idxraw[fa].data(), L.idx_raw,
                     pre_idxraw[fa].size() * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  prestate idx_raw D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/prestate_idxraw_fa" + std::to_string(fa) + ".bin",
              pre_idxraw[fa].data(),
              pre_idxraw[fa].size() * sizeof(uint16_t))) {
        std::printf("  prestate idx_raw write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      const int vg = ValidIdxCompGroups(prefix_len, fi.compress);
      pre_idxcomp.emplace_back(static_cast<size_t>(vg) * fi.idx_hd);
      if (vg > 0 &&
          cudaMemcpy(pre_idxcomp[fa].data(), L.idx_comp,
                     pre_idxcomp[fa].size() * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  prestate idx_comp D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/prestate_idxcomp_fa" + std::to_string(fa) + ".bin",
              pre_idxcomp[fa].data(),
              pre_idxcomp[fa].size() * sizeof(uint16_t))) {
        std::printf("  prestate idx_comp write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      fa++;
    }
  }

  // 5. Capture buffers (test-only, capture_on).
  std::vector<std::vector<float>> ssm_cap_host;
  std::vector<float*> ssm_cap_dev;
  std::vector<float*> ssm_cap_ptrs;
  std::vector<std::vector<uint16_t>> qkv_raw_cap_host;
  std::vector<uint16_t*> qkv_raw_cap_dev;
  std::vector<uint16_t*> qkv_raw_cap_ptrs;
  std::vector<std::vector<uint16_t>> gated_n_cap_host;
  std::vector<uint16_t*> gated_n_cap_dev;
  std::vector<uint16_t*> gated_n_cap_ptrs;
  if (capture_on) {
    for (int li = 0; li < dims.num_lin; ++li) {
      ssm_cap_host.emplace_back(dims.ssm_elems[li]);
      const size_t ssm_bytes = dims.ssm_elems[li] * sizeof(float);
      void* p = nullptr;
      if (!CudaMallocRetry(&p, ssm_bytes, "ssm_capture")) {
        size_t mf = 0, mt = 0;
        cudaMemGetInfo(&mf, &mt);
        std::printf("  ssm_capture alloc failed (lin %d) req=%zu bytes; "
                    "free=%.2f GB total=%.2f GB\n",
                    li, ssm_bytes, static_cast<double>(mf) / 1e9,
                    static_cast<double>(mt) / 1e9);
        TeardownModel(&st);
        return false;
      }
      ssm_cap_dev.push_back(static_cast<float*>(p));
      cudaMemsetAsync(ssm_cap_dev.back(), 0,
                      dims.ssm_elems[li] * sizeof(float), st.stream);
      ssm_cap_ptrs.push_back(ssm_cap_dev.back());
      qkv_raw_cap_host.emplace_back(dims.in_qkv[li]);
      void* pq = nullptr;
      if (!CudaMallocRetry(&pq, dims.in_qkv[li] * sizeof(uint16_t),
                           "qkv_raw_capture")) {
        std::printf("  qkv_raw_capture alloc failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      qkv_raw_cap_dev.push_back(static_cast<uint16_t*>(pq));
      qkv_raw_cap_ptrs.push_back(qkv_raw_cap_dev.back());
    }
    for (int pi = 0; pi < dims.num_ple; ++pi) {
      gated_n_cap_host.emplace_back(dims.ple_hc_dim[pi]);
      void* pg = nullptr;
      if (!CudaMallocRetry(&pg, dims.ple_hc_dim[pi] * sizeof(uint16_t),
                           "gated_n_capture")) {
        std::printf("  gated_n_capture alloc failed (ple %d)\n", pi);
        TeardownModel(&st);
        return false;
      }
      gated_n_cap_dev.push_back(static_cast<uint16_t*>(pg));
      gated_n_cap_ptrs.push_back(gated_n_cap_dev.back());
    }
  }

  // 6. T4 batch {b, filler...} with save_checkpoints=true (ALWAYS on).
  const int32_t batch_in[4] = {fx.b, fx.filler[0], fx.filler[1], fx.filler[2]};
  Status sb = ModelDecodeBatch(st.m, batch_in, kT, base,
                               st.seq.history.data(),
                               static_cast<int>(st.seq.history.size()),
                               st.d_batch, st.stream, nullptr,
                               /*save_checkpoints=*/true, 0,
                               q4t::model::LogitsRows::kAllRows,
                               capture_on ? ssm_cap_ptrs.data() : nullptr,
                               capture_on ? qkv_raw_cap_ptrs.data() : nullptr,
                               capture_on ? gated_n_cap_ptrs.data() : nullptr);
  if (!sb.ok()) {
    std::printf("  T4 batch failed: %s\n", sb.message().c_str());
    const cudaError_t sticky = cudaGetLastError();
    std::printf("  sticky_error_after_batch=%s\n",
                cudaGetErrorString(sticky));
    int* pd = nullptr;
    if (cudaMalloc(reinterpret_cast<void**>(&pd), sizeof(int)) ==
        cudaSuccess) {
      if (cudaMemsetAsync(pd, 7, sizeof(int), st.stream) == cudaSuccess) {
        int hv = 0;
        const cudaError_t pe =
            cudaMemcpy(&hv, pd, sizeof(int), cudaMemcpyDeviceToHost);
        std::printf("  probe_d2h=%s val=%d\n",
                    pe == cudaSuccess ? "OK" : cudaGetErrorString(pe), hv);
      } else {
        std::printf("  probe_memset=%s\n",
                    cudaGetErrorString(cudaGetLastError()));
      }
      cudaFree(pd);
    }
    TeardownModel(&st);
    return false;
  }
  if (cudaStreamSynchronize(st.stream) != cudaSuccess) {
    std::printf("  T4 stream sync failed\n");
    TeardownModel(&st);
    return false;
  }

  std::string log;
  int diff_count = 0;
  auto log_check = [&log, &diff_count](const std::string& name,
                                       bool identical) {
    log += name + (identical ? " identical\n" : " DIFF\n");
    if (!identical) diff_count++;
  };
  log += "# writer-row0 capture (capture_on=" +
         std::string(capture_on ? "1" : "0") + ")\n";
  log += "fixture=" + fixture_path + "\n";
  log += "step=26 P=" + std::to_string(base) + " b=" + std::to_string(fx.b) +
         " num_ckpt=" + std::to_string(num_ckpt) + " cap=" +
         std::to_string(cap) + "\n";
  log += "valid_boundary=P+1=" + std::to_string(boundary) +
         " (pos 0.." + std::to_string(prefix_len) +
         "); future rows pos " + std::to_string(prefix_len + 1) + "/" +
         std::to_string(prefix_len + 2) + "/" + std::to_string(prefix_len + 3) +
         " NOT valid\n";

  // 7. Full T4 output (all 4 rows) — always (non-interference control).
  {
    std::vector<uint16_t> h(static_cast<size_t>(kT) * st.vocab);
    if (cudaMemcpy(h.data(), st.d_batch, h.size() * sizeof(uint16_t),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      std::printf("  T4 output D2H failed\n");
      TeardownModel(&st);
      return false;
    }
    if (!AllFiniteBf16(h.data(), static_cast<int>(h.size()))) {
      std::printf("  T4 output non-finite\n");
      TeardownModel(&st);
      return false;
    }
    const std::string path = work_dir + "/t4_output.bin";
    if (!WriteAndVerifyBytes(path, h.data(), h.size() * sizeof(uint16_t))) {
      std::printf("  T4 output write failed\n");
      TeardownModel(&st);
      return false;
    }
    log += "t4_output rows=" + std::to_string(kT) +
           " file=t4_output.bin\n";
    for (int r = 0; r < kT; ++r) {
      int argmax = 0;
      float best = -1e30f;
      for (int v = 0; v < st.vocab; ++v) {
        const float x = Bf16ToFloat(h[static_cast<size_t>(r) * st.vocab + v]);
        if (x > best) {
          best = x;
          argmax = v;
        }
      }
      log += "  row" + std::to_string(r) + " argmax=" + std::to_string(argmax) +
             " val=" + std::to_string(best) + "\n";
    }
  }

  // 8. All valid checkpoint rows (0/1/2) of SSM/conv/PLE — always.
  for (int tt = 0; tt < num_ckpt; ++tt) {
    for (int li = 0; li < dims.num_lin; ++li) {
      const size_t ssm_e = dims.ssm_elems[li];
      const float* src =
          st.m.d_verify_ssm_ckpt +
          (static_cast<size_t>(li) * cap + tt) * ssm_e;
      std::vector<float> h(ssm_e);
      if (cudaMemcpy(h.data(), src, ssm_e * sizeof(float),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  ckpt SSM D2H failed (tt=%d lin=%d)\n", tt, li);
        TeardownModel(&st);
        return false;
      }
      const std::string path = work_dir + "/ckpt_ssm_row" + std::to_string(tt) +
                               "_lin" + std::to_string(li) + ".bin";
      if (!WriteAndVerifyBytes(path, h.data(), h.size() * sizeof(float))) {
        std::printf("  ckpt SSM write failed (tt=%d lin=%d)\n", tt, li);
        TeardownModel(&st);
        return false;
      }
      const size_t conv_e = dims.conv_elems[li];
      const uint16_t* srcc =
          st.m.d_verify_conv_ckpt +
          (static_cast<size_t>(li) * cap + tt) * conv_e;
      std::vector<uint16_t> hc(conv_e);
      if (cudaMemcpy(hc.data(), srcc, conv_e * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  ckpt conv D2H failed (tt=%d lin=%d)\n", tt, li);
        TeardownModel(&st);
        return false;
      }
      const std::string pathc = work_dir + "/ckpt_conv_row" +
                                std::to_string(tt) + "_lin" +
                                std::to_string(li) + ".bin";
      if (!WriteAndVerifyBytes(pathc, hc.data(),
                               hc.size() * sizeof(uint16_t))) {
        std::printf("  ckpt conv write failed (tt=%d lin=%d)\n", tt, li);
        TeardownModel(&st);
        return false;
      }
    }
    for (int pi = 0; pi < dims.num_ple; ++pi) {
      const size_t ple_e = dims.ple_conv_elems[pi];
      const uint16_t* src =
          st.m.d_verify_ple_conv_ckpt +
          (static_cast<size_t>(pi) * cap + tt) * ple_e;
      std::vector<uint16_t> h(ple_e);
      if (cudaMemcpy(h.data(), src, ple_e * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  ckpt PLE D2H failed (tt=%d ple=%d)\n", tt, pi);
        TeardownModel(&st);
        return false;
      }
      const std::string path = work_dir + "/ckpt_ple_row" +
                                std::to_string(tt) + "_ple" +
                                std::to_string(pi) + ".bin";
      if (!WriteAndVerifyBytes(path, h.data(),
                               h.size() * sizeof(uint16_t))) {
        std::printf("  ckpt PLE write failed (tt=%d ple=%d)\n", tt, pi);
        TeardownModel(&st);
        return false;
      }
    }
    log += "ckpt row" + std::to_string(tt) + " SSM/conv/PLE captured\n";
  }

  // 9. Writer checks (capture_on): independent capture / host reconstruction
  //    vs checkpoint row-0.
  if (capture_on) {
    // 9a. SSM: independent S_smem capture vs ckpt SSM row-0.
    for (int li = 0; li < dims.num_lin; ++li) {
      const size_t ssm_e = dims.ssm_elems[li];
      if (cudaMemcpy(ssm_cap_host[li].data(), ssm_cap_dev[li],
                     ssm_e * sizeof(float), cudaMemcpyDeviceToHost) !=
          cudaSuccess) {
        std::printf("  ssm_capture D2H failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      const std::string path = work_dir + "/ssm_capture_lin" +
                               std::to_string(li) + ".bin";
      if (!WriteAndVerifyBytes(path, ssm_cap_host[li].data(),
                               ssm_cap_host[li].size() * sizeof(float))) {
        std::printf("  ssm_capture write failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      log += "ssm_capture lin" + std::to_string(li) +
             " file=ssm_capture_lin" + std::to_string(li) + ".bin\n";
      bool any = false;
      for (float f : ssm_cap_host[li])
        if (f != 0.0f) {
          any = true;
          break;
        }
      if (!any) {
        log += "writer_check ssm lin" + std::to_string(li) +
               " capture_all_zero (capture did not fire)\n";
        diff_count++;
      }
      const float* ck = st.m.d_verify_ssm_ckpt +
                        (static_cast<size_t>(li) * cap + 0) * ssm_e;
      std::vector<float> hck(ssm_e);
      if (cudaMemcpy(hck.data(), ck, ssm_e * sizeof(float),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  ckpt SSM row0 D2H failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      log_check("writer_check ssm lin" + std::to_string(li) +
                    " capture_vs_ckpt ",
                std::memcmp(hck.data(), ssm_cap_host[li].data(),
                            ssm_e * sizeof(float)) == 0);
    }
    // 9b. conv: host reconstruction [old_state[1..hist-1], qkv_raw row0]
    //     vs ckpt conv row-0 (kernel layout: state [ch, hist]). Checkpoint
    //     row t is the conv window AFTER batch token t: the pre-batch window
    //     slid by t+1 (oldest t+1 entries dropped), newest entry = input row
    //     t. For t=0: [old_state[1..hist-1], input_row0].
    for (int li = 0; li < dims.num_lin; ++li) {
      const size_t inq = dims.in_qkv[li];
      const size_t hist = dims.conv_hist[li];
      if (cudaMemcpy(qkv_raw_cap_host[li].data(), qkv_raw_cap_dev[li],
                     inq * sizeof(uint16_t), cudaMemcpyDeviceToHost) !=
          cudaSuccess) {
        std::printf("  qkv_raw_capture D2H failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      const std::string path = work_dir + "/qkv_raw_row0_lin" +
                               std::to_string(li) + ".bin";
      if (!WriteAndVerifyBytes(path, qkv_raw_cap_host[li].data(),
                               qkv_raw_cap_host[li].size() * sizeof(uint16_t))) {
        std::printf("  qkv_raw_capture write failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      bool any = false;
      for (uint16_t v : qkv_raw_cap_host[li])
        if (v != 0) {
          any = true;
          break;
        }
      if (!any) {
        log += "writer_check conv lin" + std::to_string(li) +
               " qkv_raw_all_zero (capture did not fire)\n";
        diff_count++;
      }
      std::vector<uint16_t> recon(inq * hist);
      for (size_t ch = 0; ch < inq; ++ch)
        for (size_t k = 0; k < hist; ++k)
          recon[ch * hist + k] =
              (k < hist - 1) ? pre_conv[li][ch * hist + (k + 1)]
                             : qkv_raw_cap_host[li][ch];
      const std::string pathr = work_dir + "/writer_conv_recon_lin" +
                                std::to_string(li) + ".bin";
      if (!WriteAndVerifyBytes(pathr, recon.data(),
                               recon.size() * sizeof(uint16_t))) {
        std::printf("  writer_conv_recon write failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      const uint16_t* ck =
          st.m.d_verify_conv_ckpt +
          (static_cast<size_t>(li) * cap + 0) * inq * hist;
      std::vector<uint16_t> hck(inq * hist);
      if (cudaMemcpy(hck.data(), ck, inq * hist * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  ckpt conv row0 D2H failed (lin %d)\n", li);
        TeardownModel(&st);
        return false;
      }
      log_check("writer_check conv lin" + std::to_string(li) +
                    " recon_vs_ckpt ",
                std::memcmp(hck.data(), recon.data(),
                            inq * hist * sizeof(uint16_t)) == 0);
    }
    // 9c. PLE: host reconstruction [old_state[1..sl-1], gated_n row0]
    //     vs ckpt PLE row-0 (kernel layout: state [c, state_len]). Same
    //     post-token slide semantics as the conv checkpoint (row t = window
    //     after batch token t).
    for (int pi = 0; pi < dims.num_ple; ++pi) {
      const size_t C = dims.ple_hc_dim[pi];
      const size_t sl = dims.ple_state_len[pi];
      if (cudaMemcpy(gated_n_cap_host[pi].data(), gated_n_cap_dev[pi],
                     C * sizeof(uint16_t), cudaMemcpyDeviceToHost) !=
          cudaSuccess) {
        std::printf("  gated_n_capture D2H failed (ple %d)\n", pi);
        TeardownModel(&st);
        return false;
      }
      const std::string path = work_dir + "/gated_n_row0_ple" +
                               std::to_string(pi) + ".bin";
      if (!WriteAndVerifyBytes(path, gated_n_cap_host[pi].data(),
                               gated_n_cap_host[pi].size() * sizeof(uint16_t))) {
        std::printf("  gated_n_capture write failed (ple %d)\n", pi);
        TeardownModel(&st);
        return false;
      }
      bool any = false;
      for (uint16_t v : gated_n_cap_host[pi])
        if (v != 0) {
          any = true;
          break;
        }
      if (!any) {
        log += "writer_check ple ple" + std::to_string(pi) +
               " gated_n_all_zero (capture did not fire)\n";
        diff_count++;
      }
      std::vector<uint16_t> recon(C * sl);
      for (size_t c = 0; c < C; ++c)
        for (size_t k = 0; k < sl; ++k)
          recon[c * sl + k] =
              (k < sl - 1) ? pre_ple[pi][c * sl + (k + 1)]
                           : gated_n_cap_host[pi][c];
      const std::string pathr = work_dir + "/writer_ple_recon_ple" +
                                std::to_string(pi) + ".bin";
      if (!WriteAndVerifyBytes(pathr, recon.data(),
                               recon.size() * sizeof(uint16_t))) {
        std::printf("  writer_ple_recon write failed (ple %d)\n", pi);
        TeardownModel(&st);
        return false;
      }
      const uint16_t* ck =
          st.m.d_verify_ple_conv_ckpt +
          (static_cast<size_t>(pi) * cap + 0) * C * sl;
      std::vector<uint16_t> hck(C * sl);
      if (cudaMemcpy(hck.data(), ck, C * sl * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  ckpt PLE row0 D2H failed (ple %d)\n", pi);
        TeardownModel(&st);
        return false;
      }
      log_check("writer_check ple ple" + std::to_string(pi) +
                    " recon_vs_ckpt ",
                std::memcmp(hck.data(), recon.data(),
                            C * sl * sizeof(uint16_t)) == 0);
    }
  }

  // 10. Post-batch live state at boundary P+1 (always): host metadata,
  //     rope_pos[0..P], per full-attention layer page_table[0..P] / KV slots
  //     [0..P] / idx_raw[0..P] / idx_comp valid groups. The old prefix
  //     (0..P-1) is verified unchanged by the T4 batch (appendonly checks,
  //     section 10b); row P was written by it. The batch is uncommitted, so
  //     the live host history has length P (NOT P+1): the expected committed
  //     history (fixture prefix + b) is recorded separately from the
  //     fixture, never read from live memory.
  std::vector<std::vector<uint16_t>> refstate_kv;  // per full-attn layer
  std::vector<std::vector<int>> refstate_pt;
  std::vector<std::vector<uint16_t>> refstate_idxraw;
  std::vector<std::vector<uint16_t>> refstate_idxcomp;
  std::vector<int> refstate_rope;
  {
    const size_t hist_size = st.seq.history.size();
    std::string t = "live_position=" + std::to_string(st.seq.position) + "\n";
    t += "live_history_size=" + std::to_string(hist_size) + "\n";
    t += "ref_boundary_position=" + std::to_string(boundary) + "\n";
    t += "live_history[0..P-1]=";  // uncommitted; length P, in-bounds only
    for (size_t p = 0; p < hist_size; ++p)
      t += std::to_string(st.seq.history[p]) + ",";
    t += "\n";
    t += "expected_committed_history[0..P]=";  // fixture prefix + b
    for (int p = 0; p < prefix_len; ++p)
      t += std::to_string(fx.prefix_tokens[p]) + ",";
    t += std::to_string(fx.b) + "\n";
    if (!WriteAndVerifyText(work_dir + "/refstate_host.txt", t)) {
      std::printf("  refstate host write failed\n");
      TeardownModel(&st);
      return false;
    }
    // Host sanity: the batch must be uncommitted (position/history at P)
    // and the live history must be exactly the fixture prefix.
    log_check("host position_after_batch==P ",
              st.seq.position == prefix_len);
    log_check("host live_history_vs_fixture_prefix ",
              hist_size == static_cast<size_t>(prefix_len) &&
              std::equal(st.seq.history.begin(), st.seq.history.end(),
                         fx.prefix_tokens.begin()));
    const int ml = st.m.cfg.max_len;
    refstate_rope.resize(static_cast<size_t>(3) * boundary);
    const int* basep = st.m.d_rope_pos;  // seq_id 0
    for (int axis = 0; axis < 3; ++axis) {
      if (cudaMemcpy(&refstate_rope[static_cast<size_t>(axis) * boundary],
                     basep + static_cast<size_t>(axis) * ml,
                     static_cast<size_t>(boundary) * sizeof(int),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  refstate rope_pos D2H failed (axis %d)\n", axis);
        TeardownModel(&st);
        return false;
      }
    }
    if (!WriteAndVerifyBytes(work_dir + "/refstate_rope_pos.bin",
                             refstate_rope.data(),
                             refstate_rope.size() * sizeof(int))) {
      std::printf("  refstate rope_pos write failed\n");
      TeardownModel(&st);
      return false;
    }
    int fa = 0;
    for (const auto& fi : full_attn) {
      const auto& L = st.m.layers[fi.layer_idx];
      refstate_pt.emplace_back(boundary);
      if (cudaMemcpy(refstate_pt[fa].data(), L.page_table,
                     static_cast<size_t>(boundary) * sizeof(int),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  refstate page_table D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/refstate_pt_fa" + std::to_string(fa) + ".bin",
              refstate_pt[fa].data(),
              refstate_pt[fa].size() * sizeof(int))) {
        std::printf("  refstate page_table write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      const size_t slot = static_cast<size_t>(fi.nkv) * 2 * fi.hd;
      std::vector<uint16_t> kv(static_cast<size_t>(boundary) * slot);
      for (int p = 0; p < boundary; ++p) {
        const int off = refstate_pt[fa][p] * kKvPageSize + (p % kKvPageSize);
        if (cudaMemcpy(&kv[static_cast<size_t>(p) * slot],
                       L.kv_cache + static_cast<size_t>(off) * slot,
                       slot * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  refstate KV D2H failed (fa %d pos %d)\n", fa, p);
          TeardownModel(&st);
          return false;
        }
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/refstate_kv_fa" + std::to_string(fa) + ".bin",
              kv.data(), kv.size() * sizeof(uint16_t))) {
        std::printf("  refstate KV write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      refstate_kv.push_back(std::move(kv));
      refstate_idxraw.emplace_back(static_cast<size_t>(boundary) * fi.idx_hd);
      if (cudaMemcpy(refstate_idxraw[fa].data(), L.idx_raw,
                     refstate_idxraw[fa].size() * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  refstate idx_raw D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/refstate_idxraw_fa" + std::to_string(fa) + ".bin",
              refstate_idxraw[fa].data(),
              refstate_idxraw[fa].size() * sizeof(uint16_t))) {
        std::printf("  refstate idx_raw write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      const int groups = ValidIdxCompGroups(prefix_len, fi.compress);
      refstate_idxcomp.emplace_back(static_cast<size_t>(groups) * fi.idx_hd);
      if (groups > 0 &&
          cudaMemcpy(refstate_idxcomp[fa].data(), L.idx_comp,
                     refstate_idxcomp[fa].size() * sizeof(uint16_t),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  refstate idx_comp D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      if (!WriteAndVerifyBytes(
              work_dir + "/refstate_idxcomp_fa" + std::to_string(fa) + ".bin",
              refstate_idxcomp[fa].data(),
              refstate_idxcomp[fa].size() * sizeof(uint16_t))) {
        std::printf("  refstate idx_comp write failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      fa++;
    }
    log += "refstate boundary=P+1=" + std::to_string(boundary) +
           " host/rope/pt/kv/idx captured\n";
  }

  // 10b. Append-only verification: the T4 batch must not disturb committed
  //      entries (positions 0..P-1). Pre-batch reference (section 4b) vs
  //      post-batch live state (section 10). This upgrades the former
  //      "page/slot arithmetic shows" assumption to a checked invariant.
  {
    {
      // Axis-wise: pre_rope has stride prefix_len/axis ([0..P-1]),
      // refstate_rope has stride boundary/axis ([0..P]); a flat compare
      // would misalign axes 1/2. Compare the old region [0..P-1] per axis.
      bool rope_ok = true;
      for (int axis = 0; axis < 3; ++axis) {
        rope_ok = rope_ok &&
                  std::equal(pre_rope.begin() +
                                 static_cast<size_t>(axis) * prefix_len,
                             pre_rope.begin() +
                                 static_cast<size_t>(axis + 1) * prefix_len,
                             refstate_rope.begin() +
                                 static_cast<size_t>(axis) * boundary);
      }
      log_check("appendonly rope_pos old[0..P-1] pre_vs_post ", rope_ok);
    }
    for (int fa = 0; fa < static_cast<int>(full_attn.size()); ++fa) {
      log_check("appendonly page_table fa" + std::to_string(fa) +
                    " old[0..P-1] pre_vs_post ",
                std::equal(pre_pt[fa].begin(), pre_pt[fa].end(),
                           refstate_pt[fa].begin()));
      log_check("appendonly idx_raw fa" + std::to_string(fa) +
                    " old[0..P-1] pre_vs_post ",
                std::equal(pre_idxraw[fa].begin(), pre_idxraw[fa].end(),
                           refstate_idxraw[fa].begin()));
      log_check("appendonly idx_comp fa" + std::to_string(fa) +
                    " groups[0..G) pre_vs_post ",
                std::equal(pre_idxcomp[fa].begin(), pre_idxcomp[fa].end(),
                           refstate_idxcomp[fa].begin()));
      log_check("appendonly kv fa" + std::to_string(fa) +
                    " old[0..P-1] pre_vs_post ",
                std::equal(pre_kv[fa].begin(), pre_kv[fa].end(),
                           refstate_kv[fa].begin()));
    }
    log += "appendonly checks: pre-batch vs post-batch old region [0..P-1]\n";
  }

  // 11. T4 KV/indexer rows at positions P..P+3 (always): the 4 rows the
  //     batch wrote. Position P overlaps the last refstate row (consistency
  //     check); P+1..P+3 are future rows (NOT valid for the restored state).
  {
    int fa = 0;
    for (const auto& fi : full_attn) {
      const auto& L = st.m.layers[fi.layer_idx];
      const size_t slot = static_cast<size_t>(fi.nkv) * 2 * fi.hd;
      for (int r = 0; r < kT; ++r) {
        const int pos = prefix_len + r;
        int page = 0;
        if (cudaMemcpy(&page, L.page_table + pos, sizeof(int),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  t4 page_table D2H failed (fa %d pos %d)\n", fa, pos);
          TeardownModel(&st);
          return false;
        }
        const int off = page * kKvPageSize + (pos % kKvPageSize);
        std::vector<uint16_t> kv(slot);
        if (cudaMemcpy(kv.data(), L.kv_cache + static_cast<size_t>(off) * slot,
                       slot * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  t4 KV D2H failed (fa %d pos %d)\n", fa, pos);
          TeardownModel(&st);
          return false;
        }
        if (!WriteAndVerifyBytes(
                work_dir + "/t4kv_fa" + std::to_string(fa) + "_pos" +
                    std::to_string(pos) + ".bin",
                kv.data(), kv.size() * sizeof(uint16_t))) {
          std::printf("  t4 KV write failed (fa %d pos %d)\n", fa, pos);
          TeardownModel(&st);
          return false;
        }
        if (r == 0) {
          const std::vector<uint16_t> last_row(
              refstate_kv[fa].end() - slot, refstate_kv[fa].end());
          if (kv != last_row) {
            log += "t4_consistency fa" + std::to_string(fa) +
                   " posP_kv_vs_refstate_last_row DIFF\n";
            diff_count++;
          }
        }
        std::vector<uint16_t> ir(fi.idx_hd);
        if (cudaMemcpy(ir.data(),
                       L.idx_raw + static_cast<size_t>(pos) * fi.idx_hd,
                       fi.idx_hd * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  t4 idx_raw D2H failed (fa %d pos %d)\n", fa, pos);
          TeardownModel(&st);
          return false;
        }
        if (!WriteAndVerifyBytes(
                work_dir + "/t4idxraw_fa" + std::to_string(fa) + "_pos" +
                    std::to_string(pos) + ".bin",
                ir.data(), ir.size() * sizeof(uint16_t))) {
          std::printf("  t4 idx_raw write failed (fa %d pos %d)\n", fa, pos);
          TeardownModel(&st);
          return false;
        }
      }
      fa++;
    }
    log += "t4 rows pos " + std::to_string(prefix_len) + ".." +
           std::to_string(prefix_len + kT - 1) + " KV/idx_raw captured\n";
  }

  // 12. Production restore: ModelRestoreCheckpoint(ckpt_idx=0).
  Status sr = ModelRestoreCheckpoint(st.m, 0, st.stream, 0);
  if (!sr.ok()) {
    std::printf("  restore failed: %s\n", sr.message().c_str());
    TeardownModel(&st);
    return false;
  }
  if (cudaStreamSynchronize(st.stream) != cudaSuccess) {
    std::printf("  restore stream sync failed\n");
    TeardownModel(&st);
    return false;
  }

  // 13. Restored live recurrent state (SSM/conv/PLE) + restore checks.
  {
    int li = 0;
    int pi = 0;
    for (const auto& l : st.m.layers) {
      if (!l.is_full_attention) {
        const size_t ssm_e = dims.ssm_elems[li];
        std::vector<float> h(ssm_e);
        if (cudaMemcpy(h.data(), l.ssm_state, ssm_e * sizeof(float),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  restored SSM D2H failed (lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        const std::string path = work_dir + "/restore_ssm_lin" +
                                 std::to_string(li) + ".bin";
        if (!WriteAndVerifyBytes(path, h.data(), h.size() * sizeof(float))) {
          std::printf("  restored SSM write failed (lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        const float* ck = st.m.d_verify_ssm_ckpt +
                          (static_cast<size_t>(li) * cap + 0) * ssm_e;
        std::vector<float> hck(ssm_e);
        if (cudaMemcpy(hck.data(), ck, ssm_e * sizeof(float),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  ckpt SSM row0 D2H failed (restore, lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        log_check("restore_check ssm lin" + std::to_string(li) +
                      " ckpt_vs_restored ",
                  std::memcmp(hck.data(), h.data(), ssm_e * sizeof(float)) ==
                  0);
        if (capture_on) {
          log_check("restore_xcheck ssm lin" + std::to_string(li) +
                        " capture_vs_restored ",
                    std::memcmp(ssm_cap_host[li].data(), h.data(),
                                ssm_e * sizeof(float)) == 0);
        }
        const size_t conv_e = dims.conv_elems[li];
        std::vector<uint16_t> hc(conv_e);
        if (cudaMemcpy(hc.data(), l.conv_state, conv_e * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  restored conv D2H failed (lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        const std::string pathc = work_dir + "/restore_conv_lin" +
                                  std::to_string(li) + ".bin";
        if (!WriteAndVerifyBytes(pathc, hc.data(),
                                 hc.size() * sizeof(uint16_t))) {
          std::printf("  restored conv write failed (lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        const uint16_t* ckc =
            st.m.d_verify_conv_ckpt +
            (static_cast<size_t>(li) * cap + 0) * conv_e;
        std::vector<uint16_t> hckc(conv_e);
        if (cudaMemcpy(hckc.data(), ckc, conv_e * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  ckpt conv row0 D2H failed (restore, lin %d)\n", li);
          TeardownModel(&st);
          return false;
        }
        log_check("restore_check conv lin" + std::to_string(li) +
                      " ckpt_vs_restored ",
                  std::memcmp(hckc.data(), hc.data(),
                              conv_e * sizeof(uint16_t)) == 0);
        li++;
      }
      if (l.has_ple) {
        const size_t ple_e = dims.ple_conv_elems[pi];
        std::vector<uint16_t> h(ple_e);
        if (cudaMemcpy(h.data(), l.ple_conv_state, ple_e * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  restored PLE D2H failed (ple %d)\n", pi);
          TeardownModel(&st);
          return false;
        }
        const std::string path = work_dir + "/restore_ple_ple" +
                                 std::to_string(pi) + ".bin";
        if (!WriteAndVerifyBytes(path, h.data(),
                                 h.size() * sizeof(uint16_t))) {
          std::printf("  restored PLE write failed (ple %d)\n", pi);
          TeardownModel(&st);
          return false;
        }
        const uint16_t* ck =
            st.m.d_verify_ple_conv_ckpt +
            (static_cast<size_t>(pi) * cap + 0) * ple_e;
        std::vector<uint16_t> hck(ple_e);
        if (cudaMemcpy(hck.data(), ck, ple_e * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  ckpt PLE row0 D2H failed (restore, ple %d)\n", pi);
          TeardownModel(&st);
          return false;
        }
        log_check("restore_check ple ple" + std::to_string(pi) +
                      " ckpt_vs_restored ",
                  std::memcmp(hck.data(), h.data(),
                              ple_e * sizeof(uint16_t)) == 0);
        pi++;
      }
    }
  }

  // 14. Post-restore KV region [0..P] vs pre-restore reference (always): the
  //     production restore is recurrent-only and must not disturb the valid
  //     paged region.
  {
    int fa = 0;
    for (const auto& fi : full_attn) {
      const auto& L = st.m.layers[fi.layer_idx];
      const size_t slot = static_cast<size_t>(fi.nkv) * 2 * fi.hd;
      std::vector<int> pt(boundary);
      if (cudaMemcpy(pt.data(), L.page_table,
                     static_cast<size_t>(boundary) * sizeof(int),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        std::printf("  post-restore page_table D2H failed (fa %d)\n", fa);
        TeardownModel(&st);
        return false;
      }
      std::vector<uint16_t> kv(static_cast<size_t>(boundary) * slot);
      for (int p = 0; p < boundary; ++p) {
        const int off = pt[p] * kKvPageSize + (p % kKvPageSize);
        if (cudaMemcpy(&kv[static_cast<size_t>(p) * slot],
                       L.kv_cache + static_cast<size_t>(off) * slot,
                       slot * sizeof(uint16_t),
                       cudaMemcpyDeviceToHost) != cudaSuccess) {
          std::printf("  post-restore KV D2H failed (fa %d pos %d)\n", fa, p);
          TeardownModel(&st);
          return false;
        }
      }
      log_check("restore_check kv fa" + std::to_string(fa) +
                    " region[0..P] pre_vs_post ",
                kv == refstate_kv[fa]);
      fa++;
    }
  }

  // 15. Host state after the restore.
  log += "host_position_after_restore=" + std::to_string(st.seq.position) +
         " history_size=" + std::to_string(st.seq.history.size()) +
         " (production restore is recurrent-only; host/KV rollback is the "
         "caller's responsibility in the MTP flow)\n";

  // 16. Explicit limitations (reported, not silent).
  log += "LIMITATION: old-prefix KV/indexer (pos 0..P-1) reference is the "
         "live cache after the T4 batch (page/slot arithmetic: T4 writes "
         "only pos P..P+3 slots, so 0..P-1 is unchanged by it); not "
         "independently re-derived\n";
  log += "LIMITATION: production restore restores recurrent state only; "
         "host position/history rollback and KV invalidation are the "
         "caller's responsibility in the MTP flow and are not exercised "
         "here\n";
  log += "diff_count=" + std::to_string(diff_count) + "\n";

  if (!WriteAndVerifyText(work_dir + "/writer_row0_log.txt", log)) {
    std::printf("  log write failed\n");
    TeardownModel(&st);
    return false;
  }
  std::printf("  writer_row0 done: P=%d capture=%d diff_count=%d\n", base,
              capture_on ? 1 : 0, diff_count);
  for (auto& d : ssm_cap_dev)
    if (d) cudaFree(d);
  for (auto& d : qkv_raw_cap_dev)
    if (d) cudaFree(d);
  for (auto& d : gated_n_cap_dev)
    if (d) cudaFree(d);
  TeardownModel(&st);
  return diff_count == 0;
}
