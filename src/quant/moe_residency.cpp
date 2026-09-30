// Per-layer tiered expert residency (see moe_residency.h).
#include "q4t/quant/moe_residency.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <thread>

#include "q4t/quant/swizzle.h"

namespace q4t {
namespace quant {

namespace {

std::string ExpertName(int layer_id, int expert, const char* proj,
                       const char* suffix) {
  return "model.language_model.layers." + std::to_string(layer_id) +
         ".mlp.experts." + std::to_string(expert) + "." + proj + "." + suffix;
}

Status H2D(const void* src, void* dst, size_t bytes, cudaStream_t stream) {
  if (bytes == 0) return Status();
  if (cudaMemcpyAsync(dst, src, bytes, cudaMemcpyHostToDevice, stream) !=
      cudaSuccess) {
    return Status::Fail(std::string("residency H2D failed: ") +
                        cudaGetErrorString(cudaGetLastError()));
  }
  return Status();
}

}  // namespace

int MoEResidencyLoadThreads() {
  // Default 8: the range-read fast path issues few, large, contiguous
  // preads per expert, so more parallel workers saturate the NVMe without
  // the random-read latency pile-up the old 10-small-read path had.
  int n = 8;
  if (const char* env = std::getenv("Q4T_MOE_LOAD_THREADS")) {
    const int v = std::atoi(env);
    if (v > 0) n = v;
  }
  return std::max(1, std::min(n, MoEResidency::kMaxLoadThreads));
}

size_t MoEResidencyLayerBytes(int hs, int moe_is, int C) {
  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);
  size_t b = 0;
  b += static_cast<size_t>(2 * C * moe_is) * (hs / 2);  // gu_packed
  b += static_cast<size_t>(C) * gu_sf_block;
  b += static_cast<size_t>(C * hs) * (moe_is / 2);  // dn_packed
  b += static_cast<size_t>(C) * dn_sf_block;
  b += static_cast<size_t>(4 * C) * sizeof(float);  // scalar scales
  return b;
}

size_t MoEResidencyStagingBytes(int hs, int moe_is) {
  const size_t gu_w_bytes = static_cast<size_t>(moe_is) * (hs / 2);
  const size_t gu_s_bytes = static_cast<size_t>(moe_is) * (hs / 16);
  const size_t dn_w_bytes = static_cast<size_t>(hs) * (moe_is / 2);
  const size_t dn_s_bytes = static_cast<size_t>(hs) * (moe_is / 16);
  return 2 * gu_w_bytes + dn_w_bytes + 2 * gu_s_bytes + dn_s_bytes +
         2 * gu_s_bytes + SfBufferSize(2 * moe_is, hs) +
         SfBufferSize(hs, moe_is) + 4 * sizeof(float);
}

Status MoEResidency::Init(const io::WeightLoader& loader, int layer_id,
                          int E, int hs, int moe_is, int C,
                          cudaStream_t stream) {
  if (E <= 0 || hs <= 0 || moe_is <= 0 || C <= 0 || C > E) {
    return Status::Fail("invalid residency dims");
  }
  if ((2 * moe_is) % 128 != 0 || (hs % 128) != 0) {
    return Status::Fail(
        "2*moe_is and hs must be multiples of 128 (per-expert SF blocks)");
  }
  loader_ = &loader;
  layer_id_ = layer_id;
  E_ = E;
  hs_ = hs;
  moe_is_ = moe_is;
  C_ = C;
  load_threads_ = MoEResidencyLoadThreads();

  layout_.E = C;
  layout_.hs = hs;
  layout_.moe_is = moe_is;
  layout_.slot_mode = true;

  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);
  const size_t scal_bytes = static_cast<size_t>(C) * sizeof(float);
  auto alloc = [&](void** p, size_t bytes) -> Status {
    if (cudaMalloc(p, bytes) != cudaSuccess) {
      return Status::Fail(std::string("residency cudaMalloc failed (") +
                          std::to_string(bytes) + " bytes)");
    }
    return Status();
  };
  Status s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_packed),
                  static_cast<size_t>(2 * C * moe_is) * (hs / 2))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_sf),
                  static_cast<size_t>(C) * gu_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_packed),
                  static_cast<size_t>(C * hs) * (moe_is / 2))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_sf),
                  static_cast<size_t>(C) * dn_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.gu_input_scale),
                  scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&layout_.dn_input_scale),
                  scal_bytes)))
    return s;

  layout_.gu_w_scale2_h.assign(C, 0.0f);
  layout_.gu_input_scale_h.assign(C, 0.0f);
  layout_.dn_w_scale2_h.assign(C, 0.0f);
  layout_.dn_input_scale_h.assign(C, 0.0f);

  slot_expert_.assign(C, -1);
  slot_tick_.assign(C, 0);
  slot_protected_.assign(C, 0);
  expert_slot_.assign(E, -1);
  resident_count_ = 0;
  protected_count_ = 0;

  // Pinned staging pool, one buffer per load worker (see header): a worker
  // reuses its own buffer for the next expert; reusing it waits
  // (cudaEventSynchronize) for the H2D that last used it.
  staging_bytes_ = MoEResidencyStagingBytes(hs, moe_is);
  staging_.assign(load_threads_, nullptr);
  staging_in_flight_.assign(load_threads_, false);
  for (int t = 0; t < load_threads_; ++t) {
    if (cudaHostAlloc(&staging_[t], staging_bytes_,
                      cudaHostAllocDefault) != cudaSuccess) {
      return Status::Fail("residency pinned staging alloc failed");
    }
    if (cudaEventCreateWithFlags(&staging_event_[t],
                                 cudaEventDisableTiming) != cudaSuccess) {
      return Status::Fail("residency staging event create failed");
    }
  }
  inited_ = true;
  (void)stream;
  return Status();
}

