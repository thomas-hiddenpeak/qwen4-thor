// New bounded reproduction + six-cell diagnosis (2026-10-11, user-authorized
// through the coordination thread). See
// .q4t-work/mtp-correctness-contract.md (top section) for the round boundary.
//
// mtp_fullstate_roundtrip (mechanism verification, run FIRST):
//   Verifies the full-valid-state save/restore mechanism is a bit-exact
//   round trip before it is trusted by the other two tests.
//
// mtp_repro_256 (Part A, new reproduction baseline):
//   One 256-step B1 vs T4 row-0 diagnostic with per-token logging (full
//   common prefix, inputs, position, per-step status) and a fixture written
//   at the first greedy divergence. This is a NEW baseline on the current
//   binary; it is NOT a reproduction of the old fe80f004 run (that binary
//   was not found in the checked archives). If no divergence in 256 steps,
//   the run is recorded as not-reproduced and the range is not extended.
//
// mtp_sixcell (Part B, six-cell matrix):
//   On the fixed prefix P from the fixture, construct two full valid states
//   (S_B1 via B1 replay; S_T4 via batched/partial-rollback), cross-check
//   their components, then run six continuations
//   {S_B1, S_T4} x {B1[b], T4[b,u,v,w], T4[b,u',v',w']}, comparing row 0
//   only. Suffix tokens are fixed before the run (from the fixture), never
//   generated from output argmax. No 0.02 acceptance; measurements only.
//
// Full-valid-state coverage statement: position/history/seq_id (host),
// rope_delta, d_rope_pos rows [0..P-1], per-linear-layer ssm/conv, PLE
// ple_conv, and per full-attention layer page_table[0..P-1] + KV slots for
// positions [0..P-1] + idx_raw/idx_comp rows [0..P-1]. Future unused
// storage (beyond P) is neither saved nor compared. The existing
// ModelSnapshotState is RECURRENT-ONLY and is never called full here.
#include "q4t/model/model.h"
#include "q4t/test.h"
#include "q4t/text/tokenizer.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
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
using q4t::model::ModelSnapshotState;
using q4t::model::ModelRestoreState;
using q4t::model::ModelSnapshotCheckpoint;
using q4t::model::ModelStateSnapshot;
using q4t::model::kKvPageSize;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";
const char* kDefaultPromptPath =
    "tests/fixtures/mtp-verify-1k-prompt.txt";
const char* kDefaultWorkDir = ".q4t-work/mtp-repro-20261011";

bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}

struct Top2 {
  int id = 0;
  float value = -1e30f;
  int runner_up_id = -1;
  float runner_up = -1e30f;
  float gap() const { return value - runner_up; }
};

// Greedy argmax with lowest-index tie-break; also records the top-2.
Top2 Top2Bf16(const uint16_t* lg, int vocab) {
  Top2 t;
  for (int v = 0; v < vocab; ++v) {
    const float x = Bf16ToFloat(lg[v]);
    if (x > t.value) {
      t.runner_up_id = t.id;
      t.runner_up = t.value;
      t.value = x;
      t.id = v;
    } else if (x > t.runner_up) {
      t.runner_up_id = v;
      t.runner_up = x;
    }
  }
  return t;
}

bool AllFiniteBf16(const uint16_t* lg, int n) {
  for (int i = 0; i < n; ++i)
    if (!std::isfinite(Bf16ToFloat(lg[i]))) return false;
  return true;
}

// Pairwise error measurement with zero-reference-norm handling: when the
// reference (b) has zero norm, l2_rel is undefined and only max_abs is
// reported (ref_zero_norm=true).
struct PairMetric {
  float l2_rel = 0.0f;
  float max_abs = 0.0f;
  bool ref_zero_norm = false;
};

PairMetric PairMetricBf16(const uint16_t* a, const uint16_t* b, int n) {
  double num = 0.0, den = 0.0;
  float mad = 0.0f;
  for (int i = 0; i < n; ++i) {
    const float x = Bf16ToFloat(a[i]);
    const float y = Bf16ToFloat(b[i]);
    const double d = static_cast<double>(x) - static_cast<double>(y);
    num += d * d;
    den += static_cast<double>(y) * static_cast<double>(y);
    mad = std::max(mad, std::fabs(x - y));
  }
  PairMetric pm;
  pm.max_abs = mad;
  if (den <= 0.0) {
    pm.ref_zero_norm = true;
    return pm;
  }
  pm.l2_rel = static_cast<float>(std::sqrt(num / den));
  return pm;
}

std::string ReadFileText(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return std::string();
  return std::string((std::istreambuf_iterator<char>(in)),
                     std::istreambuf_iterator<char>());
}


// Write text to path and verify the write: flush + stream state + close
// status + full re-read byte compare. Returns false on any failure.
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

// Write bytes to path and verify the write: flush + stream state + close
// status + full re-read byte compare. Returns false on any failure.
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

// ---------------------------------------------------------------------------
// Full valid state (recurrent + paged valid region + host). See the file
// header for the coverage statement.
// ---------------------------------------------------------------------------
struct FullState {
  int prefix_len = 0;  // P: valid positions [0..P-1]; == position == history.size()
  int position = 0;
  int seq_id = 0;
  ModelSequence::Stage stage = ModelSequence::Stage::kDecode;  // actual stage
  std::vector<int32_t> history;
  std::vector<int> rope_delta;
  std::vector<int> rope_pos;  // d_rope_pos[seq][3][0..P-1]
  std::vector<uint8_t> recurrent;  // ModelSnapshotState bytes
  // Per full-attention layer (in layer order):
  std::vector<std::vector<int>> page_tables;      // [0..P-1]
  std::vector<std::vector<uint16_t>> kv_slots;    // P slots of [nkv,2,hd]
  std::vector<std::vector<uint16_t>> idx_raw;     // P rows of idx_hd
  std::vector<std::vector<uint16_t>> idx_comp;    // floor(P/compress) valid groups of idx_hd
};

