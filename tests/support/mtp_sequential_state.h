// Test-local complete valid-state reader/restorer. No production snapshot or
// checkpoint reader is used. Frozen scope: text, S1/slot0, identity page map.
#pragma once

#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace q4t::test::sequential {

inline void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}
inline void Check(const Status& status, const std::string& operation) {
  Require(status.ok(), operation + ": " + status.message());
}
inline void Cuda(cudaError_t error, const std::string& operation) {
  Require(error == cudaSuccess, operation + ": " + cudaGetErrorString(error));
}
inline float Value(uint16_t bits) {
  return std::bit_cast<float>(uint32_t{bits} << 16);
}
template <typename T>
std::vector<T> Read(const T* pointer, size_t elements) {
  std::vector<T> values(elements);
  if (elements)
    Cuda(cudaMemcpy(values.data(), pointer, elements * sizeof(T),
                    cudaMemcpyDeviceToHost),
         "read test observation");
  return values;
}
template <typename T>
void Write(T* pointer, const std::vector<T>& values) {
  if (!values.empty())
    Cuda(cudaMemcpy(pointer, values.data(), values.size() * sizeof(T),
                    cudaMemcpyHostToDevice),
         "restore test observation");
}
template <typename T>
void Exact(const std::vector<T>& actual, const std::vector<T>& expected,
           const std::string& label) {
  Require(actual.size() == expected.size(), label + ": shape");
  if (actual.empty() || std::memcmp(actual.data(), expected.data(),
                                    actual.size() * sizeof(T)) == 0)
    return;
  size_t first = 0;
  while (first < actual.size() &&
         std::memcmp(&actual[first], &expected[first], sizeof(T)) == 0)
    ++first;
  throw std::runtime_error(label + ": unequal at element " +
                           std::to_string(first));
}
inline void Finite(const std::vector<uint16_t>& values,
                   const std::string& label) {
  for (size_t i = 0; i < values.size(); ++i)
    if (!std::isfinite(Value(values[i])))
      throw std::runtime_error(label + ": nonfinite BF16 element " +
                               std::to_string(i));
}
inline int32_t Argmax(const std::vector<uint16_t>& logits) {
  Require(!logits.empty(), "empty host argmax input");
  Finite(logits, "host argmax");
  int32_t best = 0;
  for (size_t i = 1; i < logits.size(); ++i)
    if (Value(logits[i]) > Value(logits[best])) best = static_cast<int32_t>(i);
  return best;
}
inline void SameSequence(const model::ModelSequence& actual,
                         const model::ModelSequence& expected,
                         const std::string& label) {
  Require(actual.position == expected.position &&
              actual.history == expected.history &&
              actual.seq_id == expected.seq_id &&
              actual.stage == expected.stage &&
              actual.HasPending() == expected.HasPending() &&
              actual.SubmittedPosition() == expected.SubmittedPosition(),
          label + ": sequence mismatch");
}

enum class Kind { kBf16, kFp32, kInteger };
struct Region {
  std::string name;
  void* device = nullptr;
  Kind kind = Kind::kInteger;
  std::vector<uint8_t> bytes;

  void CheckFinite(const std::vector<uint8_t>& data) const {
    if (kind == Kind::kInteger) return;
    const size_t width = kind == Kind::kFp32 ? 4 : 2;
    Require(data.size() % width == 0, name + ": scalar alignment");
    for (size_t offset = 0; offset < data.size(); offset += width) {
      float value;
      if (kind == Kind::kFp32) {
        std::memcpy(&value, data.data() + offset, sizeof(value));
      } else {
        uint16_t bits;
        std::memcpy(&bits, data.data() + offset, sizeof(bits));
        value = Value(bits);
      }
      if (!std::isfinite(value))
        throw std::runtime_error(name + ": nonfinite valid state");
    }
  }
};

struct State {
  std::vector<Region> regions;
  model::ModelSequence sequence;
  std::vector<int> rope_delta;
  int checkpoint_rows = 0;
  std::vector<int> checkpoint_slots;
  bool has_main = false;
  size_t bytes = 0;

