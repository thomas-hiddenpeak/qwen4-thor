// NVFP4 routed-expert MoE weight loader (direct-to-packed).
#include "q4t/quant/moe_weights.h"

#include <cuda_runtime.h>

#include <cstdio>
#include <cstring>
#include <vector>

namespace q4t {
namespace quant {

namespace {

// Build the checkpoint tensor name for one expert projection/suffix.
std::string ExpertName(int layer_id, int expert, const char* proj,
                       const char* suffix) {
  return "model.language_model.layers." + std::to_string(layer_id) +
         ".mlp.experts." + std::to_string(expert) + "." + proj + "." + suffix;
}

// H2D-copy a host buffer to a device pointer, returning a Status on error.
Status H2D(const void* src, void* dst, size_t bytes, cudaStream_t stream) {
  if (bytes == 0) return Status();
  if (cudaMemcpyAsync(dst, src, bytes, cudaMemcpyHostToDevice, stream) !=
      cudaSuccess) {
    return Status::Fail(std::string("cudaMemcpyAsync failed: ") +
                        cudaGetErrorString(cudaGetLastError()));
  }
  return Status();
}

}  // namespace

Status LoadMoEWeights(const io::WeightLoader& loader, int layer_id, int E,
                      int hs, int moe_is, MoEWeightLayout* out,
                      cudaStream_t stream) {
  if (E <= 0 || hs <= 0 || moe_is <= 0) {
    return Status::Fail("invalid MoE dims");
  }
  if ((hs % 32) != 0 || (moe_is % 32) != 0) {
    return Status::Fail("hs and moe_is must be multiples of 32 (FP4 GEMM)");
  }
  // Per-expert SF blocks require the row counts to be multiples of the 128-row
  // swizzle atom so each expert's block is a valid standalone swizzled buffer.
  if ((2 * moe_is) % 128 != 0 || (hs % 128) != 0) {
    return Status::Fail(
        "2*moe_is and hs must be multiples of 128 (per-expert SF blocks)");
  }

  out->E = E;
  out->hs = hs;
  out->moe_is = moe_is;

  const size_t gu_sf_block = SfBufferSize(2 * moe_is, hs);
  const size_t dn_sf_block = SfBufferSize(hs, moe_is);
  const size_t gu_packed_bytes = static_cast<size_t>(2 * E * moe_is) * (hs / 2);
  const size_t dn_packed_bytes = static_cast<size_t>(E * hs) * (moe_is / 2);
  const size_t scal_bytes = static_cast<size_t>(E) * sizeof(float);

  auto alloc = [&](void** p, size_t bytes) -> Status {
    if (cudaMalloc(p, bytes) != cudaSuccess) {
      return Status::Fail(std::string("cudaMalloc failed (") +
                          std::to_string(bytes) + " bytes)");
    }
    return Status();
  };
  Status s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_packed), gu_packed_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_sf),
                  static_cast<size_t>(E) * gu_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_packed), dn_packed_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_sf),
                  static_cast<size_t>(E) * dn_sf_block)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->gu_input_scale), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_w_scale2), scal_bytes)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&out->dn_input_scale), scal_bytes)))
    return s;

  out->gu_w_scale2_h.resize(E);
  out->gu_input_scale_h.resize(E);
  out->dn_w_scale2_h.resize(E);
  out->dn_input_scale_h.resize(E);

  // Host staging (reused across experts).
  const size_t gu_w_bytes = static_cast<size_t>(moe_is) * (hs / 2);
  const size_t gu_s_bytes = static_cast<size_t>(moe_is) * (hs / 16);
  const size_t dn_w_bytes = static_cast<size_t>(hs) * (moe_is / 2);
  std::vector<uint8_t> gu_w(gu_w_bytes);
  std::vector<uint8_t> up_w(gu_w_bytes);
  std::vector<uint8_t> dn_w(dn_w_bytes);
  std::vector<uint8_t> gate_s(gu_s_bytes);
  std::vector<uint8_t> up_s(gu_s_bytes);
  std::vector<uint8_t> gu_s_merged(2 * gu_s_bytes);
  std::vector<uint8_t> dn_s(static_cast<size_t>(hs) * (moe_is / 16));
  float scal[4];  // [gu_ws2, gu_isc, dn_ws2, dn_isc]

  for (int e = 0; e < E; ++e) {
    // --- packed weights (row-major) ---
    std::string n = ExpertName(layer_id, e, "gate_proj", "weight");
    if (!(s = loader.ReadTensor(n, gu_w.data()))) return s;
    n = ExpertName(layer_id, e, "up_proj", "weight");
    if (!(s = loader.ReadTensor(n, up_w.data()))) return s;
    uint8_t* gu_dst = out->gu_packed_expert(e);
    if (!(s = H2D(gu_w.data(), gu_dst, gu_w_bytes, stream))) return s;
    if (!(s = H2D(up_w.data(), gu_dst + moe_is * (hs / 2), gu_w_bytes, stream)))
      return s;

    n = ExpertName(layer_id, e, "down_proj", "weight");
    if (!(s = loader.ReadTensor(n, dn_w.data()))) return s;
    uint8_t* dn_dst = out->dn_packed + static_cast<size_t>(e) * dn_w_bytes;
    if (!(s = H2D(dn_w.data(), dn_dst, dn_w_bytes, stream))) return s;

    // --- gate/up scale: merge (gate rows then up rows) + swizzle per expert ---
    n = ExpertName(layer_id, e, "gate_proj", "weight_scale");
    if (!(s = loader.ReadTensor(n, gate_s.data()))) return s;
    n = ExpertName(layer_id, e, "up_proj", "weight_scale");
    if (!(s = loader.ReadTensor(n, up_s.data()))) return s;
    std::memcpy(gu_s_merged.data(), gate_s.data(), gu_s_bytes);
    std::memcpy(gu_s_merged.data() + gu_s_bytes, up_s.data(), gu_s_bytes);
    std::vector<uint8_t> gu_sw = SwizzleSf(gu_s_merged.data(), 2 * moe_is, hs);
    if (gu_sw.size() != gu_sf_block) {
      return Status::Fail("gu_sf swizzle size mismatch");
    }
    if (!(s = H2D(gu_sw.data(), out->gu_sf_expert(e), gu_sw.size(), stream)))
      return s;

    // --- down scale: swizzle per expert ---
    n = ExpertName(layer_id, e, "down_proj", "weight_scale");
    if (!(s = loader.ReadTensor(n, dn_s.data()))) return s;
    std::vector<uint8_t> dn_sw = SwizzleSf(dn_s.data(), hs, moe_is);
    if (dn_sw.size() != dn_sf_block) {
      return Status::Fail("dn_sf swizzle size mismatch");
    }
    if (!(s = H2D(dn_sw.data(), out->dn_sf_expert(e), dn_sw.size(), stream)))
      return s;

    // --- scalars (device + host copy) ---
    n = ExpertName(layer_id, e, "gate_proj", "weight_scale_2");
    if (!(s = loader.ReadTensor(n, &scal[0]))) return s;
    n = ExpertName(layer_id, e, "gate_proj", "input_scale");
    if (!(s = loader.ReadTensor(n, &scal[1]))) return s;
    n = ExpertName(layer_id, e, "down_proj", "weight_scale_2");
    if (!(s = loader.ReadTensor(n, &scal[2]))) return s;
    n = ExpertName(layer_id, e, "down_proj", "input_scale");
    if (!(s = loader.ReadTensor(n, &scal[3]))) return s;
    out->gu_w_scale2_h[e] = scal[0];
    out->gu_input_scale_h[e] = scal[1];
    out->dn_w_scale2_h[e] = scal[2];
    out->dn_input_scale_h[e] = scal[3];
    if (!(s = H2D(&scal[0], out->gu_w_scale2 + e, sizeof(float), stream)))
      return s;
    if (!(s = H2D(&scal[1], out->gu_input_scale + e, sizeof(float), stream)))
      return s;
    if (!(s = H2D(&scal[2], out->dn_w_scale2 + e, sizeof(float), stream)))
      return s;
    if (!(s = H2D(&scal[3], out->dn_input_scale + e, sizeof(float), stream)))
      return s;
  }

  if (stream != nullptr) {
    if (cudaStreamSynchronize(stream) != cudaSuccess) {
      return Status::Fail(std::string("stream sync failed: ") +
                          cudaGetErrorString(cudaGetLastError()));
    }
  }
  return Status();
}

}  // namespace quant
}  // namespace q4t
