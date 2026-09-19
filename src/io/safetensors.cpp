// Safetensors reader implementation.
#include "q4t/io/safetensors.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <unordered_map>

#include "q4t/io/json.h"

namespace q4t {
namespace io {

size_t DtypeSize(Dtype dtype) {
  switch (dtype) {
    case Dtype::kF64:
    case Dtype::kI64:
      return 8;
    case Dtype::kF32:
    case Dtype::kI32:
      return 4;
    case Dtype::kF16:
    case Dtype::kBF16:
    case Dtype::kI16:
      return 2;
    case Dtype::kF8E4M3:
    case Dtype::kF8E5M2:
    case Dtype::kI8:
    case Dtype::kU8:
    case Dtype::kBool:
      return 1;
  }
  return 0;
}

bool ParseDtype(const std::string& s, Dtype* out) {
  if (s == "F64") *out = Dtype::kF64;
  else if (s == "F32") *out = Dtype::kF32;
  else if (s == "F16") *out = Dtype::kF16;
  else if (s == "BF16") *out = Dtype::kBF16;
  else if (s == "I64") *out = Dtype::kI64;
  else if (s == "I32") *out = Dtype::kI32;
  else if (s == "I16") *out = Dtype::kI16;
  else if (s == "I8") *out = Dtype::kI8;
  else if (s == "U8") *out = Dtype::kU8;
  else if (s == "BOOL") *out = Dtype::kBool;
  else if (s == "F8_E4M3" || s == "FP8_E4M3") *out = Dtype::kF8E4M3;
  else if (s == "F8_E5M2" || s == "FP8_E5M2") *out = Dtype::kF8E5M2;
  else return false;
  return true;
}

uint64_t TensorInfo::numel() const {
  uint64_t n = 1;
  for (int64_t d : shape) n *= static_cast<uint64_t>(d);
  return n;
}

uint64_t TensorInfo::byte_size() const {
  return numel() * DtypeSize(dtype);
}

struct SafetensorsFile::Impl {
  int fd = -1;
  void* map = nullptr;
  size_t map_len = 0;
  uint64_t data_offset = 0;  // where the data region starts (after header)
  std::vector<TensorInfo> tensors;
  // name -> index into `tensors`, built once in Open. Find() becomes O(1)
  // instead of a linear scan over ~1500 tensors/shard; the MoE load does
  // ~5120 lookups per layer, so this matters.
  std::unordered_map<std::string, size_t> name_index;
};

SafetensorsFile::SafetensorsFile(Impl* impl) : impl_(impl) {}

SafetensorsFile::~SafetensorsFile() {
  if (!impl_) return;
  // Evict this shard's clean file pages from the page cache BEFORE unmapping.
  // On Jetson Thor's 122 GB unified memory, the ~84 GB of mmap'd weight
  // shards would otherwise stay resident in the page cache (munmap only
  // drops the mapping, not the clean file pages) and starve the CUDA driver
  // of the memory it needs for runtime allocations. POSIX_FADV_DONTNEED
  // actively reclaims them. (Qwen3x-Orin avoids this entirely by reading
  // shards with ::read into pinned staging instead of mmap.)
  if (impl_->fd >= 0) {
    posix_fadvise(impl_->fd, 0, 0, POSIX_FADV_DONTNEED);
  }
  if (impl_->map) munmap(impl_->map, impl_->map_len);
  if (impl_->fd >= 0) close(impl_->fd);
  delete impl_;
  impl_ = nullptr;
}

Status SafetensorsFile::Open(const std::string& path, SafetensorsFile** out) {
  *out = nullptr;
  Impl* impl = new Impl();

  impl->fd = open(path.c_str(), O_RDONLY);
  if (impl->fd < 0) {
    std::string msg = "open(" + path + ") failed: ";
    msg += std::strerror(errno);
    delete impl;
    return Status::Fail(msg);
  }
  struct stat st;
  if (fstat(impl->fd, &st) != 0) {
    close(impl->fd);
    delete impl;
    return Status::Fail("fstat failed");
  }
  const size_t file_size = static_cast<size_t>(st.st_size);
  if (file_size < 8) {
    close(impl->fd);
    delete impl;
    return Status::Fail("file too small for safetensors header");
  }

  impl->map = mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE, impl->fd, 0);
  if (impl->map == MAP_FAILED) {
    close(impl->fd);
    delete impl;
    return Status::Fail("mmap failed");
  }
  impl->map_len = file_size;
  const char* base = static_cast<const char*>(impl->map);