// Stage one expert (read + merge + swizzle) into worker `t`'s staging
// buffer. Waits first for the H2D that last used that buffer so the host
// writes cannot race an in-flight copy of the previous expert.
Status MoEResidency::StageExpert(int expert, int t) const {
  if (expert < 0 || expert >= E_) {
    return Status::Fail("expert out of range");
  }
  // Per-projection byte sizes; down/gate/up are equal by construction
  // (hs*moe_is/2 weights, hs*moe_is/16 SF).
  const size_t w_bytes = static_cast<size_t>(hs_) * (moe_is_ / 2);
  const size_t s_bytes = static_cast<size_t>(hs_) * (moe_is_ / 16);
  const size_t gu_sf_block = layout_.gu_sf_block();
  const size_t dn_sf_block = layout_.dn_sf_block();

  if (staging_in_flight_[t]) {
    if (cudaEventSynchronize(staging_event_[t]) != cudaSuccess) {
      return Status::Fail("residency staging wait failed");
    }
    staging_in_flight_[t] = false;
  }
  // Staging is in CHECKPOINT file order so the range fast path can read
  // straight into it: weights [dn|ga|up] (w_bytes each), then SF
  // [dn|ga|up] packed at s_bytes each, the gate+up SF merge scratch, the
  // swizzled SF blocks, and the scalar scales. Total bytes equal
  // MoEResidencyStagingBytes (3*w + 5*s + sf blocks + 16); the SF region
  // must stay packed or the tail overflows the pinned buffer.
  uint8_t* p = staging_[t];
  uint8_t* w_dn = p;
  uint8_t* w_ga = p + w_bytes;
  uint8_t* w_up = p + 2 * w_bytes;
  uint8_t* s_dn = p + 3 * w_bytes;
  uint8_t* s_ga = s_dn + s_bytes;
  uint8_t* s_up = s_ga + s_bytes;
  uint8_t* gu_s_merged = s_up + s_bytes;
  uint8_t* gu_sw = gu_s_merged + 2 * s_bytes;
  uint8_t* dn_sw = gu_sw + gu_sf_block;
  float* scal = reinterpret_cast<float*>(dn_sw + dn_sf_block);
  if (reinterpret_cast<uint8_t*>(scal) + 4 * sizeof(float) >
      p + staging_bytes_) {
    return Status::Fail("residency staging layout exceeds buffer");
  }

  Status s;
  // Fast path: the checkpoint keeps each expert's down/gate/up weights
  // contiguous (verified below per expert), so one pread fetches all three
  // (likewise the SF blocks and the six scalar scales). Fewer, larger,
  // contiguous reads are much faster on NVMe than ten small random reads.
  const std::string dn_w_name =
      ExpertName(layer_id_, expert, "down_proj", "weight");
  const io::TensorInfo* dn_w = loader_->FindTensor(dn_w_name);
  const io::TensorInfo* ga_w = loader_->FindTensor(
      ExpertName(layer_id_, expert, "gate_proj", "weight"));
  const io::TensorInfo* up_w = loader_->FindTensor(
      ExpertName(layer_id_, expert, "up_proj", "weight"));
  const io::TensorInfo* dn_s = loader_->FindTensor(
      ExpertName(layer_id_, expert, "down_proj", "weight_scale"));
  const io::TensorInfo* ga_s = loader_->FindTensor(
      ExpertName(layer_id_, expert, "gate_proj", "weight_scale"));
  const io::TensorInfo* up_s = loader_->FindTensor(
      ExpertName(layer_id_, expert, "up_proj", "weight_scale"));
  const bool w_ok = dn_w && ga_w && up_w &&
                    dn_w->byte_size() == w_bytes &&
                    ga_w->byte_size() == w_bytes &&
                    up_w->byte_size() == w_bytes &&
                    dn_w->data_end == ga_w->data_start &&
                    ga_w->data_end == up_w->data_start;
  const bool s_ok = dn_s && ga_s && up_s &&
                    dn_s->byte_size() == s_bytes &&
                    ga_s->byte_size() == s_bytes &&
                    up_s->byte_size() == s_bytes &&
                    dn_s->data_end == ga_s->data_start &&
                    ga_s->data_end == up_s->data_start;
  // Six scalar scales, file order: dn.input_scale, dn.ws2, ga.input_scale,
  // ga.ws2, up.input_scale, up.ws2 (each 4 B, contiguous when sc_ok).
  const char* sc_names[6] = {
      "down_proj.input_scale", "down_proj.weight_scale_2",
      "gate_proj.input_scale", "gate_proj.weight_scale_2",
      "up_proj.input_scale", "up_proj.weight_scale_2"};
  const io::TensorInfo* sc[6] = {nullptr};
  bool sc_ok = true;
  for (int i = 0; i < 6; ++i) {
    const std::string n = "model.language_model.layers." +
                          std::to_string(layer_id_) + ".mlp.experts." +
                          std::to_string(expert) + "." + sc_names[i];
    sc[i] = loader_->FindTensor(n);
    sc_ok = sc_ok && sc[i] != nullptr && sc[i]->byte_size() == sizeof(float);
  }
  if (sc_ok) {
    for (int i = 1; i < 6; ++i) {
      sc_ok = sc_ok && sc[i]->data_start == sc[0]->data_start + i * sizeof(float);
    }
  }
  if (w_ok && s_ok) {
    if (!(s = loader_->ReadRange(dn_w_name, dn_w->data_start, 3 * w_bytes,
                                 w_dn))) {
      return s;
    }
    const std::string dn_s_name =
        ExpertName(layer_id_, expert, "down_proj", "weight_scale");
    if (!(s = loader_->ReadRange(dn_s_name, dn_s->data_start, 3 * s_bytes,
                                 s_dn))) {
      return s;
    }
    if (sc_ok) {
      float file_scal[6];
      if (!(s = loader_->ReadRange(
                "model.language_model.layers." +
                    std::to_string(layer_id_) + ".mlp.experts." +
                    std::to_string(expert) + ".down_proj.input_scale",
                sc[0]->data_start, 6 * sizeof(float), file_scal))) {
        return s;
      }
      scal[0] = file_scal[3];  // gate.weight_scale_2
      scal[1] = file_scal[2];  // gate.input_scale
      scal[2] = file_scal[1];  // down.weight_scale_2
      scal[3] = file_scal[0];  // down.input_scale
    } else {
      const char* sc4[4] = {"gate_proj.weight_scale_2",
                            "gate_proj.input_scale",
                            "down_proj.weight_scale_2",
                            "down_proj.input_scale"};
      for (int i = 0; i < 4; ++i) {
        const std::string n = "model.language_model.layers." +
                              std::to_string(layer_id_) + ".mlp.experts." +
                              std::to_string(expert) + "." + sc4[i];
        if (!(s = loader_->ReadTensor(n, &scal[i]))) return s;
      }
    }
  } else {
    // Legacy per-tensor path (bit-identical bytes, arbitrary layout).
    std::string n;
    n = ExpertName(layer_id_, expert, "down_proj", "weight");
    if (!(s = loader_->ReadTensor(n, w_dn))) return s;
    n = ExpertName(layer_id_, expert, "gate_proj", "weight");
    if (!(s = loader_->ReadTensor(n, w_ga))) return s;
    n = ExpertName(layer_id_, expert, "up_proj", "weight");
    if (!(s = loader_->ReadTensor(n, w_up))) return s;
    n = ExpertName(layer_id_, expert, "down_proj", "weight_scale");
    if (!(s = loader_->ReadTensor(n, s_dn))) return s;
    n = ExpertName(layer_id_, expert, "gate_proj", "weight_scale");
    if (!(s = loader_->ReadTensor(n, s_ga))) return s;
    n = ExpertName(layer_id_, expert, "up_proj", "weight_scale");
    if (!(s = loader_->ReadTensor(n, s_up))) return s;
    const char* sc4[4] = {"gate_proj.weight_scale_2",
                          "gate_proj.input_scale",
                          "down_proj.weight_scale_2",
                          "down_proj.input_scale"};
    for (int i = 0; i < 4; ++i) {
      const std::string n = "model.language_model.layers." +
                            std::to_string(layer_id_) + ".mlp.experts." +
                            std::to_string(expert) + "." + sc4[i];
      if (!(s = loader_->ReadTensor(n, &scal[i]))) return s;
    }
  }

  // Merge gate+up scales then swizzle both blocks (identical to the
  // LoadMoEWeights per-expert path, so device bytes match bit-for-bit).
  std::memcpy(gu_s_merged, s_ga, s_bytes);
  std::memcpy(gu_s_merged + s_bytes, s_up, s_bytes);
  SwizzleSfInto(gu_s_merged, 2 * moe_is_, hs_, gu_sw);
  SwizzleSfInto(s_dn, hs_, moe_is_, dn_sw);
  return Status();
}

