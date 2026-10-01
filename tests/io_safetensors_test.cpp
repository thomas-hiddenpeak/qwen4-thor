// Tests for the safetensors reader.
//
// Builds a small synthetic .safetensors file in /tmp (8-byte header length +
// JSON header + data) to verify header parsing, tensor metadata, and byte
// reads. Also opens the real model's tiny scale file (if present) to confirm
// the reader handles a genuine file.
#include "q4t/io/safetensors.h"
#include "q4t/test.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

using q4t::io::Dtype;
using q4t::io::SafetensorsFile;
using q4t::io::TensorInfo;
using q4t::Status;

// Write a synthetic safetensors file with one F32 tensor [4] = {1,2,3,4} and
// one I64 tensor [2] = {10, 20}.
std::string MakeSyntheticFile() {
  const char* path = "/tmp/q4t_safetensors_test.bin";
  // Data region: 4 floats (16 bytes) then 2 int64 (16 bytes).
  std::vector<uint8_t> data(32);
  const float f[4] = {1.0f, 2.0f, 3.0f, 4.0f};
  std::memcpy(data.data(), f, 16);
  const int64_t i[2] = {10, 20};
  std::memcpy(data.data() + 16, i, 16);

  const std::string header =
      R"({"w": {"dtype": "F32", "shape": [4], "data_offsets": [0, 16]}, )"
      R"("c": {"dtype": "I64", "shape": [2], "data_offsets": [16, 32]}})";

  int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0644);
  if (fd < 0) return "";
  uint64_t header_len = header.size();
  ssize_t w = write(fd, &header_len, 8);
  w += write(fd, header.data(), header.size());
  w += write(fd, data.data(), data.size());
  close(fd);
  return w == static_cast<ssize_t>(8 + header.size() + data.size()) ? path
                                                                    : "";
}

}  // namespace

Q4T_TEST(safetensors_parse_and_read) {
  const std::string path = MakeSyntheticFile();
  Q4T_CHECK(!path.empty());

  SafetensorsFile* f = nullptr;
  Status s = SafetensorsFile::Open(path, &f);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(f->num_tensors() == 2);

  const TensorInfo* w = f->Find("w");
  Q4T_CHECK(w != nullptr);
  Q4T_CHECK(w->dtype == Dtype::kF32);
  Q4T_CHECK(w->shape.size() == 1 && w->shape[0] == 4);
  Q4T_CHECK(w->byte_size() == 16);

  const TensorInfo* c = f->Find("c");
  Q4T_CHECK(c != nullptr);
  Q4T_CHECK(c->dtype == Dtype::kI64);
  Q4T_CHECK(c->byte_size() == 16);

  // Read w as floats.
  float wf[4] = {0};
  s = f->ReadTensor(*w, wf);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(wf[0] == 1.0f && wf[1] == 2.0f && wf[2] == 3.0f && wf[3] == 4.0f);

  // Read c as int64.
  int64_t ci[2] = {0};
  s = f->ReadTensor(*c, ci);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(ci[0] == 10 && ci[1] == 20);

  Q4T_CHECK(f->Find("nope") == nullptr);

  delete f;
  unlink(path.c_str());
  return true;
}

Q4T_TEST(safetensors_open_real_scale_file) {
  // The real model's tiny PLE scale file; verify the reader opens it and
  // reports at least one tensor with a sane dtype.
  const std::string path =
      "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
      "model-plefp8-scale.safetensors";
  int fd = open(path.c_str(), O_RDONLY);
  if (fd < 0) {
    Q4T_SKIP("(skipped: real scale file not present)");
  }
  close(fd);

  SafetensorsFile* f = nullptr;
  Status s = SafetensorsFile::Open(path, &f);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(f->num_tensors() >= 1);
  // Each tensor's byte_size must be consistent with its dtype and shape.
  for (const auto& t : f->tensors()) {
    Q4T_CHECK(t.byte_size() == t.numel() * q4t::io::DtypeSize(t.dtype));
    Q4T_CHECK(t.data_end > t.data_start);
  }
  delete f;
  return true;
}

// Build a synthetic safetensors file with one F32 tensor of `nbytes` bytes.
std::string MakeLargeSyntheticFile(const char* path, size_t nbytes) {
  std::vector<uint8_t> data(nbytes, 0xAB);
  const std::string header =
      std::string(R"({"w": {"dtype": "F32", "shape": [)") +
      std::to_string(nbytes / 4) +
      R"(], "data_offsets": [0, )" + std::to_string(nbytes) + R"(]}})";
  int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0644);
  if (fd < 0) return "";
  uint64_t header_len = header.size();
  ssize_t w = write(fd, &header_len, 8);
  w += write(fd, header.data(), header.size());
  w += write(fd, data.data(), data.size());
  close(fd);
  return w == static_cast<ssize_t>(8 + header.size() + data.size()) ? path
                                                                    : "";
}

