#include "q4t/server/session_state.h"

namespace q4t::server {

namespace {
void AttentionRanges(uint16_t* kv, uint16_t* raw, uint16_t* comp,
                     const model::FullAttentionWeights& weights,
                     const std::vector<int>& capacities, int max_len, int slot,
                     int prefix, std::vector<SessionRange>* ranges) {
  const size_t offset = runtime::SequenceOffset(capacities, slot, max_len);
  const size_t kv_row = static_cast<size_t>(weights.nkv) * 2 * weights.hd;
  const auto add = [&](void* data, size_t bytes) {
    if (bytes) ranges->push_back({data, bytes});
  };
  add(kv ? kv + offset * kv_row : nullptr, prefix * kv_row * 2);
  add(raw ? raw + offset * weights.idx_head_dim : nullptr,
      static_cast<size_t>(prefix) * weights.idx_head_dim * 2);
  add(comp ? comp + offset * weights.idx_head_dim : nullptr,
      static_cast<size_t>(prefix / weights.idx_compress) *
          weights.idx_head_dim * 2);
}

Status CheckBuffers(std::span<const SessionRange> ranges) {
  for (const auto& range : ranges)
    if (!range.data) return Status::Fail("session state missing buffer");
  return Status();
}
}  // namespace

Status TargetSessionStateRanges(const model::Model& model, int slot, int prefix,
                                std::vector<SessionRange>* ranges) {
  ranges->clear();
  if (slot < 0 || slot >= model.cfg.max_seq || prefix < 1 ||
      prefix >= model.cfg.Capacity(slot))
    return Status::Fail("session state slot/prefix/layout");
  auto add = [&](void* data, size_t bytes) {
    if (bytes) ranges->push_back({data, bytes});
  };
  auto recurrent = [&](void* data, size_t bytes) {
    add(data ? static_cast<uint8_t*>(data) + slot * bytes : nullptr, bytes);
  };
  for (const auto& layer : model.layers) {
    if (layer.is_full_attention) {
      AttentionRanges(layer.kv_cache, layer.idx_raw, layer.idx_comp, layer.full,
                      layer.seq_capacities, layer.max_len, slot, prefix,
                      ranges);
    } else {
      recurrent(layer.ssm_state, static_cast<size_t>(layer.linear.nv) *
                                     layer.linear.kd * layer.linear.vd * 4);
      recurrent(layer.conv_state, static_cast<size_t>(layer.linear.in_qkv()) *
                                      (layer.linear.conv_k - 1) * 2);
    }
    if (layer.has_ple)
      recurrent(layer.ple_conv_state, static_cast<size_t>(layer.hc_dim) *
                                          (layer.ple.conv_kernel - 1) *
                                          layer.ple.conv_dilation * 2);
  }
  for (int row = 0; row < 3; ++row)
    add(model.d_rope_pos
            ? model.d_rope_pos +
                  (static_cast<size_t>(slot) * 3 + row) * model.cfg.max_len
            : nullptr,
        static_cast<size_t>(prefix) * sizeof(int));
  return CheckBuffers(*ranges);
}

Status SessionStateRanges(const model::Model& model, const mtp::MtpModel& draft,
                          int slot, int prefix, uint16_t* rolling_g,
                          uint16_t* last_main_trunk,
                          std::vector<SessionRange>* ranges) {
  ranges->clear();
  // SequenceOffset and Capacity cover both uniform and explicitly sized
  // slots. The state layout is identical; no explicit-capacity opt-in is
  // required by the standard API cache path.
  if (slot < 0 || slot >= draft.max_seq || prefix < 1 ||
      prefix >= draft.cfg.Capacity(slot) || !rolling_g || !last_main_trunk)
    return Status::Fail("session state slot/prefix/layout");
  const auto status = TargetSessionStateRanges(model, slot, prefix, ranges);
  if (!status) return status;
  AttentionRanges(draft.kv_cache, draft.idx_raw, draft.idx_comp,
                  draft.full_attn, draft.cfg.seq_capacities, draft.cfg.max_len,
                  slot, prefix, ranges);
  // The draft's RoPE table is immutable text identity shared by all slots.
  ranges->push_back({rolling_g, static_cast<size_t>(draft.hc_dim()) * 2});
  ranges->push_back({last_main_trunk, static_cast<size_t>(model.hc_dim()) * 2});
  return CheckBuffers(*ranges);
}

}  // namespace q4t::server