// Commit one staged expert into its slot: H2D (stream-ordered after any
// earlier GEMM that read the evicted slot, before any GEMM that reads the
// new expert) plus host identity/scale bookkeeping.
Status MoEResidency::CommitExpert(int expert, int slot, int t,
                                  cudaStream_t stream) const {
  if (expert < 0 || expert >= E_) {
    return Status::Fail("expert out of range");
  }
  if (slot < 0 || slot >= C_) {
    return Status::Fail("slot out of range");
  }
  const size_t w_bytes = static_cast<size_t>(hs_) * (moe_is_ / 2);
  const size_t s_bytes = static_cast<size_t>(hs_) * (moe_is_ / 16);
  const size_t gu_sf_block = layout_.gu_sf_block();
  const size_t dn_sf_block = layout_.dn_sf_block();

  // Staging is in checkpoint file order (see StageExpert):
  // [w_dn|w_ga|w_up|s_dn|s_ga|s_up|gu_s_merged|gu_sw|dn_sw|scal] with the
  // SF region packed at s_bytes each (total = MoEResidencyStagingBytes).
  uint8_t* p = staging_[t];
  uint8_t* w_dn = p;
  uint8_t* w_ga = p + w_bytes;
  uint8_t* w_up = p + 2 * w_bytes;
  uint8_t* gu_sw = p + 3 * w_bytes + 5 * s_bytes;
  uint8_t* dn_sw = gu_sw + gu_sf_block;
  const float* scal = reinterpret_cast<const float*>(dn_sw + dn_sf_block);
  if (reinterpret_cast<const uint8_t*>(scal) + 4 * sizeof(float) >
      p + staging_bytes_) {
    return Status::Fail("residency staging layout exceeds buffer");
  }

  Status s;
  // H2D into the slot.
  uint8_t* gu_dst =
      layout_.gu_packed + static_cast<size_t>(slot) * moe_is_ * hs_;
  if (!(s = H2D(w_ga, gu_dst, w_bytes, stream))) return s;
  if (!(s = H2D(w_up, gu_dst + moe_is_ * (hs_ / 2), w_bytes, stream)))
    return s;
  uint8_t* dn_dst =
      layout_.dn_packed + static_cast<size_t>(slot) * hs_ * (moe_is_ / 2);
  if (!(s = H2D(w_dn, dn_dst, w_bytes, stream))) return s;
  if (!(s = H2D(gu_sw, layout_.gu_sf + static_cast<size_t>(slot) * gu_sf_block,
                gu_sf_block, stream)))
    return s;
  if (!(s = H2D(dn_sw, layout_.dn_sf + static_cast<size_t>(slot) * dn_sf_block,
                dn_sf_block, stream)))
    return s;
  if (!(s = H2D(&scal[0], layout_.gu_w_scale2 + slot, sizeof(float), stream)))
    return s;
  if (!(s = H2D(&scal[1], layout_.gu_input_scale + slot, sizeof(float),
                stream)))
    return s;
  if (!(s = H2D(&scal[2], layout_.dn_w_scale2 + slot, sizeof(float), stream)))
    return s;
  if (!(s = H2D(&scal[3], layout_.dn_input_scale + slot, sizeof(float),
                stream)))
    return s;

  // Mark this staging buffer in flight until its last H2D completes (the
  // next reuser waits on the event).
  if (cudaEventRecord(staging_event_[t], stream) != cudaSuccess) {
    return Status::Fail("residency staging event record failed");
  }
  staging_in_flight_[t] = true;

  // Host identity + scales (the GEMM wrappers read the host scale vectors).
  layout_.gu_w_scale2_h[slot] = scal[0];
  layout_.gu_input_scale_h[slot] = scal[1];
  layout_.dn_w_scale2_h[slot] = scal[2];
  layout_.dn_input_scale_h[slot] = scal[3];
  if (slot_expert_[slot] < 0) ++resident_count_;
  if (slot_expert_[slot] >= 0) expert_slot_[slot_expert_[slot]] = -1;
  slot_expert_[slot] = expert;
  expert_slot_[expert] = slot;
  ++stats_.loads;
  stats_.load_bytes +=
      static_cast<double>(3 * w_bytes + 3 * s_bytes +
                          4 * sizeof(float));
  return Status();
}