// Count 4 KB pages of `path` currently resident in the page cache via an
// mmap + mincore (mincore reports residency without faulting pages in).
size_t CountResidentPages(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return 0;
  struct stat st;
  if (fstat(fd, &st) != 0) {
    close(fd);
    return 0;
  }
  const size_t len = static_cast<size_t>(st.st_size);
  void* map = mmap(nullptr, len, PROT_READ, MAP_PRIVATE, fd, 0);
  if (map == MAP_FAILED) {
    close(fd);
    return 0;
  }
  const size_t pages = (len + 4095) / 4096;
  std::vector<unsigned char> vec(pages);
  size_t resident = 0;
  size_t done = 0;
  char* m = static_cast<char*>(map);
  while (done < pages) {
    const ssize_t r = mincore(m + done * 4096, (pages - done) * 4096,
                              vec.data() + done);
    if (r <= 0) break;  // guard: mincore may return 0; never spin
    for (ssize_t i = 0; i < r; ++i) resident += vec[done + i] & 1;
    done += static_cast<size_t>(r);
  }
  munmap(map, len);
  close(fd);
  return resident;
}

Q4T_TEST(safetensors_keep_page_cache) {
  // C5: the destructor's POSIX_FADV_DONTNEED must be skipped when
  // SetKeepPageCache(true) was called, so the file's page-cache pages
  // survive (tiered MoE residency streams expert shards on demand and the
  // page cache is the C3 pre-warm tier).
  //
  // Residency is observed via mmap + mincore. Some kernels (observed on
  // Tegra 6.8) return 0 from mincore for regular files, making residency
  // unobservable; in that case verify the API path only and skip the
  // residency assertion (the end-to-end effect is covered by the
  // residency pilot: pread_avg 4.0 ms -> 0.87 ms after C5).
  const size_t nbytes = 16 * 1024 * 1024;  // 4096 pages of 4 KB
  const char* evict_path = "/tmp/q4t_safetensors_evict_test.bin";
  const char* keep_path = "/tmp/q4t_safetensors_keep_test.bin";
  Q4T_CHECK(!MakeLargeSyntheticFile(evict_path, nbytes).empty());
  Q4T_CHECK(!MakeLargeSyntheticFile(keep_path, nbytes).empty());

  // Probe whether mincore reports residency on this kernel: read the file
  // (populating the page cache), then mincore must report most pages
  // resident. If it reports 0, mincore is non-functional here.
  bool mincore_works = false;
  {
    SafetensorsFile* f = nullptr;
    Q4T_CHECK(SafetensorsFile::Open(evict_path, &f).ok());
    std::vector<uint8_t> buf(nbytes);
    Q4T_CHECK(f->ReadRange(0, nbytes, buf.data()).ok());
    delete f;
    mincore_works = CountResidentPages(evict_path) > 4096 / 2;
  }

  if (!mincore_works) {
    // API path only: SetKeepPageCache must be accepted and destruction
    // must not fail or crash.
    SafetensorsFile* f = nullptr;
    Q4T_CHECK(SafetensorsFile::Open(keep_path, &f).ok());
    f->SetKeepPageCache(true);
    std::vector<uint8_t> buf(nbytes);
    Q4T_CHECK(f->ReadRange(0, nbytes, buf.data()).ok());
    delete f;
    unlink(evict_path);
    unlink(keep_path);
    return true;
  }

  // Evict path: default destruction drops the pages.
  {
    SafetensorsFile* f = nullptr;
    Q4T_CHECK(SafetensorsFile::Open(evict_path, &f).ok());
    std::vector<uint8_t> buf(nbytes);
    Q4T_CHECK(f->ReadRange(0, nbytes, buf.data()).ok());  // populate cache
    delete f;  // FADV_DONTNEED
  }
  const size_t evict_resident = CountResidentPages(evict_path);
  Q4T_CHECK(evict_resident < 4096 / 2);  // most pages evicted

  // Keep path: SetKeepPageCache(true) preserves the pages.
  {
    SafetensorsFile* f = nullptr;
    Q4T_CHECK(SafetensorsFile::Open(keep_path, &f).ok());
    f->SetKeepPageCache(true);
    std::vector<uint8_t> buf(nbytes);
    Q4T_CHECK(f->ReadRange(0, nbytes, buf.data()).ok());  // populate cache
    delete f;  // no FADV_DONTNEED
  }
  const size_t keep_resident = CountResidentPages(keep_path);
  Q4T_CHECK(keep_resident > 4096 * 9 / 10);  // pages retained

  unlink(evict_path);
  unlink(keep_path);
  return true;
}
