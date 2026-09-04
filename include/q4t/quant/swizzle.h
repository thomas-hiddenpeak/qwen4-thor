// NVFP4 scale-factor (SF) swizzle layout.
//
// cuBLASLt's CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3 (and the hardware
// tcgen05.mma) do NOT store the per-16-element e4m3 scale factors in plain
// row-major order. They use a 128-row x 64-element swizzled atom (CUTLASS
// Sm1xxBlockScaledBasicChunk::SfKMajorAtom):
//
//   logical coords (r = row, g = K/16 group index)
//     i      = r % 32
//     j      = (r % 128) / 32
//     ga     = g % 4
//     within = i * 16 + j * 4 + ga
//     offset = within + (g / 4) * 512
//                    + (r / 128) * num_g_tiles * 512
//   where num_g_tiles = ceil((K/16) / 4).
//
// The physical buffer must be padded to whole atoms:
//   total_bytes = ceil(rows / 128) * num_g_tiles * 512
// even when rows < 128 (e.g. decode M=1 still allocates a full 128-row atom).
//
// The main FP4 payload stays row-major [rows, K/2]; only the SF tensor is
// swizzled. Verified against CuTe tile_to_shape(SfAtom, (M,K), Step<_2,_1>)
// and against cuBLASLt matmul on Thor SM110a (16/16 real-shape cases pass).
#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace q4t {
namespace quant {

// Swizzled byte offset for the SF at logical (row, group).
inline std::size_t SfOffset(int row, int group, int num_g_tiles) {
  int i = row % 32;
  int j = (row % 128) / 32;
  int ga = group % 4;
  int within = i * 16 + j * 4 + ga;
  return static_cast<std::size_t>(within) +
         static_cast<std::size_t>(group / 4) * 512 +
         static_cast<std::size_t>(row / 128) *
             static_cast<std::size_t>(num_g_tiles) * 512;
}

// Number of 64-element SF tiles along K for a given K.
inline int SfNumGtiles(int K) {
  int groups = K / 16;
  return (groups + 3) / 4;
}

// Physical SF buffer size in bytes for `rows` rows and reduction dim K.
inline std::size_t SfBufferSize(int rows, int K) {
  int m_blocks = (rows + 127) / 128;
  return static_cast<std::size_t>(m_blocks) *
         static_cast<std::size_t>(SfNumGtiles(K)) * 512;
}

// Convert a row-major [rows, K/16] e4m3 scale buffer into the swizzled layout.
// Returns a buffer of SfBufferSize(rows, K) bytes (padded, zero-filled).
inline std::vector<uint8_t> SwizzleSf(const uint8_t* row_major, int rows,
                                      int K) {
  int groups = K / 16;
  int num_g_tiles = SfNumGtiles(K);
  std::vector<uint8_t> out(SfBufferSize(rows, K), 0);
  for (int r = 0; r < rows; ++r) {
    for (int g = 0; g < groups; ++g) {
      out[SfOffset(r, g, num_g_tiles)] =
          row_major[static_cast<std::size_t>(r) * groups + g];
    }
  }
  return out;
}

}  // namespace quant
}  // namespace q4t
