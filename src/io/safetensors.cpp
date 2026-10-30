// Safetensors reader implementation.
#include "q4t/io/safetensors.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <limits>
#include <memory>
#include <unordered_map>
#include <vector>

#include "q4t/io/json.h"

namespace q4t {
namespace io {

namespace {

bool UnsignedInteger(const Json& value, uint64_t* out) {
  if (!value.IsNumber() || value.str.empty()) return false;
  const char* end = value.str.data() + value.str.size();
  const auto result = std::from_chars(value.str.data(), end, *out);
  return result.ec == std::errc{} && result.ptr == end;
}

bool TensorBytes(const TensorInfo& tensor, uint64_t* bytes) {
  const size_t element_bytes = DtypeSize(tensor.dtype);
  if (element_bytes == 0) return false;
  bool empty = false;
  for (int64_t dimension : tensor.shape) {
    if (dimension < 0) return false;
    empty |= dimension == 0;
  }
  uint64_t count = empty ? 0 : 1;
  if (!empty) {
    for (int64_t dimension : tensor.shape) {
      const auto size = static_cast<uint64_t>(dimension);
      if (size > std::numeric_limits<uint64_t>::max() / count) return false;
      count *= size;
    }
  }
  if (count > std::numeric_limits<uint64_t>::max() / element_bytes)
    return false;
  *bytes = count * element_bytes;
  return true;
}

}  // namespace

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
  void* map = MAP_FAILED;
  size_t map_len = 0;
  // When true, the destructor skips POSIX_FADV_DONTNEED so this file's
  // page-cache pages survive shard-LRU eviction (tiered MoE residency).
  bool keep_page_cache = false;
  uint64_t data_offset = 0;  // where the data region starts (after header)
  std::vector<TensorInfo> tensors;
  // name -> index into `tensors`, built once in Open. Find() becomes O(1)
  // instead of a linear scan over ~1500 tensors/shard; the MoE load does
  // ~5120 lookups per layer, so this matters.
  std::unordered_map<std::string, size_t> name_index;

  ~Impl() {
    // Failed Open owns these resources too. Keep the same page-cache policy
    // on success, but release fd/map on every early return or exception.
    if (fd >= 0 && !keep_page_cache)
      posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
    if (map != MAP_FAILED) munmap(map, map_len);
    if (fd >= 0) close(fd);
  }
};

SafetensorsFile::SafetensorsFile(Impl* impl) : impl_(impl) {}

SafetensorsFile::~SafetensorsFile() {
  delete impl_;
  impl_ = nullptr;
}

void SafetensorsFile::SetKeepPageCache(bool keep) {
  impl_->keep_page_cache = keep;
}

Status SafetensorsFile::Open(const std::string& path, SafetensorsFile** out) {
  if (!out) return Status::Fail("missing safetensors output");
  *out = nullptr;
  auto impl = std::make_unique<Impl>();

  impl->fd = open(path.c_str(), O_RDONLY);
  if (impl->fd < 0) {
    std::string msg = "open(" + path + ") failed: ";
    msg += std::strerror(errno);
    return Status::Fail(msg);
  }
  struct stat st;
  if (fstat(impl->fd, &st) != 0) {
    return Status::Fail("fstat failed");
  }
  if (st.st_size < 8 || static_cast<uint64_t>(st.st_size) >
                            std::numeric_limits<size_t>::max()) {
    return Status::Fail("file too small for safetensors header");
  }
  const size_t file_size = static_cast<size_t>(st.st_size);

  impl->map = mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE, impl->fd, 0);
  if (impl->map == MAP_FAILED) {
    return Status::Fail("mmap failed");
  }
  impl->map_len = file_size;
  const char* base = static_cast<const char*>(impl->map);

  // Header length: first 8 bytes, little-endian u64.
  uint64_t header_len = 0;
  std::memcpy(&header_len, base, 8);
  if (header_len > file_size - 8) {
    return Status::Fail("header length exceeds file size");
  }
  const std::string header(base + 8, header_len);
  impl->data_offset = 8 + header_len;

  Json root;
  JsonParseLimits limits;
  limits.reject_duplicate_keys = true;
  // Keep integer spelling: a double would silently round offsets above 2^53.
  Status s = ParseJson(header, &root, true, limits);
  if (!s.ok()) {
    return s;
  }
  if (!root.IsObject()) {
    return Status::Fail("safetensors header is not an object");
  }

  for (const auto& kv : root.object) {
    if (kv.first == "__metadata__") continue;
    const Json& v = kv.second;
    if (!v.IsObject()) {
      return Status::Fail("tensor entry is not an object: " + kv.first);
    }
    TensorInfo info;
    info.name = kv.first;
    const std::string dtype_str = v.GetString("dtype");
    if (!ParseDtype(dtype_str, &info.dtype)) {
      return Status::Fail("unknown dtype '" + dtype_str + "' for " +
                          kv.first);
    }
    const Json* shape = v.GetArray("shape");
    if (!shape) {
      return Status::Fail("missing shape for " + kv.first);
    }
    for (const Json& d : shape->array) {
      uint64_t dimension = 0;
      if (!UnsignedInteger(d, &dimension) ||
          dimension > static_cast<uint64_t>(
                          std::numeric_limits<int64_t>::max())) {
        return Status::Fail("invalid shape for " + kv.first);
      }
      info.shape.push_back(static_cast<int64_t>(dimension));
    }
    const Json* offsets = v.GetArray("data_offsets");
    if (!offsets || offsets->array.size() != 2) {
      return Status::Fail("missing data_offsets for " + kv.first);
    }
    uint64_t bytes = 0;
    if (!UnsignedInteger(offsets->array[0], &info.data_start) ||
        !UnsignedInteger(offsets->array[1], &info.data_end) ||
        !TensorBytes(info, &bytes) || info.data_end < info.data_start ||
        info.data_end > file_size - impl->data_offset ||
        info.data_end - info.data_start != bytes) {
      return Status::Fail("invalid tensor extent for " + kv.first);
    }
    impl->tensors.push_back(std::move(info));
  }

