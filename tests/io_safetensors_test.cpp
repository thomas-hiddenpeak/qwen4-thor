// Tests for the safetensors reader.
//
// Builds a small synthetic .safetensors file in /tmp (8-byte header length +
// JSON header + data) to verify header parsing, tensor metadata, and byte
// reads. Also opens the real model's tiny scale file (if present) to confirm
// the reader handles a genuine file.
#include "q4t/io/safetensors.h"
#include "q4t/test.h"

#include <fcntl.h>
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
    std::printf("  (skipped: real scale file not present)\n");
    return true;
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