  void Add(const std::string& name, void* device, size_t size, Kind kind) {
    Require(device || size == 0, name + ": null valid-state buffer");
    Region region{name, device, kind,
                  Read(static_cast<uint8_t*>(device), size)};
    region.CheckFinite(region.bytes);
    bytes += size;
    regions.push_back(std::move(region));
  }
  void RestoreDevice() const {
    for (const auto& region : regions)
      Write(static_cast<uint8_t*>(region.device), region.bytes);
  }
  void RestoreMain(const model::Model& main,
                   model::ModelSequence* destination) const {
    Require(has_main, "attempted main restore from draft state");
    RestoreDevice();
    main.rope_delta = rope_delta;
    main.verify_ckpt_rows = checkpoint_rows;
    main.verify_ckpt_slots = checkpoint_slots;
    *destination = sequence;
  }
  void CompareDevice(const std::string& label) const {
    for (const auto& region : regions) {
      const auto current =
          Read(static_cast<uint8_t*>(region.device), region.bytes.size());
      region.CheckFinite(current);
      Exact(current, region.bytes, label + "." + region.name);
    }
    std::printf(
        "  sequential_state=%s bytes=%zu regions=%zu exact=1 finite=1\n",
        label.c_str(), bytes, regions.size());
  }
  void CompareMain(const model::Model& main, const model::ModelSequence& actual,
                   const std::string& label) const {
    Require(has_main, "attempted main comparison from draft state");
    SameSequence(actual, sequence, label);
    Require(main.rope_delta == rope_delta &&
                main.verify_ckpt_rows == checkpoint_rows &&
                main.verify_ckpt_slots == checkpoint_slots,
            label + ": position/checkpoint metadata mismatch");
    CompareDevice(label);
  }
};