  std::vector<std::pair<uint64_t, uint64_t>> intervals;
  for (const auto& tensor : impl->tensors) {
    if (tensor.data_start != tensor.data_end)
      intervals.emplace_back(tensor.data_start, tensor.data_end);
  }
  std::sort(intervals.begin(), intervals.end());
  uint64_t previous_end = 0;
  for (const auto& [start, end] : intervals) {
    if (start < previous_end)
      return Status::Fail("overlapping safetensors tensors");
    previous_end = end;
  }

  impl->name_index.reserve(impl->tensors.size());
  for (size_t i = 0; i < impl->tensors.size(); ++i) {
    impl->name_index.emplace(impl->tensors[i].name, i);
  }

  *out = new SafetensorsFile(impl.get());
  impl.release();
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
  uint64_t bytes = 0;
  if (!TensorBytes(t, &bytes) || t.data_end < t.data_start ||
      t.data_end - t.data_start != bytes) {
    return Status::Fail("invalid tensor extent for " + t.name);
  }
  return ReadRange(t.data_start, bytes, dst);
}

Status SafetensorsFile::ReadRange(uint64_t data_start, uint64_t length,
                                  void* dst) const {
  const uint64_t data_bytes = impl_->map_len - impl_->data_offset;
  if (data_start > data_bytes || length > data_bytes - data_start) {
    return Status::Fail("read range extends past file end");
  }
  if (length && !dst) return Status::Fail("missing read range buffer");
  // File size is bounded by off_t; subtraction above bounds every position.
  // pread is thread-safe and avoids mmap's per-page user-space minor faults.
  const off_t base = static_cast<off_t>(impl_->data_offset + data_start);
  size_t got = 0;
  while (got < length) {
    const ssize_t r = pread(impl_->fd, static_cast<char*>(dst) + got,
                            length - got, base + static_cast<off_t>(got));
    if (r < 0) {
      if (errno == EINTR) continue;
      return Status::Fail("pread failed for range");
    }
    if (r == 0) break;  // unexpected EOF
    got += static_cast<size_t>(r);
  }
  if (got != length) return Status::Fail("short pread for range");
  return Status();
}

Status SafetensorsFile::ReadRangev(uint64_t data_start, size_t count,
                                  const void* const* dsts,
                                  const size_t* lens) const {
  if (count > 0 && (!dsts || !lens)) {
    return Status::Fail("missing read range buffers");
  }
  size_t total = 0;
  for (size_t i = 0; i < count; ++i) {
    if (lens[i] > std::numeric_limits<size_t>::max() - total) {
      return Status::Fail("read range size overflow");
    }
    if (lens[i] && !dsts[i]) return Status::Fail("missing read range buffer");
    total += lens[i];
  }
  const uint64_t data_bytes = impl_->map_len - impl_->data_offset;
  if (data_start > data_bytes || total > data_bytes - data_start) {
    return Status::Fail("read range extends past file end");
  }
  if (total == 0) return Status();
  // preadv: one syscall copies the contiguous file range into the
  // scattered destinations (kernel-side scatter, no user-space copy).
  // Omit empty entries so partial-read advancement always makes progress.
  std::vector<struct iovec> iov;
  iov.reserve(count);
  for (size_t i = 0; i < count; ++i) {
    if (lens[i] > 0) iov.push_back({const_cast<void*>(dsts[i]), lens[i]});
  }
  static const long max_iov = sysconf(_SC_IOV_MAX);
  if (max_iov <= 0) return Status::Fail("cannot query preadv iovec limit");
  const off_t base = static_cast<off_t>(impl_->data_offset + data_start);
  size_t done = 0;
  size_t idx = 0;
  while (done < total) {
    const size_t remaining = iov.size() - idx;
    const int batch = static_cast<int>(
        remaining < static_cast<size_t>(max_iov) ? remaining : max_iov);
    const ssize_t r = preadv(impl_->fd, iov.data() + idx,
                            batch, base + static_cast<off_t>(done));
    if (r < 0) {
      if (errno == EINTR) continue;
      return Status::Fail("preadv failed for range");
    }
    if (r == 0) break;  // unexpected EOF
    done += static_cast<size_t>(r);
    size_t rem = static_cast<size_t>(r);
    while (rem > 0) {
      const size_t space = iov[idx].iov_len;
      if (rem < space) {
        iov[idx].iov_base = static_cast<char*>(iov[idx].iov_base) + rem;
        iov[idx].iov_len -= rem;
        rem = 0;
      } else {
        rem -= space;
        ++idx;
      }
    }
  }
  if (done != total) return Status::Fail("short preadv for range");
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