  // Header length: first 8 bytes, little-endian u64.
  uint64_t header_len = 0;
  std::memcpy(&header_len, base, 8);
  if (8 + header_len > file_size) {
    delete impl;
    return Status::Fail("header length exceeds file size");
  }
  const std::string header(base + 8, header_len);
  impl->data_offset = 8 + header_len;

  Json root;
  Status s = ParseJson(header, &root);
  if (!s.ok()) {
    delete impl;
    return s;
  }
  if (!root.IsObject()) {
    delete impl;
    return Status::Fail("safetensors header is not an object");
  }

  for (const auto& kv : root.object) {
    if (kv.first == "__metadata__") continue;
    const Json& v = kv.second;
    if (!v.IsObject()) {
      delete impl;
      return Status::Fail("tensor entry is not an object: " + kv.first);
    }
    TensorInfo info;
    info.name = kv.first;
    const std::string dtype_str = v.GetString("dtype");
    if (!ParseDtype(dtype_str, &info.dtype)) {
      delete impl;
      return Status::Fail("unknown dtype '" + dtype_str + "' for " +
                          kv.first);
    }
    const Json* shape = v.GetArray("shape");
    if (!shape) {
      delete impl;
      return Status::Fail("missing shape for " + kv.first);
    }
    for (const Json& d : shape->array) {
      info.shape.push_back(d.AsInt());
    }
    const Json* offsets = v.GetArray("data_offsets");
    if (!offsets || offsets->array.size() != 2) {
      delete impl;
      return Status::Fail("missing data_offsets for " + kv.first);
    }
    info.data_start = static_cast<uint64_t>(offsets->array[0].AsInt());
    info.data_end = static_cast<uint64_t>(offsets->array[1].AsInt());
    impl->tensors.push_back(std::move(info));
  }

  impl->name_index.reserve(impl->tensors.size());
  for (size_t i = 0; i < impl->tensors.size(); ++i) {
    impl->name_index.emplace(impl->tensors[i].name, i);
  }

  *out = new SafetensorsFile(impl);
  return Status();
}

const std::vector<TensorInfo>& SafetensorsFile::tensors() const {
  return impl_->tensors;
}

const TensorInfo* SafetensorsFile::Find(const std::string& name) const {
  auto it = impl_->name_index.find(name);
  if (it == impl_->name_index.end()) return nullptr;
  return &impl_->tensors[it->second];
}

size_t SafetensorsFile::num_tensors() const { return impl_->tensors.size(); }

uint64_t SafetensorsFile::file_size() const { return impl_->map_len; }

Status SafetensorsFile::ReadTensor(const TensorInfo& t, void* dst) const {
  const uint64_t bytes = t.byte_size();
  if (impl_->data_offset + t.data_end > impl_->map_len) {
    return Status::Fail("tensor " + t.name + " extends past file end");
  }
  // pread (not mmap+memcpy): one kernel call copies the whole range,
  // thread-safely (no shared-fd lseek race under the parallel MoE load) and
  // without the per-page user-space minor faults the mmap path incurred on
  // first touch. Each layer's ~1.26 GB of expert weights is ~320K 4 KB pages;
  // faulting them one-by-one cost ~350 ms/layer (matching the measured
  // 363 ms/layer floor) even with the data already in the page cache.
  // Reference: Qwen3x-Orin reads shards with ::read for the same reason.
  const off_t base = static_cast<off_t>(impl_->data_offset + t.data_start);
  size_t got = 0;
  while (got < bytes) {
    const ssize_t r = pread(impl_->fd, static_cast<char*>(dst) + got,
                            bytes - got, base + static_cast<off_t>(got));
    if (r < 0) {
      if (errno == EINTR) continue;
      return Status::Fail("pread failed for " + t.name);
    }
    if (r == 0) break;  // unexpected EOF
    got += static_cast<size_t>(r);
  }
  if (got != bytes) return Status::Fail("short pread for " + t.name);
  return Status();
}

Status SafetensorsFile::ReadTensorToDevice(const TensorInfo& t, void* dst,
                                           void* device_dst,
                                           cudaStream_t stream) const {
  Status s = ReadTensor(t, dst);
  if (!s.ok()) return s;
  if (cudaMemcpyAsync(device_dst, dst, t.byte_size(), cudaMemcpyHostToDevice,
                      stream) != cudaSuccess) {
    return Status::Fail("H2D copy failed for " + t.name);
  }
  return Status();
}

}  // namespace io
}  // namespace q4t