Status MoEResidency::InitHot(const std::vector<int>& hot_experts,
                             cudaStream_t stream) {
  if (!inited_) return Status::Fail("residency not initialized");
  LoadPlan plan;
  int slot = 0;
  for (int e : hot_experts) {
    if (e < 0 || e >= E_) continue;
    if (expert_slot_[e] >= 0) continue;  // duplicate in the list
    if (slot >= C_) break;
    plan.experts.push_back(e);
    plan.slots.push_back(slot);
    ++slot;
  }
  Status s = LoadPhase1(plan);
  if (!s.ok()) return s;
  while (!plan.fully_committed()) {
    s = LoadPhase2(plan, stream);
    if (!s.ok()) return s;
    s = LoadPhase1(plan);
    if (!s.ok()) return s;
  }
  for (int i = 0; i < static_cast<int>(plan.experts.size()); ++i) {
    slot_tick_[plan.slots[i]] = ++tick_;
  }
  return Status();
}

void MoEResidency::SetHotProtected() {
  if (!inited_ || hot_protected_) return;
  for (int s = 0; s < C_; ++s) {
    const int e = slot_expert_[s];
    if (e >= 0 && expert_slot_[e] == s) {
      slot_protected_[s] = 1;
      ++protected_count_;
    }
  }
  hot_protected_ = true;
}