// Prove, rather than assume, that every logical row maps to the contiguous
// physical row in this frozen S1 fixture. This includes every future mapping.
// A nonidentity mapping fails the scope check instead of silently reading the
// wrong bytes. The fully checked identity map permits one coalesced KV copy.
inline void PageMap(State* state, int* table, int capacity,
                    const std::string& name) {
  const auto map = Read(table, static_cast<size_t>(capacity));
  for (int p = 0; p < capacity; ++p)
    if (map[p] != p / model::kKvPageSize)
      throw std::runtime_error(name + ": nonidentity page map outside fixture");
  state->Add(name, table, static_cast<size_t>(capacity) * sizeof(int),
             Kind::kInteger);
}
inline void Attention(State* state, uint16_t* kv, int* page_table,
                      uint16_t* raw, uint16_t* compressed, int capacity,
                      int length, int nkv, int hd, int idx_hd, int compress,
                      const std::string& label) {
  Require(length >= 0 && length <= capacity && nkv > 0 && hd > 0 &&
              idx_hd > 0 && compress > 0,
          label + ": invalid cache domain");
  PageMap(state, page_table, capacity, label + ".pages");
  state->Add(label + ".kv", kv, static_cast<size_t>(length) * nkv * 2 * hd * 2,
             Kind::kBf16);
  state->Add(label + ".index_raw", raw,
             static_cast<size_t>(length) * idx_hd * 2, Kind::kBf16);
  state->Add(label + ".index_complete", compressed,
             static_cast<size_t>(length / compress) * idx_hd * 2, Kind::kBf16);
}
inline void Rope(State* state, int* rope, int capacity, int length,
                 const std::string& label) {
  for (int row = 0; row < 3; ++row)
    state->Add(label + ".rope" + std::to_string(row),
               rope + static_cast<size_t>(row) * capacity,
               static_cast<size_t>(length) * sizeof(int), Kind::kInteger);
}
inline State CaptureMain(const model::Model& main,
                         const model::ModelSequence& sequence) {
  Require(main.cfg.max_seq == 1 && sequence.seq_id == 0 &&
              !sequence.HasPending() && sequence.position >= 0 &&
              sequence.history.size() == static_cast<size_t>(sequence.position),
          "main valid-state capture requires committed S1/slot0");
  State result;
  result.has_main = true;
  result.sequence = sequence;
  result.rope_delta = main.rope_delta;
  result.checkpoint_rows = main.verify_ckpt_rows;
  result.checkpoint_slots = main.verify_ckpt_slots;
  Require(result.checkpoint_rows == 0,
          "ordinary/strict path left a valid speculative checkpoint");
  for (const auto& layer : main.layers) {
    const std::string label = "layer" + std::to_string(layer.layer_id);
    Require(layer.max_seq == 1, label + ": pooled scope changed");
    if (layer.is_full_attention) {
      Attention(&result, layer.kv_cache, layer.page_table, layer.idx_raw,
                layer.idx_comp, layer.max_len, sequence.position,
                layer.full.nkv, layer.full.hd, layer.full.idx_head_dim,
                layer.full.idx_compress, label);
    } else {
      result.Add(label + ".ssm", layer.ssm_state,
                 static_cast<size_t>(layer.linear.nv) * layer.linear.kd *
                     layer.linear.vd * sizeof(float),
                 Kind::kFp32);
      result.Add(label + ".conv", layer.conv_state,
                 static_cast<size_t>(layer.linear.in_qkv()) *
                     (layer.linear.conv_k - 1) * 2,
                 Kind::kBf16);
    }
    if (layer.has_ple)
      result.Add(label + ".ple_conv", layer.ple_conv_state,
                 static_cast<size_t>(layer.hc_dim) *
                     (layer.ple.conv_kernel - 1) * layer.ple.conv_dilation * 2,
                 Kind::kBf16);
  }
  Rope(&result, main.d_rope_pos, main.cfg.max_len, sequence.position, "main");
  return result;
}
inline State CaptureDraft(const mtp::MtpModel& draft, int length) {
  Require(draft.max_seq == 1, "draft valid-state capture requires S1");
  State result;
  Attention(&result, draft.kv_cache, draft.page_table, draft.idx_raw,
            draft.idx_comp, draft.cfg.max_len, length, draft.full_attn.nkv,
            draft.full_attn.hd, draft.full_attn.idx_head_dim,
            draft.full_attn.idx_compress, "draft");
  Rope(&result, draft.d_rope_pos, draft.cfg.max_len, length, "draft");
  return result;
}

inline void PoisonFuture(const model::Model& main, int length, int byte) {
  Require(byte == 0x3f || byte == 0xbf, "unfrozen future poison pattern");
  for (const auto& layer : main.layers) {
    if (!layer.is_full_attention) continue;
    const auto map = Read(layer.page_table, layer.max_len);
    for (int p = 0; p < layer.max_len; ++p)
      Require(map[p] == p / model::kKvPageSize,
              "future poison requires verified identity map");
    const size_t kv_row =
        static_cast<size_t>(layer.full.nkv) * 2 * layer.full.hd;
    const size_t idx_row = layer.full.idx_head_dim;
    const size_t groups = length / layer.full.idx_compress;
    Cuda(cudaMemset(layer.kv_cache + static_cast<size_t>(length) * kv_row, byte,
                    static_cast<size_t>(layer.max_len - length) * kv_row * 2),
         "poison future KV");
    Cuda(cudaMemset(layer.idx_raw + static_cast<size_t>(length) * idx_row, byte,
                    static_cast<size_t>(layer.max_len - length) * idx_row * 2),
         "poison future index raw");
    Cuda(
        cudaMemset(layer.idx_comp + groups * idx_row, byte,
                   (static_cast<size_t>(layer.max_len) - groups) * idx_row * 2),
        "poison invisible compressed keys");
  }
  for (int row = 0; row < 3; ++row)
    Cuda(cudaMemset(main.d_rope_pos +
                        static_cast<size_t>(row) * main.cfg.max_len + length,
                    byte, static_cast<size_t>(main.cfg.max_len - length) * 4),
         "poison future RoPE");
}

}  // namespace q4t::test::sequential