Status SaveFullState(const Model& m, const ModelSequence& seq, int prefix_len,
                     cudaStream_t stream, FullState* fs) {
  if (seq.HasPending()) {
    return Status::Fail(
        "fullstate: sequence has pending (uncommitted) work; cannot save a "
        "valid state");
  }
  // The valid full state stops exactly at the committed position. "pending"
  // means ModelSequence::HasPending() (uncommitted async submission) and
  // "slot" means seq_id — NOT future positions a batched forward will write.
  // prefix_len must equal the committed position and the history length.
  if (prefix_len != seq.position ||
      static_cast<int>(seq.history.size()) != seq.position || prefix_len < 0) {
    return Status::Fail(
        "fullstate: prefix_len != position != history.size(); the valid state "
        "must stop at the committed position");
  }
  // This helper copies KV/page-table/indexer/recurrent as slot 0 only; assert
  // the single-sequence, slot-0 configuration (no multi-slot extension).
  if (m.cfg.max_seq != 1 || seq.seq_id != 0) {
    return Status::Fail(
        "fullstate: helper is slot-0 only; requires max_seq==1 && seq_id==0");
  }
  fs->prefix_len = prefix_len;
  fs->position = seq.position;
  fs->seq_id = seq.seq_id;
  fs->stage = seq.stage;  // actual stage, not assumed kDecode
  fs->history = seq.history;
  fs->rope_delta = m.rope_delta;
  const int ml = m.cfg.max_len;
  // d_rope_pos is [max_seq, 3, max_len]: for this sequence the three MRoPE
  // axes (t,h,w) are rows of length max_len, NOT a contiguous 3*P block.
  // Copy each axis separately with stride max_len, indexed by seq_id.
  const int seq_id = seq.seq_id;
  fs->rope_pos.resize(static_cast<size_t>(3) * prefix_len);
  if (prefix_len > 0) {
    const int* base =
        m.d_rope_pos + static_cast<size_t>(seq_id) * 3 * static_cast<size_t>(ml);
    for (int axis = 0; axis < 3; ++axis) {
      if (cudaMemcpy(&fs->rope_pos[static_cast<size_t>(axis) * prefix_len],
                     base + static_cast<size_t>(axis) * ml,
                     static_cast<size_t>(prefix_len) * sizeof(int),
                     cudaMemcpyDeviceToHost) != cudaSuccess) {
        return Status::Fail("fullstate: rope_pos D2H");
      }
    }
  }
  ModelStateSnapshot snap;
  Status s = ModelSnapshotState(m, &snap, stream);
  if (!s.ok()) return s;
  fs->recurrent.assign(snap.data.begin(), snap.data.end());
  fs->page_tables.clear();
  fs->kv_slots.clear();
  fs->idx_raw.clear();
  fs->idx_comp.clear();
  for (int li = 0; li < m.cfg.num_layers; ++li) {
    const auto& L = m.layers[li];
    if (!L.is_full_attention) continue;
    const int nkv = L.full.nkv;
    const int hd = L.full.hd;
    const int idx_hd = L.full.idx_head_dim;
    std::vector<int> pt(prefix_len);
    if (prefix_len > 0 &&
        cudaMemcpy(pt.data(), L.page_table,
                   static_cast<size_t>(prefix_len) * sizeof(int),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return Status::Fail("fullstate: page_table D2H");
    }
    const size_t slot_bytes =
        static_cast<size_t>(nkv) * 2 * hd * sizeof(uint16_t);
    std::vector<uint16_t> kv(static_cast<size_t>(prefix_len) * nkv * 2 * hd);
    for (int p = 0; p < prefix_len; ++p) {
      const int page = pt[p];
      const int off =
          page * kKvPageSize + (p % kKvPageSize);
      if (cudaMemcpy(&kv[static_cast<size_t>(p) * nkv * 2 * hd],
                     L.kv_cache + static_cast<size_t>(off) * nkv * 2 * hd,
                     slot_bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
        return Status::Fail("fullstate: kv slot D2H");
      }
    }
    // idx_raw is per-token (valid rows [0..P-1]); idx_comp is per *group* of
    // `compress` tokens and is only written when a full group completes, so
    // only floor(P/compress) groups are valid. Copy exactly those.
    const int compress = L.full.idx_compress;
    const int comp_groups = prefix_len / compress;  // floor
    std::vector<uint16_t> ir(static_cast<size_t>(prefix_len) * idx_hd);
    std::vector<uint16_t> ic(static_cast<size_t>(comp_groups) * idx_hd);
    if (prefix_len > 0 &&
        cudaMemcpy(ir.data(), L.idx_raw,
                   static_cast<size_t>(prefix_len) * idx_hd * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return Status::Fail("fullstate: idx_raw D2H");
    }
    if (comp_groups > 0 &&
        cudaMemcpy(ic.data(), L.idx_comp,
                   static_cast<size_t>(comp_groups) * idx_hd * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return Status::Fail("fullstate: idx_comp D2H");
    }
    fs->page_tables.push_back(std::move(pt));
    fs->kv_slots.push_back(std::move(kv));
    fs->idx_raw.push_back(std::move(ir));
    fs->idx_comp.push_back(std::move(ic));
  }
  return Status();
}

Status RestoreFullState(Model& m, ModelSequence* seq, const FullState& fs,
                        cudaStream_t stream) {
  if (m.cfg.max_seq != 1 || fs.seq_id != 0) {
    return Status::Fail(
        "fullstate: helper is slot-0 only; requires max_seq==1 && seq_id==0");
  }
  // Reject uncommitted work BEFORE any device write (symmetric to save).
  if (seq->HasPending()) {
    return Status::Fail(
        "fullstate: sequence has pending (uncommitted) work; cannot restore "
        "over it before any device write");
  }
  if (fs.prefix_len != fs.position ||
      static_cast<int>(fs.history.size()) != fs.position) {
    return Status::Fail(
        "fullstate: saved state inconsistent (prefix_len != position != "
        "history.size())");
  }
  const int prefix_len = fs.prefix_len;
  for (size_t c = 0; c < fs.page_tables.size(); ++c) {
    int li = 0;
    size_t seen = 0;
    for (int l = 0; l < m.cfg.num_layers; ++l) {
      if (m.layers[l].is_full_attention) {
        if (seen == c) { li = l; break; }
        ++seen;
      }
    }
    const auto& L = m.layers[li];
    const int nkv = L.full.nkv;
    const int hd = L.full.hd;
    const int idx_hd = L.full.idx_head_dim;
    if (prefix_len > 0 &&
        cudaMemcpy(L.page_table, fs.page_tables[c].data(),
                   static_cast<size_t>(prefix_len) * sizeof(int),
                   cudaMemcpyHostToDevice) != cudaSuccess) {
      return Status::Fail("fullstate: page_table H2D");
    }
    const size_t slot_bytes =
        static_cast<size_t>(nkv) * 2 * hd * sizeof(uint16_t);
    for (int p = 0; p < prefix_len; ++p) {
      const int page = fs.page_tables[c][p];
      const int off = page * kKvPageSize + (p % kKvPageSize);
      if (cudaMemcpy(L.kv_cache + static_cast<size_t>(off) * nkv * 2 * hd,
                     &fs.kv_slots[c][static_cast<size_t>(p) * nkv * 2 * hd],
                     slot_bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
        return Status::Fail("fullstate: kv slot H2D");
      }
    }
    const int compress = L.full.idx_compress;
    const int comp_groups = prefix_len / compress;  // floor; must match save
    if (prefix_len > 0 &&
        cudaMemcpy(L.idx_raw, fs.idx_raw[c].data(),
                   static_cast<size_t>(prefix_len) * idx_hd * 2,
                   cudaMemcpyHostToDevice) != cudaSuccess) {
      return Status::Fail("fullstate: idx_raw H2D");
    }
    if (comp_groups > 0 &&
        cudaMemcpy(L.idx_comp, fs.idx_comp[c].data(),
                   static_cast<size_t>(comp_groups) * idx_hd * 2,
                   cudaMemcpyHostToDevice) != cudaSuccess) {
      return Status::Fail("fullstate: idx_comp H2D");
    }
  }
  // Symmetric to save: d_rope_pos is [max_seq, 3, max_len]; write each axis
  // separately with stride max_len, indexed by seq_id (not a flat 3*P block).
  const int ml = m.cfg.max_len;
  const int seq_id = fs.seq_id;
  if (prefix_len > 0) {
    int* base =
        m.d_rope_pos + static_cast<size_t>(seq_id) * 3 * static_cast<size_t>(ml);
    for (int axis = 0; axis < 3; ++axis) {
      if (cudaMemcpy(base + static_cast<size_t>(axis) * ml,
                     &fs.rope_pos[static_cast<size_t>(axis) * prefix_len],
                     static_cast<size_t>(prefix_len) * sizeof(int),
                     cudaMemcpyHostToDevice) != cudaSuccess) {
        return Status::Fail("fullstate: rope_pos H2D");
      }
    }
  }
  ModelStateSnapshot snap;
  snap.data = fs.recurrent;
  snap.bytes = fs.recurrent.size();
  snap.valid = true;
  Status s = ModelRestoreState(m, snap, stream);
  if (!s.ok()) return s;
  seq->position = fs.position;
  seq->seq_id = fs.seq_id;
  seq->history = fs.history;
  seq->stage = fs.stage;  // actual saved stage
  m.rope_delta = fs.rope_delta;
  return Status();
}

// Component cross-check between two full states. Returns 0 when all valid
// components are bit-identical; prints per-component diff details otherwise.
int CompareFullStates(const FullState& a, const FullState& b,
                      const char* label_a, const char* label_b) {
  int diffs = 0;
  auto report = [&](const char* name, size_t diff_bytes, size_t total,
                    size_t first_off) {
    if (diff_bytes == 0) {
      std::printf("  cmp %-14s %s vs %s: identical (%zu bytes)\n", name,
                  label_a, label_b, total);
      return;
    }
    ++diffs;
    std::printf("  cmp %-14s %s vs %s: DIFF %zu/%zu bytes first_off=%zu\n",
                name, label_a, label_b, diff_bytes, total, first_off);
  };
  auto cmp_bytes = [&](const char* name, const uint8_t* pa,
                       const uint8_t* pb, size_t n) {
    size_t d = 0, first = n;
    for (size_t k = 0; k < n; ++k) {
      if (pa[k] != pb[k]) {
        ++d;
        if (first == n) first = k;
      }
    }
    report(name, d, n, first);
  };
  if (a.position != b.position || a.seq_id != b.seq_id ||
      a.prefix_len != b.prefix_len || a.rope_delta != b.rope_delta ||
      a.stage != b.stage) {
    ++diffs;
    std::printf(
        "  cmp host scalars: DIFF pos=%d/%d seq_id=%d/%d P=%d/%d "
        "stage=%d/%d rope_delta_same=%d\n",
        a.position, b.position, a.seq_id, b.seq_id, a.prefix_len,
        b.prefix_len, static_cast<int>(a.stage), static_cast<int>(b.stage),
        a.rope_delta == b.rope_delta ? 1 : 0);
  } else {
    std::printf("  cmp host scalars: identical (pos=%d seq_id=%d P=%d "
                "stage=%d)\n",
                a.position, a.seq_id, a.prefix_len,
                static_cast<int>(a.stage));
  }
  if (a.history != b.history) {
    ++diffs;
    std::printf("  cmp history: DIFF len=%zu/%zu\n", a.history.size(),
                b.history.size());
  } else {
    std::printf("  cmp history: identical (len=%zu)\n", a.history.size());
  }
  cmp_bytes("rope_pos",
            reinterpret_cast<const uint8_t*>(a.rope_pos.data()),
            reinterpret_cast<const uint8_t*>(b.rope_pos.data()),
            a.rope_pos.size() * sizeof(int));
  cmp_bytes("recurrent",
            reinterpret_cast<const uint8_t*>(a.recurrent.data()),
            reinterpret_cast<const uint8_t*>(b.recurrent.data()),
            a.recurrent.size());
  for (size_t c = 0; c < a.page_tables.size(); ++c) {
    char name[64];
    std::snprintf(name, sizeof(name), "page_table[%zu]", c);
    cmp_bytes(name,
              reinterpret_cast<const uint8_t*>(a.page_tables[c].data()),
              reinterpret_cast<const uint8_t*>(b.page_tables[c].data()),
              a.page_tables[c].size() * sizeof(int));
    std::snprintf(name, sizeof(name), "kv[%zu]", c);
    cmp_bytes(name,
              reinterpret_cast<const uint8_t*>(a.kv_slots[c].data()),
              reinterpret_cast<const uint8_t*>(b.kv_slots[c].data()),
              a.kv_slots[c].size() * sizeof(uint16_t));
    std::snprintf(name, sizeof(name), "idx_raw[%zu]", c);
    cmp_bytes(name,
              reinterpret_cast<const uint8_t*>(a.idx_raw[c].data()),
              reinterpret_cast<const uint8_t*>(b.idx_raw[c].data()),
              a.idx_raw[c].size() * sizeof(uint16_t));
    std::snprintf(name, sizeof(name), "idx_comp[%zu]", c);
    cmp_bytes(name,
              reinterpret_cast<const uint8_t*>(a.idx_comp[c].data()),
              reinterpret_cast<const uint8_t*>(b.idx_comp[c].data()),
              a.idx_comp[c].size() * sizeof(uint16_t));
  }
  return diffs;
}

// ---------------------------------------------------------------------------
// Shared setup.
// ---------------------------------------------------------------------------
struct Setup {
  Model m;
  ModelSequence seq;
  int vocab = 0;
  int prompt_tokens = 0;
  uint16_t* d_plain = nullptr;
  uint16_t* d_batch = nullptr;
  uint16_t* d_prefill = nullptr;
  cudaStream_t stream = nullptr;
  std::vector<int32_t> prompt;
};

bool CheckCandidatesUnset() {
  bool ok = true;
  const char* names[] = {"Q4T_HC_MIX_ROWWISE_GEMV",
                         "Q4T_INPROJ_ROWWISE_GEMV",
                         "Q4T_INPROJ_CAPTURE"};
  for (const char* n : names) {
    if (std::getenv(n) != nullptr) {
      std::printf("  BOUNDARY VIOLATION: %s is set; must be unset\n", n);
      ok = false;
    }
  }
  std::printf("  candidate env check: %s\n", ok ? "unset (ok)" : "FAILED");
  return ok;
}

Status SetupModel(Setup* st, const std::string& prompt_path,
                  int reserve) {
  q4t::text::TokenizerLimits limits;
  std::unique_ptr<q4t::text::Tokenizer> tok;
  Status s = q4t::text::Tokenizer::Load(std::string(kModelDir) +
                                            "/tokenizer.json",
                                        limits, &tok);
  if (!s.ok()) return s;
  const std::string text = ReadFileText(prompt_path);
  if (text.empty())
    return Status::Fail("prompt file unreadable: " + prompt_path);
  std::vector<std::uint32_t> uids;
  s = tok->Encode(text, &uids);
  if (!s.ok()) return s;
  st->prompt.resize(uids.size());
  for (size_t i = 0; i < uids.size(); ++i)
    st->prompt[i] = static_cast<int32_t>(uids[i]);
  st->prompt_tokens = static_cast<int>(uids.size());

  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.ple_sidecar = kPleSidecar;
  cfg.max_prefill = 8192;
  cfg.max_len = 8192;
  cfg.max_seq = 1;
  s = LoadModel(cfg, &st->m, nullptr);
  if (!s.ok()) return s;
  st->vocab = st->m.cfg.vocab;
  st->stream = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&st->d_plain),
                 static_cast<size_t>(st->vocab) * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&st->d_batch),
                 static_cast<size_t>(4) * st->vocab * 2) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&st->d_prefill),
                 static_cast<size_t>(st->prompt_tokens) * st->vocab * 2) !=
          cudaSuccess) {
    st->m.Free();
    return Status::Fail("cudaMalloc failed");
  }
  s = ModelBeginSequence(st->m, &st->seq, st->stream);
  if (!s.ok()) return s;
  s = ModelPrefill(st->m, &st->seq, st->prompt.data(), st->prompt_tokens,
                   st->d_prefill, st->stream);
  if (!s.ok()) return s;
  s = ModelReserveVerifyCheckpoints(st->m, reserve);
  if (!s.ok()) return s;
  return Status();
}