Status MoEResidency::PlanResolve(const int32_t* needed, int n,
                                 int32_t* slot_of, LoadPlan* plan,
                                 bool decode_phase) const {
  if (!inited_) return Status::Fail("residency not initialized");
  if (n <= 0) return Status();
  if (n > (1 << 20)) {
    return Status::Fail("residency resolve set too large");
  }
  plan->clear();
  ++tick_;
  ++stats_.resolve_calls;

  // Mark the needed set so victim selection never evicts an expert that this
  // call still has to load (avoids a load-evict-reload cycle).
  std::vector<uint8_t> needed_mark(E_, 0);
  for (int i = 0; i < n; ++i) {
    const int e = needed[i];
    if (e < 0 || e >= E_) {
      return Status::Fail("needed expert out of range");
    }
    needed_mark[e] = 1;
  }

  std::vector<uint8_t> reserved(C_, 0);
  // In-call dedup: the needed list repeats experts (one entry per token
  // top-k slot). A miss reserves a slot but does not commit it, so without
  // this map every repeat of the same expert would miss again and reserve a
  // second slot, exhausting capacity even when the DISTINCT set fits.
  std::vector<int32_t> planned(E_, -1);
  for (int i = 0; i < n; ++i) {
    const int e = needed[i];
    ++stats_.expert_lookups;
    if (decode_phase) {
      ++stats_.decode_lookups;
    } else {
      ++stats_.prefill_lookups;
    }
    int slot = expert_slot_[e];
    if (slot < 0) slot = planned[e];
    if (slot >= 0) {
      slot_tick_[slot] = tick_;
      ++stats_.hits;
      slot_of[i] = slot;
      continue;
    }
    // Miss: pick a victim. Prefer an empty slot; else the LRU slot that is
    // not protected, not needed by this call, and not reserved by an earlier
    // miss in this call. The caller keeps the distinct needed set within the
    // available (non-protected) capacity, so this always succeeds.
    ++stats_.misses;
    if (decode_phase) {
      ++stats_.decode_misses;
    } else {
      ++stats_.prefill_misses;
    }
    int v = -1;
    for (int s = 0; s < C_; ++s) {
      if (slot_expert_[s] < 0 && !slot_protected_[s] && !reserved[s]) {
        v = s;
        break;
      }
    }
    if (v < 0) {
      uint64_t best = UINT64_MAX;
      for (int s = 0; s < C_; ++s) {
        if (slot_protected_[s] || reserved[s]) continue;
        const int se = slot_expert_[s];
        if (se >= 0 && needed_mark[se]) continue;
        if (slot_tick_[s] < best) {
          best = slot_tick_[s];
          v = s;
        }
      }
    }
    if (v < 0) {
      int distinct = 0;
      for (int e = 0; e < E_; ++e)
        if (needed_mark[e]) ++distinct;
      int resident = 0;
      for (int e = 0; e < E_; ++e)
        if (needed_mark[e] && expert_slot_[e] >= 0) ++resident;
      int reserved_n = 0;
      for (int s = 0; s < C_; ++s)
        if (reserved[s]) ++reserved_n;
      std::fprintf(stderr,
                   "[q4t][residency][diag] layer=%d C=%d protected=%d "
                   "distinct=%d resident=%d misses_so_far=%d reserved=%d "
                   "n=%d decode=%d\n",
                   layer_id_, C_, protected_count_, distinct, resident,
                   static_cast<int>(stats_.misses), reserved_n, n,
                   decode_phase);
      return Status::Fail("no residency slot available (needed set exceeds "
                          "dynamic capacity)");
    }
    reserved[v] = 1;
    planned[e] = v;
    plan->experts.push_back(e);
    plan->slots.push_back(v);
    slot_of[i] = v;
  }
  return Status();
}

