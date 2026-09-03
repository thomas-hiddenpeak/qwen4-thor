// Safetensors reader: mmap-based, zero-copy access to tensor metadata and
// data.
//
// A .safetensors file is laid out as:
//   [ 8-byte little-endian u64: header_len ]
//   [ header_len bytes: JSON header ]
//   [ data: contiguous tensor bytes ]
//
// The JSON header is a map of tensor name -> { "dtype": str, "shape": [..],
// "data_offsets": [start, end] } where data_offsets are byte offsets into the
// data region (which starts right after the header). "__metadata__" (optional)
// is ignored.
//
// This reader mmaps the file read-only, so opening a 10 GB shard costs nothing
// up front; tensor bytes are read on demand (the page cache pages them in).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>

#include "q4t/status.h"

namespace q4t {
namespace io {

// Element dtype of a safetensors tensor.
enum class Dtype {
  kF64,
  kF32,
  kF16,
  kBF16,
  kI64,
  kI32,
  kI16,
  kI8,
  kU8,
  kBool,
  kF8E4M3,
  kF8E5M2,
};

// Byte size of one element for a dtype.
size_t DtypeSize(Dtype dtype);

// Parse a safetensors dtype string (e.g. "BF16", "F32", "I64", "F8_E4M3").
// Returns false on an unknown string.
bool ParseDtype(const std::string& s, Dtype* out);

// A tensor's metadata (no data pointer; data is read on demand).
struct TensorInfo {
  std::string name;
  Dtype dtype = Dtype::kF32;
  std::vector<int64_t> shape;
  uint64_t data_start = 0;  // byte offset into the data region
  uint64_t data_end = 0;    // exclusive
  uint64_t numel() const;
  uint64_t byte_size() const;
};

// A read-only, mmap'd .safetensors file.
class SafetensorsFile {
 public:
  // Open and parse the header. Does not read tensor data.
  static Status Open(const std::string& path, SafetensorsFile** out);
  ~SafetensorsFile();
  SafetensorsFile(const SafetensorsFile&) = delete;
  SafetensorsFile& operator=(const SafetensorsFile&) = delete;

  const std::vector<TensorInfo>& tensors() const;
  // Look up a tensor by name; nullptr if absent.
  const TensorInfo* Find(const std::string& name) const;
  size_t num_tensors() const;

  // Read a tensor's bytes into `dst` (must be at least byte_size()).
  Status ReadTensor(const TensorInfo& t, void* dst) const;
  // Read a tensor's bytes into `dst`, then H2D-copy to `device_dst` on
  // `stream` (both must be at least byte_size()).
  Status ReadTensorToDevice(const TensorInfo& t, void* dst, void* device_dst,
                            cudaStream_t stream) const;

  uint64_t file_size() const;

 private:
  struct Impl;
  explicit SafetensorsFile(Impl* impl);
  Impl* impl_;
};

}  // namespace io
}  // namespace q4t