void TeardownModel(Setup* st) {
  cudaFree(st->d_plain);
  cudaFree(st->d_batch);
  cudaFree(st->d_prefill);
  ModelEndSequence(&st->seq);
  st->m.Free();
}

// Fresh sequence + prefill (resets all per-layer state).
Status FreshPrefill(Setup* st) {
  Status s = ModelEndSequence(&st->seq);
  if (!s.ok()) return s;
  s = ModelBeginSequence(st->m, &st->seq, st->stream);
  if (!s.ok()) return s;
  return ModelPrefill(st->m, &st->seq, st->prompt.data(), st->prompt_tokens,
                      st->d_prefill, st->stream);
}

// Fresh sequence + prefill of only the first n prompt tokens (n <
// prompt_tokens). Used to construct the prefix-minus-last state when the
// divergence prefix ends inside the prompt (P == prompt_len): re-feeding the
// last prompt token after a full prefill would be invalid, so the truncated
// prefill is the correct construction from P-1.
Status TruncatedPrefill(Setup* st, int n) {
  if (n <= 0 || n >= st->prompt_tokens)
    return Status::Fail("truncated_prefill: n must be in [1, prompt_tokens)");
  Status s = ModelEndSequence(&st->seq);
  if (!s.ok()) return s;
  s = ModelBeginSequence(st->m, &st->seq, st->stream);
  if (!s.ok()) return s;
  return ModelPrefill(st->m, &st->seq, st->prompt.data(), n, st->d_prefill,
                      st->stream);
}

// ---------------------------------------------------------------------------
// mtp_fullstate_roundtrip: mechanism verification.
// ---------------------------------------------------------------------------
Q4T_TEST(mtp_fullstate_roundtrip) {
  if (!CudaAvailable()) Q4T_SKIP("(skipped: no CUDA device)");
  if (!CheckCandidatesUnset()) return false;
  Setup st;
  Status s = SetupModel(&st, kDefaultPromptPath, 4);
  if (!s.ok()) {
    std::printf("  setup failed: %s\n", s.message().c_str());
    return false;
  }
  std::printf("  prompt_tokens=%d vocab=%d\n", st.prompt_tokens, st.vocab);
  // Decode 3 fixed tokens (B1), save full state F1.
  const int32_t fixed[5] = {42, 43, 44, 45, 46};
  for (int i = 0; i < 3; ++i) {
    s = ModelDecodeStepSeq(st.m, &st.seq, fixed[i], st.d_plain, st.stream);
    if (!s.ok()) {
      std::printf("  setup decode %d failed: %s\n", i, s.message().c_str());
      TeardownModel(&st);
      return false;
    }
  }
  const int P1 = st.seq.position;
  FullState f1;
  s = SaveFullState(st.m, st.seq, P1, st.stream, &f1);
  if (!s.ok()) {
    std::printf("  save F1 failed: %s\n", s.message().c_str());
    TeardownModel(&st);
    return false;
  }
  // Independent RoPE layout check (NOT just save/restore self-consistency):
  // for pure text the 3 MRoPE axes equal the logical position, so the saved
  // rope_pos[axis][p] must equal p. A flat-copy / wrong-stride bug would fail
  // this even if the round trip were self-consistent.
  {
    bool rope_ok = true;
    for (int axis = 0; axis < 3 && rope_ok; ++axis) {
      for (int p = 0; p < P1; ++p) {
        const int v = f1.rope_pos[static_cast<size_t>(axis) * P1 + p];
        if (v != p) {
          std::printf("  rope layout MISMATCH axis=%d p=%d val=%d\n", axis, p,
                      v);
          rope_ok = false;
          break;
        }
      }
    }
    std::printf("  independent rope layout check (pure text axis[p]==p): %s\n",
                rope_ok ? "PASS" : "FAIL");
    if (!rope_ok) {
      TeardownModel(&st);
      return false;
    }
  }
  // Perturb: 2 more B1 tokens; capture the first perturbation logits.
  std::vector<uint16_t> ref_logits(st.vocab);
  for (int i = 0; i < 2; ++i) {
    s = ModelDecodeStepSeq(st.m, &st.seq, fixed[3 + i], st.d_plain, st.stream);
    if (!s.ok()) {
      std::printf("  perturb decode %d failed: %s\n", i, s.message().c_str());
      TeardownModel(&st);
      return false;
    }
    if (i == 0)
      cudaMemcpy(ref_logits.data(), st.d_plain,
                 static_cast<size_t>(st.vocab) * 2,
                 cudaMemcpyDeviceToHost);
  }
  // Restore F1, save F2, compare bit-exactly.
  s = RestoreFullState(st.m, &st.seq, f1, st.stream);
  if (!s.ok()) {
    std::printf("  restore F1 failed: %s\n", s.message().c_str());
    TeardownModel(&st);
    return false;
  }
  FullState f2;
  s = SaveFullState(st.m, st.seq, P1, st.stream, &f2);
  if (!s.ok()) {
    std::printf("  save F2 failed: %s\n", s.message().c_str());
    TeardownModel(&st);
    return false;
  }
  std::printf("  roundtrip component compare (F1 vs F2):\n");
  const int diffs = CompareFullStates(f1, f2, "F1", "F2");
  // Functional check: decode the same perturbation token; logits must be
  // bit-identical to the pre-perturb capture.
  std::vector<uint16_t> re_logits(st.vocab);
  s = ModelDecodeStepSeq(st.m, &st.seq, fixed[3], st.d_plain, st.stream);
  if (!s.ok()) {
    std::printf("  re-decode failed: %s\n", s.message().c_str());
    TeardownModel(&st);
    return false;
  }
  cudaMemcpy(re_logits.data(), st.d_plain,
             static_cast<size_t>(st.vocab) * 2, cudaMemcpyDeviceToHost);
  bool logits_same =
      std::equal(ref_logits.begin(), ref_logits.end(), re_logits.begin());
  std::printf("  functional re-decode logits bit-identical: %s\n",
              logits_same ? "yes" : "NO");
  TeardownModel(&st);
  if (diffs != 0 || !logits_same) {
    std::printf("  FAIL: full-state roundtrip not bit-exact\n");
    return false;
  }
  std::printf("  PASS: full-state save/restore is a bit-exact round trip\n");
  return true;
}