Status MoEResidency::LoadPhase1(LoadPlan& plan) const {
  if (!inited_) return Status::Fail("residency not initialized");
  const int n = static_cast<int>(plan.experts.size());
  const int off = static_cast<int>(plan.next_stage);
  if (off >= n) return Status();  // fully staged
  if (off == 0) plan.workers.assign(n, 0);
  // One chunk per call: entry off + t is staged by worker t into staging
  // buffer t, so a buffer holds at most one expert at a time. Reusing a
  // buffer for the next chunk is safe because StageExpert waits on the
  // staging event recorded by the commit that last H2D'd from it.
  const int cnt = std::min(load_threads_, n - off);
  std::atomic<int> first_err{0};  // 0 = ok, else 1
  std::vector<std::thread> pool;
  pool.reserve(cnt);
  for (int t = 0; t < cnt; ++t) {
    const int i = off + t;
    pool.emplace_back([&, t, i]() {
      Status s = StageExpert(plan.experts[i], t);
      if (!s.ok()) {
        first_err.store(1, std::memory_order_relaxed);
      } else {
        plan.workers[i] = t;
      }
    });
  }
  for (auto& th : pool) th.join();
  if (first_err.load(std::memory_order_relaxed) != 0) {
    return Status::Fail("residency parallel stage failed");
  }
  plan.next_stage = off + cnt;
  return Status();
}