// ---------------------------------------------------------------------------
// Fixture format (line-based, v1).
// ---------------------------------------------------------------------------
std::string FormatIntList(const std::vector<int32_t>& v) {
  std::string out;
  for (size_t i = 0; i < v.size(); ++i) {
    if (i) out += ",";
    out += std::to_string(v[i]);
  }
  return out;
}

bool ParseIntList(const std::string& line, const std::string& key,
                  std::vector<int32_t>* out) {
  const std::string prefix = key + " ";
  const size_t pos = line.find(prefix);
  if (pos == std::string::npos) return false;
  std::string rest = line.substr(pos + prefix.size());
  out->clear();
  for (const auto& tok : [&] {
    std::vector<std::string> parts;
    std::string cur;
    for (char c : rest) {
      if (c == ',') {
        if (!cur.empty()) {
          parts.push_back(cur);
          cur.clear();
        }
      } else {
        cur += c;
      }
    }
    if (!cur.empty()) parts.push_back(cur);
    return parts;
  }()) {
    try {
      out->push_back(std::stoi(tok));
    } catch (...) {
      return false;
    }
  }
  return !out->empty();
}

bool ParseIntField(const std::string& line, const std::string& key,
                   int* out) {
  const std::string prefix = key + " ";
  const size_t pos = line.find(prefix);
  if (pos == std::string::npos) return false;
  *out = std::stoi(line.substr(pos + prefix.size()));
  return true;
}

bool ParseFloatField(const std::string& line, const std::string& key,
                     float* out) {
  const std::string prefix = key + " ";
  const size_t pos = line.find(prefix);
  if (pos == std::string::npos) return false;
  *out = std::stof(line.substr(pos + prefix.size()));
  return true;
}

struct Fixture {
  std::string binary_sha256;
  int reserve = 4;
  int steps = 256;
  std::string prompt_file;
  std::vector<int32_t> prompt_tokens;
  std::vector<int32_t> prefix_tokens;  // prompt + common decode prefix
  int divergence_step = -1;
  bool predeclared = false;
  int input_token_b = -1;
  int plain_argmax = -1;
  int batch_argmax = -1;
  float gap_plain = 0.0f;
  float gap_batch = 0.0f;
  float l2_rel = 0.0f;
  std::vector<int32_t> filler = {42, 42, 42};
  std::vector<int32_t> filler_alt = {43, 43, 43};
};

bool LoadFixture(const std::string& path, Fixture* fx) {
  const std::string text = ReadFileText(path);
  if (text.empty()) return false;
  bool ok = true;
  for (const auto& line : [&] {
    std::vector<std::string> lines;
    std::string cur;
    for (char c : text) {
      if (c == '\n') {
        lines.push_back(cur);
        cur.clear();
      } else {
        cur += c;
      }
    }
    if (!cur.empty()) lines.push_back(cur);
    return lines;
  }()) {
    if (line.rfind("#", 0) == 0) continue;
    if (line.rfind("binary_sha256 ", 0) == 0)
      fx->binary_sha256 = line.substr(16);
    else if (line.rfind("prompt_file ", 0) == 0)
      fx->prompt_file = line.substr(12);
    else if (line.rfind("divergence_step ", 0) == 0)
      ok &= ParseIntField(line, "divergence_step", &fx->divergence_step);
    else if (line.rfind("predeclared ", 0) == 0) {
      int v = 0;
      ok &= ParseIntField(line, "predeclared", &v);
      fx->predeclared = v != 0;
    }
    else if (line.rfind("input_token_b ", 0) == 0)
      ok &= ParseIntField(line, "input_token_b", &fx->input_token_b);
    else if (line.rfind("plain_argmax ", 0) == 0)
      ok &= ParseIntField(line, "plain_argmax", &fx->plain_argmax);
    else if (line.rfind("batch_argmax ", 0) == 0)
      ok &= ParseIntField(line, "batch_argmax", &fx->batch_argmax);
    else if (line.rfind("gap_plain ", 0) == 0)
      ok &= ParseFloatField(line, "gap_plain", &fx->gap_plain);
    else if (line.rfind("gap_batch ", 0) == 0)
      ok &= ParseFloatField(line, "gap_batch", &fx->gap_batch);
    else if (line.rfind("l2_rel ", 0) == 0)
      ok &= ParseFloatField(line, "l2_rel", &fx->l2_rel);
    else if (line.rfind("prompt_tokens ", 0) == 0)
      ok &= ParseIntList(line, "prompt_tokens", &fx->prompt_tokens);
    else if (line.rfind("prefix_tokens ", 0) == 0)
      ok &= ParseIntList(line, "prefix_tokens", &fx->prefix_tokens);
    else if (line.rfind("filler ", 0) == 0)
      ok &= ParseIntList(line, "filler", &fx->filler);
    else if (line.rfind("filler_alt ", 0) == 0)
      ok &= ParseIntList(line, "filler_alt", &fx->filler_alt);
  }
  return ok && !fx->prefix_tokens.empty() && fx->input_token_b >= 0;
}