Status MoEResidency::LoadPhase2(LoadPlan& plan, cudaStream_t stream,
                                bool* loaded) const {
  if (!inited_) return Status::Fail("residency not initialized");
  if (plan.next_commit >= plan.next_stage) return Status();  // nothing staged
  if (plan.workers.size() != plan.experts.size()) {
    return Status::Fail("residency plan missing worker map");
  }
  for (size_t i = plan.next_commit; i < plan.next_stage; ++i) {
    const int e = plan.experts[i];
    const int v = plan.slots[i];
    // Only an overwrite of an occupied slot is an eviction; loading an empty
    // slot (initial fill) is not.
    if (slot_expert_[v] >= 0) ++stats_.evictions;
    Status s = CommitExpert(e, v, plan.workers[i], stream);
    if (!s.ok()) return s;
    slot_tick_[v] = tick_;
    if (loaded) *loaded = true;
  }
  plan.next_commit = plan.next_stage;
  return Status();
}

Status MoEResidency::Resolve(const int32_t* needed, int n, int32_t* slot_of,
                             cudaStream_t stream, bool* loaded,
                             bool decode_phase) const {
  LoadPlan plan;
  Status s = PlanResolve(needed, n, slot_of, &plan, decode_phase);
  if (!s.ok()) return s;
  if (loaded) *loaded = false;
  s = LoadPhase1(plan);
  if (!s.ok()) return s;
  while (!plan.fully_committed()) {
    s = LoadPhase2(plan, stream, loaded);
    if (!s.ok()) return s;
    s = LoadPhase1(plan);
    if (!s.ok()) return s;
  }
  return Status();
}

void MoEResidency::Free() {
  if (!inited_) return;
  auto free_if = [](void** p) {
    if (*p) {
      cudaFree(*p);
      *p = nullptr;
    }
  };
  free_if(reinterpret_cast<void**>(&layout_.gu_packed));
  free_if(reinterpret_cast<void**>(&layout_.gu_sf));
  free_if(reinterpret_cast<void**>(&layout_.dn_packed));
  free_if(reinterpret_cast<void**>(&layout_.dn_sf));
  free_if(reinterpret_cast<void**>(&layout_.gu_w_scale2));
  free_if(reinterpret_cast<void**>(&layout_.gu_input_scale));
  free_if(reinterpret_cast<void**>(&layout_.dn_w_scale2));
  free_if(reinterpret_cast<void**>(&layout_.dn_input_scale));
  for (int t = 0; t < load_threads_; ++t) {
    if (staging_[t]) {
      cudaFreeHost(staging_[t]);
      staging_[t] = nullptr;
    }
    if (staging_event_[t]) {
      cudaEventDestroy(staging_event_[t]);
      staging_event_[t] = nullptr;
    }
  }
  staging_.clear();
  inited_ = false;
}

}  // namespace quant
}  // namespace q4t