// ---------------------------------------------------------------------------
// mtp_repro_256 (Part A, new reproduction baseline).
//
// One NEW 256-step B1-vs-T4 row-0 diagnostic with per-token logging. Each
// step saves the full valid state S_i, runs the T4 batch side [t,42,42,42]
// from S_i (save_checkpoints=true, matching the old 256-step diagnostic and
// the production MTP verify path), restores S_i, then runs the plain B1 side
// [t] from S_i, so the common prefix always continues on the plain (B1) side
// and the two sides never generate different prefixes themselves. The full
// common prefix + input are written and flushed BEFORE the forwards; every
// D2H, finiteness, file write and close is checked, and any persistence
// failure fails the test. Execution safety: if the state save fails, the test
// records and stops immediately (no batch forward, no restore of a possibly
// half-filled snapshot); if the batch forward fails, the test stops in the
// error CUDA state (no restore, no plain decode). The first greedy divergence
// (if any) is written as the fixture; 256 steps without a divergence is
// recorded as not-reproduced (range NOT extended) and a fixture is written at
// the pre-declared step 217 as a path diagnostic, explicitly NOT a fault
// reproduction. This is a NEW baseline on the current binary; it is NOT a
// reproduction of the old fe80f004 run (that binary was not found in the
// checked archives).
// ---------------------------------------------------------------------------
Q4T_TEST(mtp_repro_256) {
  if (!CudaAvailable()) Q4T_SKIP("(skipped: no CUDA device)");
  if (!CheckCandidatesUnset()) return false;
  const int kSteps = 256;
  // Pre-declared fixed position for the no-divergence path diagnostic: the
  // old failure step. Using it on the NEW baseline is a path diagnostic only
  // and is explicitly NOT a reproduction of the old fault.
  const int kPredeclaredStep = 217;
  const char* work_env = std::getenv("Q4T_REPRO_DIR");
  const std::string work_dir = work_env ? work_env : kDefaultWorkDir;
  const std::string log_path = work_dir + "/repro256_tokens.log";
  const std::string fixture_path = work_dir + "/fixture.txt";
  const std::string nr_path = work_dir + "/not_reproduced.txt";
  if (!std::filesystem::create_directories(work_dir) &&
      !std::filesystem::exists(work_dir)) {
    std::printf("  cannot create work dir %s\n", work_dir.c_str());
    return false;
  }

  Setup st;
  Status s = SetupModel(&st, kDefaultPromptPath, 4);
  if (!s.ok()) {
    std::printf("  setup failed: %s\n", s.message().c_str());
    return false;
  }
  const char* sha_env = std::getenv("Q4T_REPRO_BINARY_SHA256");
  const std::string sha = sha_env ? sha_env : "not-provided";
  std::ofstream log(log_path, std::ios::binary | std::ios::trunc);
  if (!log) {
    std::printf("  cannot open log %s\n", log_path.c_str());
    TeardownModel(&st);
    return false;
  }
  log << "# q4t-mtp-repro-256 log v2\n";
  log << "binary_sha256 " << sha << "\n";
  log << "model_hash_file " << work_dir << "/model_hashes.txt\n";
  log << "prompt_file " << kDefaultPromptPath << "\n";
  log << "prompt_tokens " << FormatIntList(st.prompt) << "\n";
  log << "config max_seq=1 max_prefill=8192 max_len=8192 reserve=4 "
         "steps=256\n";
  log << "batch_save_checkpoints=1 (actual; matches old 256-step diagnostic "
         "and production MTP verify path)\n";
  log << "order per step: save S_i, T4 batch from S_i, restore S_i, B1 plain "
         "from S_i; common prefix continues on the plain side\n";
  log << "argmax_tie_rule lowest_id (strict > keeps the lowest index on "
         "ties)\n";
  log << "predeclared_step " << kPredeclaredStep
      << " (no-divergence path diagnostic only; NOT a fault reproduction)\n";
  log << "note NEW baseline on current binary; NOT a reproduction of the old "
         "fe80f004 run\n";
  log << "per_token: pre line = step pos input prefix (written before the "
         "forwards); post line = step pos input plain_argmax batch_argmax "
         "gap_plain gap_batch l2_rel finite_plain finite_batch "
         "save/batch/restore/plain actual status\n";
  log.flush();
  if (!log.good()) {
    std::printf("  log header write failed; aborting\n");
    TeardownModel(&st);
    return false;
  }

  // t_0 = argmax of the prefill last-row logits (common-prefix seed).
  std::vector<uint16_t> h_last(st.vocab);
  if (cudaMemcpy(h_last.data(),
                 st.d_prefill +
                     static_cast<size_t>(st.prompt_tokens - 1) * st.vocab,
                 static_cast<size_t>(st.vocab) * 2,
                 cudaMemcpyDeviceToHost) != cudaSuccess) {
    std::printf("  prefill logits D2H failed; aborting\n");
    TeardownModel(&st);
    return false;
  }
  if (!AllFiniteBf16(h_last.data(), st.vocab)) {
    std::printf("  prefill seed logits non-finite; aborting\n");
    TeardownModel(&st);
    return false;
  }
  int32_t t = Top2Bf16(h_last.data(), st.vocab).id;

  std::vector<int32_t> common_prefix = st.prompt;
  int first_divergence = -1;
  int diverge_input = -1;
  int plain_argmax = -1, batch_argmax = -1;
  float gap_plain = 0.0f, gap_batch = 0.0f, div_l2 = 0.0f;

  // Pre-declared position capture (used only in the no-divergence branch).
  bool predeclared_captured = false;
  int32_t pre_input = -1;
  int pre_plain = -1, pre_batch = -1;
  float pre_gap_plain = 0.0f, pre_gap_batch = 0.0f, pre_l2 = 0.0f;
  std::vector<int32_t> pre_prefix;

  for (int i = 0; i < kSteps; ++i) {
    const int base = st.seq.position;
    // 0) BEFORE the forwards: write the full common prefix + input, flush,
    //    and verify the write actually landed.
    log << "pre step=" << i << " pos=" << base << " input=" << t
        << " prefix=" << FormatIntList(common_prefix) << "\n";
    log.flush();
    if (!log.good()) {
      std::printf("  (repro256) log write failed at step %d (pre line); "
                  "aborting\n",
                  i);
      TeardownModel(&st);
      return false;
    }

    // 1) Save the full valid state S_i (committed position == base).
    FullState F;
    Status sf = SaveFullState(st.m, st.seq, base, st.stream, &F);
    if (!sf.ok()) {
      // Execution safety: F may be half-filled. Do NOT run the batch forward
      // and do NOT restore F; record and stop immediately.
      log << "post step=" << i << " pos=" << base << " input=" << t
          << " save=0 batch=- restore=- plain=- status=save_failed "
          << sf.message() << "\n";
      log.flush();
      if (!log.good()) {
        std::printf("  (repro256) log write failed at step %d (failure "
                    "line); aborting\n",
                    i);
      }
      std::printf("  (repro256) step %d SaveFullState failed: %s; stopping "
                  "before any forward\n",
                  i, sf.message().c_str());
      TeardownModel(&st);
      return false;
    }
    // 2) Batch side: T4 [t,42,42,42] from S_i (row 0 = logits at `base`),
    //    save_checkpoints=true (actual value; see log header).
    const int32_t batch_in[4] = {t, 42, 42, 42};
    Status sb = ModelDecodeBatch(st.m, batch_in, 4, base,
                                 st.seq.history.data(),
                                 static_cast<int>(st.seq.history.size()),
                                 st.d_batch, st.stream, nullptr,
                                 /*save_checkpoints=*/true, 0);
    if (!sb.ok()) {
      // Execution safety: the batch forward failed; the CUDA context may be
      // in an error state. Do NOT restore and do NOT run the plain decode.
      log << "post step=" << i << " pos=" << base << " input=" << t
          << " save=1 batch=0 restore=- plain=- status=batch_failed "
          << sb.message() << "\n";
      log.flush();
      if (!log.good()) {
        std::printf("  (repro256) log write failed at step %d (failure "
                    "line); aborting\n",
                    i);
      }
      std::printf("  (repro256) step %d ModelDecodeBatch failed: %s; "
                  "stopping in error state, no restore/plain\n",
                  i, sb.message().c_str());
      TeardownModel(&st);
      return false;
    }
    // 3) Restore S_i so the plain side starts from the same committed state.
    Status sr = RestoreFullState(st.m, &st.seq, F, st.stream);
    if (!sr.ok()) {
      log << "post step=" << i << " pos=" << base << " input=" << t
          << " save=1 batch=1 batch_ckpt=1 restore=0 plain=- "
          << "status=restore_failed " << sr.message() << "\n";
      log.flush();
      if (!log.good()) {
        std::printf("  (repro256) log write failed at step %d (failure "
                    "line); aborting\n",
                    i);
      }
      std::printf("  (repro256) step %d RestoreFullState failed: %s; "
                  "stopping before plain decode\n",
                  i, sr.message().c_str());
      TeardownModel(&st);
      return false;
    }
    // 4) Plain side: B1 [t] from S_i.
    Status sp = ModelDecodeStepSeq(st.m, &st.seq, t, st.d_plain, st.stream);
    if (!sp.ok()) {
      log << "post step=" << i << " pos=" << base << " input=" << t
          << " save=1 batch=1 batch_ckpt=1 restore=1 plain=0 "
          << "status=plain_failed " << sp.message() << "\n";
      log.flush();
      if (!log.good()) {
        std::printf("  (repro256) log write failed at step %d (failure "
                    "line); aborting\n",
                    i);
      }
      std::printf("  (repro256) step %d ModelDecodeStepSeq failed: %s; "
                  "stopping\n",
                  i, sp.message().c_str());
      TeardownModel(&st);
      return false;
    }
    // 5) Row-0 comparison (both are logits at position `base`).
    std::vector<uint16_t> h_plain(st.vocab), h_batch0(st.vocab);
    if (cudaMemcpy(h_plain.data(), st.d_plain, st.vocab * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      std::printf("  (repro256) step %d plain logits D2H failed; aborting\n",
                  i);
      TeardownModel(&st);
      return false;
    }
    if (cudaMemcpy(h_batch0.data(), st.d_batch, st.vocab * 2,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      std::printf("  (repro256) step %d batch row0 D2H failed; aborting\n", i);
      TeardownModel(&st);
      return false;
    }
    const bool fin_p = AllFiniteBf16(h_plain.data(), st.vocab);
    const bool fin_b = AllFiniteBf16(h_batch0.data(), st.vocab);
    Top2 tp = Top2Bf16(h_plain.data(), st.vocab);
    Top2 tb = Top2Bf16(h_batch0.data(), st.vocab);
    PairMetric pm = PairMetricBf16(h_plain.data(), h_batch0.data(), st.vocab);
    log << "post step=" << i << " pos=" << base << " input=" << t
        << " plain_argmax=" << tp.id << " batch_argmax=" << tb.id
        << " gap_plain=" << tp.gap() << " gap_batch=" << tb.gap()
        << " l2_rel=" << pm.l2_rel
        << (pm.ref_zero_norm ? " ref_zero_norm=1" : "")
        << " finite_plain=" << (fin_p ? 1 : 0)
        << " finite_batch=" << (fin_b ? 1 : 0)
        << " save=1 batch=1 batch_ckpt=1 restore=1 plain=1"
        << " status=ok\n";
    log.flush();
    if (!log.good()) {
      std::printf("  (repro256) log write failed at step %d (post line); "
                  "aborting\n",
                  i);
      TeardownModel(&st);
      return false;
    }
    if (!fin_p || !fin_b) {
      std::printf("  (repro256) step %d non-finite logits (plain=%d "
                  "batch=%d); aborting\n",
                  i, fin_p ? 1 : 0, fin_b ? 1 : 0);
      TeardownModel(&st);
      return false;
    }
    std::printf("  (repro256) step %d pos=%d input=%d plain=%d batch=%d "
                "l2=%.6f\n",
                i, base, t, tp.id, tb.id, pm.l2_rel);
    if (i == kPredeclaredStep) {
      predeclared_captured = true;
      pre_input = t;
      pre_plain = tp.id;
      pre_batch = tb.id;
      pre_gap_plain = tp.gap();
      pre_gap_batch = tb.gap();
      pre_l2 = pm.l2_rel;
      pre_prefix = common_prefix;  // prompt + t_0..t_{i-1}
    }
    if (tp.id != tb.id) {
      first_divergence = i;
      diverge_input = t;
      plain_argmax = tp.id;
      batch_argmax = tb.id;
      gap_plain = tp.gap();
      gap_batch = tb.gap();
      div_l2 = pm.l2_rel;
      break;
    }
    common_prefix.push_back(t);
    t = tp.id;  // common prefix continues on the plain (B1) side
  }
  log.flush();
  if (!log.good()) {
    std::printf("  (repro256) log final flush failed; aborting\n");
    TeardownModel(&st);
    return false;
  }
  log.close();
  if (log.fail()) {
    std::printf("  (repro256) log close reported failure; aborting\n");
    TeardownModel(&st);
    return false;
  }

  if (first_divergence < 0) {
    // (a) Not-reproduced record (on disk, checked).
    {
      std::ofstream nr(nr_path, std::ios::binary | std::ios::trunc);
      if (!nr) {
        std::printf("  cannot open %s\n", nr_path.c_str());
        TeardownModel(&st);
        return false;
      }
      nr << "# q4t-mtp-repro-256 not-reproduced record v1\n"
         << "binary_sha256 " << sha << "\n"
         << "prompt_file " << kDefaultPromptPath << "\n"
         << "config max_seq=1 max_prefill=8192 max_len=8192 reserve=4 "
            "steps=256\n"
         << "batch_save_checkpoints=1\n"
         << "argmax_tie_rule lowest_id\n"
         << "result no B1/T4 greedy divergence in 256 steps\n"
         << "range_not_extended=1\n"
         << "note NEW baseline on current binary; NOT a reproduction of the "
            "old fe80f004 run\n";
      nr.flush();
      if (!nr.good()) {
        std::printf("  not-reproduced record write failed; aborting\n");
        TeardownModel(&st);
        return false;
      }
      nr.close();
      if (nr.fail()) {
        std::printf("  not-reproduced record close failed; aborting\n");
        TeardownModel(&st);
        return false;
      }
    }
    // (b) Pre-declared position fixture: path diagnostic, explicitly NOT a
    //     fault reproduction (no divergence occurred in 256 steps).
    if (!predeclared_captured) {
      std::printf("  (repro256) pre-declared step %d was not reached; "
                  "concrete gap, pre-declared fixture not written\n",
                  kPredeclaredStep);
      TeardownModel(&st);
      return false;
    }
    std::ofstream fxout(fixture_path, std::ios::binary | std::ios::trunc);
    if (!fxout) {
      std::printf("  cannot open fixture %s\n", fixture_path.c_str());
      TeardownModel(&st);
      return false;
    }
    fxout << "# q4t-mtp-repro fixture v1 (NEW baseline; not the old fe80f004 "
             "run)\n"
          << "# pre-declared path diagnostic at step " << kPredeclaredStep
          << "; NOT a fault reproduction (no divergence in 256 steps)\n"
          << "binary_sha256 " << sha << "\n"
          << "prompt_file " << kDefaultPromptPath << "\n"
          << "predeclared 1\n"
          << "divergence_step " << kPredeclaredStep << "\n"
          << "input_token_b " << pre_input << "\n"
          << "plain_argmax " << pre_plain << "\n"
          << "batch_argmax " << pre_batch << "\n"
          << "gap_plain " << pre_gap_plain << "\n"
          << "gap_batch " << pre_gap_batch << "\n"
          << "l2_rel " << pre_l2 << "\n"
          << "batch_save_checkpoints 1\n"
          << "argmax_tie_rule lowest_id\n"
          << "prompt_tokens " << FormatIntList(st.prompt) << "\n"
          << "prefix_tokens " << FormatIntList(pre_prefix) << "\n"
          << "filler 42,42,42\n"
          << "filler_alt 43,43,43\n";
    fxout.flush();
    if (!fxout.good()) {
      std::printf("  pre-declared fixture write failed; aborting\n");
      TeardownModel(&st);
      return false;
    }
    fxout.close();
    if (fxout.fail()) {
      std::printf("  pre-declared fixture close failed; aborting\n");
      TeardownModel(&st);
      return false;
    }
    std::printf("  (repro256) no B1/T4 greedy divergence in 256 steps; "
                "recorded as not-reproduced (range not extended); "
                "pre-declared path-diagnostic fixture at step %d -> %s\n",
                kPredeclaredStep, fixture_path.c_str());
    TeardownModel(&st);
    return true;
  }

  // Write the fixture at the first divergence.
  std::ofstream fxout(fixture_path, std::ios::binary | std::ios::trunc);
  if (!fxout) {
    std::printf("  cannot open fixture %s\n", fixture_path.c_str());
    TeardownModel(&st);
    return false;
  }
  fxout << "# q4t-mtp-repro fixture v1 (NEW baseline; not the old fe80f004 "
           "run)\n"
        << "binary_sha256 " << sha << "\n"
        << "prompt_file " << kDefaultPromptPath << "\n"
        << "predeclared 0\n"
        << "divergence_step " << first_divergence << "\n"
        << "input_token_b " << diverge_input << "\n"
        << "plain_argmax " << plain_argmax << "\n"
        << "batch_argmax " << batch_argmax << "\n"
        << "gap_plain " << gap_plain << "\n"
        << "gap_batch " << gap_batch << "\n"
        << "l2_rel " << div_l2 << "\n"
        << "batch_save_checkpoints 1\n"
        << "argmax_tie_rule lowest_id\n"
        << "prompt_tokens " << FormatIntList(st.prompt) << "\n"
        << "prefix_tokens " << FormatIntList(common_prefix) << "\n"
        << "filler 42,42,42\n"
        << "filler_alt 43,43,43\n";
  fxout.flush();
  if (!fxout.good()) {
    std::printf("  fixture write failed; aborting\n");
    TeardownModel(&st);
    return false;
  }
  fxout.close();
  if (fxout.fail()) {
    std::printf("  fixture close failed; aborting\n");
    TeardownModel(&st);
    return false;
  }
  std::printf("  (repro256) first divergence at step %d (pos=%d, input=%d): "
              "plain=%d batch=%d; fixture -> %s\n",
              first_divergence, st.prompt_tokens + first_divergence,
              diverge_input, plain_argmax, batch_argmax, fixture_path.c_str());
  TeardownModel(&st);
  return true;
}

// ---------------------------------------------------------------------------
// WriteFullStateEvidence: persist one FullState's valid components +
// metadata to disk with write verification (flush + stream state + close
// status + re-read byte compare). Host-side only: no model forward, no
// device traffic. Returns false on any failure (caller stops immediately).
// ---------------------------------------------------------------------------
bool WriteFullStateEvidence(const FullState& fs, const std::string& dir,
                            const char* label) {
  std::string meta;
  meta += "# q4t-mtp-fullstate evidence v1\n";
  meta += "label " + std::string(label) + "\n";
  meta += "prefix_len " + std::to_string(fs.prefix_len) + "\n";
  meta += "position " + std::to_string(fs.position) + "\n";
  meta += "seq_id(slot) " + std::to_string(fs.seq_id) + "\n";
  meta += "stage " + std::to_string(static_cast<int>(fs.stage)) + "\n";
  meta += "history " + FormatIntList(fs.history) + "\n";
  meta += "len_rope_delta " + std::to_string(fs.rope_delta.size()) + "\n";
  meta += "rope_delta " + FormatIntList(fs.rope_delta) + "\n";
  meta += "len_rope_pos " + std::to_string(fs.rope_pos.size()) + "\n";
  meta += "len_recurrent " + std::to_string(fs.recurrent.size()) + "\n";
  meta += "n_full_attention_layers " +
          std::to_string(fs.page_tables.size()) + "\n";
  for (size_t c = 0; c < fs.page_tables.size(); ++c) {
    char buf[256];
    std::snprintf(buf, sizeof(buf),
                  "len_page_table[%zu] %zu\nlen_kv[%zu] %zu\n"
                  "len_idx_raw[%zu] %zu\nlen_idx_comp[%zu] %zu\n",
                  c, fs.page_tables[c].size(), c, fs.kv_slots[c].size(), c,
                  fs.idx_raw[c].size(), c, fs.idx_comp[c].size());
    meta += buf;
  }
  if (!WriteAndVerifyText(dir + "/" + std::string(label) + "_meta.txt", meta))
    return false;
  auto write_component = [&](const char* name, const void* data,
                             size_t bytes) {
    if (bytes == 0) return true;  // zero-length: recorded in metadata only
    return WriteAndVerifyBytes(dir + "/" + std::string(label) + "_" + name,
                               data, bytes);
  };
  if (!write_component("rope_pos.bin", fs.rope_pos.data(),
                       fs.rope_pos.size() * sizeof(int)))
    return false;
  if (!write_component("recurrent.bin", fs.recurrent.data(),
                       fs.recurrent.size()))
    return false;
  for (size_t c = 0; c < fs.page_tables.size(); ++c) {
    char name[64];
    std::snprintf(name, sizeof(name), "page_table_%zu.bin", c);
    if (!write_component(name, fs.page_tables[c].data(),
                         fs.page_tables[c].size() * sizeof(int)))
      return false;
    std::snprintf(name, sizeof(name), "kv_%zu.bin", c);
    if (!write_component(name, fs.kv_slots[c].data(),
                         fs.kv_slots[c].size() * sizeof(uint16_t)))
      return false;
    std::snprintf(name, sizeof(name), "idx_raw_%zu.bin", c);
    if (!write_component(name, fs.idx_raw[c].data(),
                         fs.idx_raw[c].size() * sizeof(uint16_t)))
      return false;
    std::snprintf(name, sizeof(name), "idx_comp_%zu.bin", c);
    if (!write_component(name, fs.idx_comp[c].data(),
                         fs.idx_comp[c].size() * sizeof(uint16_t)))
      return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// mtp_sixcell (Part B): six-cell matrix on the fixed prefix P from the
// fixture. Constructs two full valid states at the same logical prefix P
// (S_B1 via B1 replay; S_T4 via batched/partial-rollback: the prefix minus
// its last token, then T4 [last,42,42,42] with save_checkpoints=true, then
// restore checkpoint[0] to accept only `last` — mtp.cu: checkpoint[a] is
// the state after accepting 1+a tokens, so accepting only the first token
// is checkpoint[0]), cross-checks their components, persists both states as
// write-verified evidence (valid components + metadata; no extra forward),
// then runs six continuations {S_B1, S_T4} x {B1[b], T4[b,u,v,w],
// T4[b,u',v',w']} (6 forwards outside state construction), comparing row 0
// only. Suffix tokens are fixed before the run (from the fixture), never
// generated from output argmax. No 0.02 acceptance; measurements only. The
// three causal contrasts are computed from the six saved row-0 logits
// (offline-recomputable from sixcell_row0_*.bin; no additional GPU run).
// When the fixture is the pre-declared no-divergence fixture, this is a path
// diagnostic, explicitly NOT a fault reproduction.
// ---------------------------------------------------------------------------
Q4T_TEST(mtp_sixcell) {
  if (!CudaAvailable()) Q4T_SKIP("(skipped: no CUDA device)");
  if (!CheckCandidatesUnset()) return false;
  const char* work_env = std::getenv("Q4T_REPRO_DIR");
  const std::string work_dir = work_env ? work_env : kDefaultWorkDir;
  const std::string fixture_path = work_dir + "/fixture.txt";
  Fixture fx;
  if (!LoadFixture(fixture_path, &fx)) {
    std::printf("  fixture not found/unreadable: %s (run mtp_repro_256 "
                "first)\n",
                fixture_path.c_str());
    return false;
  }
  std::printf("  fixture: divergence_step=%d b=%d plain=%d batch=%d "
              "prefix_len=%zu predeclared=%d\n",
              fx.divergence_step, fx.input_token_b, fx.plain_argmax,
              fx.batch_argmax, fx.prefix_tokens.size(), fx.predeclared);
  if (fx.predeclared) {
    std::printf("  NOTE: pre-declared path diagnostic (no divergence in the "
                "256-step run); NOT a fault reproduction\n");
  }

  Setup st;
  Status s = SetupModel(&st, fx.prompt_file, 4);
  if (!s.ok()) {
    std::printf("  setup failed: %s\n", s.message().c_str());
    return false;
  }
  if (fx.prompt_tokens != st.prompt) {
    std::printf("  fixture prompt_tokens != setup prompt; aborting\n");
    TeardownModel(&st);
    return false;
  }
  const int P = static_cast<int>(fx.prefix_tokens.size());
  const int32_t b = fx.input_token_b;
  const int32_t u0 = fx.filler.size() > 0 ? fx.filler[0] : 42;
  const int32_t u1 = fx.filler.size() > 1 ? fx.filler[1] : 42;
  const int32_t u2 = fx.filler.size() > 2 ? fx.filler[2] : 42;
  const int32_t a0 = fx.filler_alt.size() > 0 ? fx.filler_alt[0] : 43;
  const int32_t a1 = fx.filler_alt.size() > 1 ? fx.filler_alt[1] : 43;
  const int32_t a2 = fx.filler_alt.size() > 2 ? fx.filler_alt[2] : 43;

  // ---- Construct S_B1: B1 replay of the prefix P. ----
  FullState f_B1;
  {
    Status sf = FreshPrefill(&st);
    if (!sf.ok()) {
      std::printf("  S_B1 prefill failed: %s\n", sf.message().c_str());
      TeardownModel(&st);
      return false;
    }
    for (int i = st.prompt_tokens; i < P; ++i) {
      s = ModelDecodeStepSeq(st.m, &st.seq, fx.prefix_tokens[i], st.d_plain,
                             st.stream);
      if (!s.ok()) {
        std::printf("  S_B1 replay %d failed: %s\n", i, s.message().c_str());
        TeardownModel(&st);
        return false;
      }
    }
    if (st.seq.position != P) {
      std::printf("  S_B1 position %d != P %d\n", st.seq.position, P);
      TeardownModel(&st);
      return false;
    }
    s = SaveFullState(st.m, st.seq, P, st.stream, &f_B1);
    if (!s.ok()) {
      std::printf("  S_B1 save failed: %s\n", s.message().c_str());
      TeardownModel(&st);
      return false;
    }
  }

  // ---- Construct S_T4: batched/partial-rollback at the same prefix P. ----
  FullState f_T4;
  std::string t4_mode;
  {
    if (P < 2) {
      std::printf("  S_T4 cannot be constructed: P=%d < 2 (no valid "
                  "prefix-minus-last); concrete gap, six-cell not run\n",
                  P);
      TeardownModel(&st);
      return false;
    }
    Status sf;
    if (P <= st.prompt_tokens) {
      // Divergence at step 0 (P == prompt_len): the prefix minus its last
      // token is prefilled as ONE T=P-1 chunk. Re-feeding the last prompt
      // token after a full prefill would be invalid, so the truncated
      // prefill is the correct construction from P-1.
      t4_mode =
          "truncated_prefill(P-1)+T4[last,42,42,42]+restore_ckpt0";
      sf = TruncatedPrefill(&st, P - 1);
    } else {
      t4_mode =
          "full_prefill+B1_replay[plen,P-1)+T4[last,42,42,42]"
          "+restore_ckpt0";
      sf = FreshPrefill(&st);
      if (sf.ok()) {
        for (int i = st.prompt_tokens; i < P - 1; ++i) {
          s = ModelDecodeStepSeq(st.m, &st.seq, fx.prefix_tokens[i],
                                 st.d_plain, st.stream);
          if (!s.ok()) {
            sf = s;
            break;
          }
        }
      }
    }
    if (!sf.ok()) {
      std::printf("  S_T4 prefill/replay failed: %s\n", sf.message().c_str());
      TeardownModel(&st);
      return false;
    }
    if (st.seq.position != P - 1) {
      std::printf("  S_T4 position %d != P-1 %d\n", st.seq.position, P - 1);
      TeardownModel(&st);
      return false;
    }
    // T4 [last,42,42,42] from base=P-1 with save_checkpoints=true (actual
    // value; matches the production MTP verify path).
    const int32_t last_tok = fx.prefix_tokens[P - 1];
    const int32_t batch_in[4] = {last_tok, 42, 42, 42};
    s = ModelDecodeBatch(st.m, batch_in, 4, P - 1, st.seq.history.data(),
                         static_cast<int>(st.seq.history.size()), st.d_batch,
                         st.stream, nullptr, /*save_checkpoints=*/true, 0);
    if (!s.ok()) {
      std::printf("  S_T4 batch failed: %s\n", s.message().c_str());
      TeardownModel(&st);
      return false;
    }
    // Partial-rollback: accept only the first token -> checkpoint[0]
    // (checkpoint[a] = state after accepting 1+a tokens).
    s = ModelRestoreCheckpoint(st.m, 0, st.stream, 0);
    if (!s.ok()) {
      std::printf("  S_T4 rollback failed: %s\n", s.message().c_str());
      TeardownModel(&st);
      return false;
    }
    st.seq.position = P;
    st.seq.history = fx.prefix_tokens;
    s = SaveFullState(st.m, st.seq, P, st.stream, &f_T4);
    if (!s.ok()) {
      std::printf("  S_T4 save failed: %s\n", s.message().c_str());
      TeardownModel(&st);
      return false;
    }
  }
  std::printf("  S_T4 construction: %s (save_checkpoints=1 actual, "
              "restore ckpt_idx=0)\n",
              t4_mode.c_str());

  // ---- Cross-check S_B1 vs S_T4 (host-side component comparison). ----
  std::printf("  cross-check S_B1 vs S_T4 (same logical prefix P=%d; "
              "checkpoint/capture fixed: reserve=4, save_checkpoints=1, "
              "restore ckpt[0]):\n",
              P);
  const int xdiffs = CompareFullStates(f_B1, f_T4, "S_B1", "S_T4");
  std::printf("  cross-check total component diffs: %d (expect recurrent + "
              "last-position KV to differ via the T-path; earlier KV "
              "identical)\n",
              xdiffs);

  // ---- Evidence: persist f_B1/f_T4 valid components + metadata BEFORE the
  //      six cells (write-verified; stop on failure; no extra forward). ----
  {
    const std::string ev_dir = work_dir + "/evidence";
    if (!std::filesystem::create_directories(ev_dir) &&
        !std::filesystem::exists(ev_dir)) {
      std::printf("  cannot create evidence dir %s; aborting\n",
                  ev_dir.c_str());
      TeardownModel(&st);
      return false;
    }
    if (!WriteFullStateEvidence(f_B1, ev_dir, "f_B1")) {
      std::printf("  f_B1 evidence write/verify failed; stopping before the "
                  "six cells\n");
      TeardownModel(&st);
      return false;
    }
    if (!WriteFullStateEvidence(f_T4, ev_dir, "f_T4")) {
      std::printf("  f_T4 evidence write/verify failed; stopping before the "
                  "six cells\n");
      TeardownModel(&st);
      return false;
    }
    std::printf("  evidence persisted + verified: %s/{f_B1,f_T4}_* "
                "(valid components + metadata; no extra forward)\n",
                ev_dir.c_str());
  }

  // ---- Six continuations (6 forwards outside state construction). ----
  struct Cell {
    const char* name;
    std::vector<uint16_t> row0;
    Top2 top2;
    bool finite = false;
  };
  std::vector<Cell> cells(6);
  const char* names[6] = {"S_B1+B1[b]", "S_B1+T4[b,u,v,w]",
                          "S_B1+T4[b,u',v',w']", "S_T4+B1[b]",
                          "S_T4+T4[b,u,v,w]", "S_T4+T4[b,u',v',w']"};
  for (int c = 0; c < 6; ++c) cells[c].name = names[c];

  auto run_cell = [&](int c, const FullState& fs, bool use_batch,
                      const int32_t* batch_tokens) -> Status {
    Status sr = RestoreFullState(st.m, &st.seq, fs, st.stream);
    if (!sr.ok()) return sr;
    if (!use_batch) {
      s = ModelDecodeStepSeq(st.m, &st.seq, b, st.d_plain, st.stream);
      if (!s.ok()) return s;
      cells[c].row0.resize(st.vocab);
      if (cudaMemcpy(cells[c].row0.data(), st.d_plain, st.vocab * 2,
                     cudaMemcpyDeviceToHost) != cudaSuccess)
        return Status::Fail("cell D2H");
    } else {
      s = ModelDecodeBatch(st.m, batch_tokens, 4, P, st.seq.history.data(),
                           P, st.d_batch, st.stream, nullptr,
                           /*save_checkpoints=*/true, 0);
      if (!s.ok()) return s;
      cells[c].row0.resize(st.vocab);
      if (cudaMemcpy(cells[c].row0.data(), st.d_batch, st.vocab * 2,
                     cudaMemcpyDeviceToHost) != cudaSuccess)
        return Status::Fail("cell D2H row0");
    }
    cells[c].finite = AllFiniteBf16(cells[c].row0.data(), st.vocab);
    cells[c].top2 = Top2Bf16(cells[c].row0.data(), st.vocab);
    return Status();
  };

  const int32_t batch_u[4] = {b, u0, u1, u2};
  const int32_t batch_a[4] = {b, a0, a1, a2};
  for (int c = 0; c < 6; ++c) {
    const bool use_batch = (c != 0 && c != 3);
    const int32_t* bt = use_batch ? ((c == 1 || c == 4) ? batch_u : batch_a)
                                  : nullptr;
    const FullState& fs = (c < 3) ? f_B1 : f_T4;
    s = run_cell(c, fs, use_batch, bt);
    if (!s.ok()) {
      std::printf("  cell %d failed: %s\n", c, s.message().c_str());
      TeardownModel(&st);
      return false;
    }
  }
  for (int c = 0; c < 6; ++c) {
    if (!cells[c].finite) {
      std::printf("  cell %d (%s) has non-finite logits; aborting\n", c,
                  cells[c].name);
      TeardownModel(&st);
      return false;
    }
  }

  // ---- Report (measurement only; no 0.02 acceptance). ----
  std::printf("  six-cell row-0 results (reference = cell 0 = S_B1+B1[b]):\n");
  for (int c = 0; c < 6; ++c) {
    std::printf("    [%d] %-20s argmax=%d top2=(%d:%.4f, %d:%.4f) gap=%.4f "
                "finite=%d\n",
                c, cells[c].name, cells[c].top2.id, cells[c].top2.id,
                cells[c].top2.value, cells[c].top2.runner_up_id,
                cells[c].top2.runner_up, cells[c].top2.gap(),
                cells[c].finite ? 1 : 0);
  }
  std::printf("  pairwise error vs reference (cell 0):\n");
  for (int c = 1; c < 6; ++c) {
    PairMetric pm =
        PairMetricBf16(cells[c].row0.data(), cells[0].row0.data(), st.vocab);
    std::printf("    [%d] %-20s l2_rel=%.6f max_abs=%.6f%s\n", c,
                cells[c].name, pm.l2_rel, pm.max_abs,
                pm.ref_zero_norm ? " (ref_zero_norm)" : "");
  }
  const bool g1 = std::equal(cells[1].row0.begin(), cells[1].row0.end(),
                             cells[2].row0.begin());
  const bool g2 = std::equal(cells[4].row0.begin(), cells[4].row0.end(),
                             cells[5].row0.begin());
  std::printf("  causal guard (row0 filler-independent): S_B1 %s, S_T4 %s\n",
              g1 ? "PASS" : "FAIL", g2 ? "PASS" : "FAIL");

  // ---- Save all six row-0 logits + a summary to disk (all checked). The
  //      three causal contrasts are offline-recomputable from these files. ----
  for (int c = 0; c < 6; ++c) {
    char path[256];
    std::snprintf(path, sizeof(path), "%s/sixcell_row0_%d.bin",
                  work_dir.c_str(), c);
    if (!WriteAndVerifyBytes(path, cells[c].row0.data(),
                             cells[c].row0.size() * sizeof(uint16_t))) {
      std::printf("  cannot write/verify %s; aborting\n", path);
      TeardownModel(&st);
      return false;
    }
  }
  {
    std::string sp = work_dir + "/sixcell_summary.txt";
    std::ofstream out(sp, std::ios::binary | std::ios::trunc);
    if (!out) {
      std::printf("  cannot open %s; aborting\n", sp.c_str());
      TeardownModel(&st);
      return false;
    }
    out << "# q4t-mtp-sixcell summary v1 (measurement only; no 0.02 "
           "acceptance)\n"
        << "fixture " << fixture_path << "\n"
        << "predeclared " << fx.predeclared
        << (fx.predeclared
               ? " (path diagnostic; NOT a fault reproduction)" : "")
        << "\n"
        << "P=" << P << " b=" << b << " divergence_step="
        << fx.divergence_step << "\n"
        << "S_T4_construction " << t4_mode << "\n"
        << "t4_save_checkpoints=1 (actual; S_T4 construction and all T4 "
           "cells)\n"
        << "t4_restore_ckpt_idx=0 (accept only the first token)\n"
        << "argmax_tie_rule lowest_id (strict > keeps the lowest index on "
           "ties)\n"
        << "three_contrast offline from sixcell_row0_{0..5}.bin; no "
           "additional GPU run\n"
        << "evidence_dir " << work_dir << "/evidence (f_B1/f_T4 valid "
           "components + metadata, write-verified before the six cells)\n"
        << "crosscheck_component_diffs=" << xdiffs << "\n";
    for (int c = 0; c < 6; ++c) {
      out << "cell" << c << " " << cells[c].name << " argmax="
          << cells[c].top2.id << " gap=" << cells[c].top2.gap()
          << " finite=" << (cells[c].finite ? 1 : 0) << "\n";
    }
    for (int c = 1; c < 6; ++c) {
      PairMetric pm =
          PairMetricBf16(cells[c].row0.data(), cells[0].row0.data(), st.vocab);
      out << "pair" << c << "_vs_0 l2_rel=" << pm.l2_rel << " max_abs="
          << pm.max_abs << (pm.ref_zero_norm ? " ref_zero_norm=1" : "")
          << "\n";
    }
    // The three causal contrasts (offline-recomputable from the six saved
    // row-0 logits):
    {
      PairMetric ps = PairMetricBf16(cells[3].row0.data(),
                                     cells[0].row0.data(), st.vocab);
      out << "contrast_state S_T4+B1[b] vs S_B1+B1[b] l2_rel=" << ps.l2_rel
          << " max_abs=" << ps.max_abs
          << (ps.ref_zero_norm ? " ref_zero_norm=1" : "") << "\n";
    }
    {
      PairMetric pp = PairMetricBf16(cells[1].row0.data(),
                                     cells[0].row0.data(), st.vocab);
      out << "contrast_path S_B1+T4[b,u,v,w] vs S_B1+B1[b] l2_rel="
          << pp.l2_rel << " max_abs=" << pp.max_abs
          << (pp.ref_zero_norm ? " ref_zero_norm=1" : "") << "\n";
    }
    out << "contrast_causality cell1==cell2 " << (g1 ? 1 : 0)
        << " cell4==cell5 " << (g2 ? 1 : 0) << "\n";
    out.flush();
    if (!out.good()) {
      std::printf("  sixcell summary write failed; aborting\n");
      TeardownModel(&st);
      return false;
    }
    out.close();
    if (out.fail()) {
      std::printf("  sixcell summary close failed; aborting\n");
      TeardownModel(&st);
      return false;
    }
  }
  std::printf("  six-cell complete (measurement only; no 0.02 acceptance)\n");
  TeardownModel(&st);
  return true;
}

}  // namespace
