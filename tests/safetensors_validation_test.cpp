// Metadata and lifetime contracts using synthetic files, without CUDA.
#include "q4t/io/safetensors.h"
#include "q4t/test.h"

#include <sys/mman.h>
#include <unistd.h>

#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>

namespace {

using q4t::io::SafetensorsFile;
bool fail_after_map = false;
bool fail_new = false;

class Fixture {
 public:
  Fixture() {
    std::filesystem::create_directories(Q4T_IO_FIXTURE_ROOT);
    dir_ = std::string(Q4T_IO_FIXTURE_ROOT) + "/validation-XXXXXX";
    if (!mkdtemp(dir_.data())) throw std::runtime_error("mkdtemp failed");
  }
  ~Fixture() { std::filesystem::remove_all(dir_); }

  std::string Write(const std::string& header, const std::string& data = {}) {
    const std::string path = dir_ + "/file-" + std::to_string(next_++);
    std::ofstream file(path, std::ios::binary);
    const uint64_t size = header.size();
    file.write(reinterpret_cast<const char*>(&size), sizeof(size));
    file << header << data;
    return path;
  }
  const std::string& dir() const { return dir_; }

 private:
  std::string dir_;
  int next_ = 0;
};

size_t Fds() {
  return std::distance(std::filesystem::directory_iterator("/proc/self/fd"),
                       std::filesystem::directory_iterator{});
}

size_t Maps(const std::string& directory) {
  std::ifstream maps("/proc/self/maps");
  std::string line;
  size_t count = 0;
  while (std::getline(maps, line))
    if (line.find(directory) != std::string::npos) ++count;
  return count;
}

bool Rejects(const std::string& path) {
  SafetensorsFile* raw = nullptr;
  const auto status = SafetensorsFile::Open(path, &raw);
  const bool rejected = !status.ok() && !raw;
  delete raw;
  return rejected;
}

constexpr const char* kValid =
    R"({"a":{"dtype":"U8","shape":[4],"data_offsets":[0,4]}})";

}  // namespace

extern "C" void* __real_mmap(void*, size_t, int, int, int, off_t);
extern "C" void* __wrap_mmap(void* address, size_t length, int protection,
                             int flags, int fd, off_t offset) {
  void* result = __real_mmap(address, length, protection, flags, fd, offset);
  if (result != MAP_FAILED && fail_after_map) {
    fail_after_map = false;
    fail_new = true;
  }
  return result;
}

extern "C" void* __real__Znwm(size_t);
extern "C" void* __wrap__Znwm(size_t bytes) {
  if (fail_new) {
    fail_new = false;
    throw std::bad_alloc();
  }
  return __real__Znwm(bytes);
}

Q4T_TEST(safetensors_rejects_invalid_integer_shapes_and_extents) {
  Fixture fixture;
  const std::array<const char*, 15> bad = {
      {R"({"a":{"dtype":"U8","shape":[4],"data_offsets":[0,1]}})",
       R"({"a":{"dtype":"U8","shape":[-1],"data_offsets":[0,4]}})",
       R"({"a":{"dtype":"U8","shape":[1.5],"data_offsets":[0,1]}})",
       R"({"a":{"dtype":"U8","shape":["1"],"data_offsets":[0,1]}})",
       R"({"a":{"dtype":"U8","shape":[true],"data_offsets":[0,1]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[-1,0]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[1,0]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[0,1.0]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[0,"1"]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[0,5]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[0,18446744073709551616]}})",
       R"({"a":{"dtype":"U8","shape":[9223372036854775808],"data_offsets":[0,0]}})",
       R"({"a":{"dtype":"U8","shape":[9223372036854775807,3],"data_offsets":[0,0]}})",
       R"({"a":{"dtype":"F64","shape":[4611686018427387904],"data_offsets":[0,0]}})",
       R"({"a":{"dtype":"U8","shape":[1],"data_offsets":[18446744073709551615,0]}})"}};
  for (const char* header : bad)
    Q4T_CHECK(Rejects(fixture.Write(header, "ABCD")));
  return true;
}

Q4T_TEST(safetensors_rejects_overlap_and_duplicate_keys) {
  Fixture fixture;
  Q4T_CHECK(Rejects(
      fixture.Write(R"({"a":{"dtype":"U8","shape":[3],"data_offsets":[0,3]},)"
                    R"("b":{"dtype":"U8","shape":[2],"data_offsets":[2,4]}})",
                    "ABCD")));
  Q4T_CHECK(
      Rejects(fixture.Write(R"({"a":{"dtype":"U8","shape":[1],"shape":[4],)"
                            R"("data_offsets":[0,4]}})",
                            "ABCD")));
  Q4T_CHECK(Rejects(
      fixture.Write(R"({"a":{"dtype":"U8","shape":[2],"data_offsets":[0,2]},)"
                    R"("a":{"dtype":"U8","shape":[2],"data_offsets":[2,4]}})",
                    "ABCD")));
  return true;
}

Q4T_TEST(safetensors_preserves_scalar_empty_and_exact_large_dimensions) {
  Fixture fixture;
  const auto path = fixture.Write(
      R"({"scalar":{"dtype":"U8","shape":[],"data_offsets":[0,1]},)"
      R"("empty":{"dtype":"F64","shape":[0,9007199254740993],)"
      R"("data_offsets":[1,1]}})",
      "Z");
  SafetensorsFile* raw = nullptr;
  Q4T_CHECK(SafetensorsFile::Open(path, &raw).ok());
  std::unique_ptr<SafetensorsFile> file(raw);
  const auto* empty = file->Find("empty");
  Q4T_CHECK(empty && empty->shape[1] == 9007199254740993LL);
  Q4T_CHECK(file->ReadTensor(*empty, nullptr).ok());
  char value = 0;
  Q4T_CHECK(file->ReadTensor(*file->Find("scalar"), &value).ok());
  Q4T_CHECK(value == 'Z');
  return true;
}

Q4T_TEST(safetensors_range_and_forged_tensor_reject_before_writing) {
  Fixture fixture;
  SafetensorsFile* raw = nullptr;
  Q4T_CHECK(SafetensorsFile::Open(fixture.Write(kValid, "ABCD"), &raw).ok());
  std::unique_ptr<SafetensorsFile> file(raw);
  std::array<char, 4> bytes = {'x', 'x', 'x', 'x'};
  const auto initial = bytes;
  Q4T_CHECK(!file->ReadRange(UINT64_MAX, 2, bytes.data()).ok());
  Q4T_CHECK(!file->ReadRange(0, UINT64_MAX, bytes.data()).ok());
  Q4T_CHECK(!file->ReadRange(0, 1, nullptr).ok());
  Q4T_CHECK(file->ReadRange(4, 0, nullptr).ok());
  auto forged = *file->Find("a");
  for (const auto& shape : std::vector<std::vector<int64_t>>{
           {-1}, {std::numeric_limits<int64_t>::max(), 3}, {3}}) {
    forged.shape = shape;
    Q4T_CHECK(!file->ReadTensor(forged, bytes.data()).ok());
  }
  forged = *file->Find("a");
  forged.data_start = UINT64_MAX - 3;
  forged.data_end = 0;
  Q4T_CHECK(!file->ReadTensor(forged, bytes.data()).ok());
  const void* destinations[] = {nullptr};
  const size_t lengths[] = {1};
  Q4T_CHECK(!file->ReadRangev(0, 1, destinations, lengths).ok());
  Q4T_CHECK(bytes == initial);
  Q4T_CHECK(file->ReadTensor(*file->Find("a"), bytes.data()).ok());
  Q4T_CHECK(std::string(bytes.data(), bytes.size()) == "ABCD");
  return true;
}

Q4T_TEST(safetensors_failed_open_releases_fd_and_mapping) {
  Fixture fixture;
  for (const std::string header :
       {"!", "[]", R"({"a":null})", R"({"a":{"dtype":"bad"}})",
        R"({"a":{"dtype":"U8"}})", R"({"a":{"dtype":"U8","shape":[1]}})"}) {
    const auto path = fixture.Write(header);
    const auto fds = Fds(), maps = Maps(fixture.dir());
    for (int n = 0; n < 12; ++n) Q4T_CHECK(Rejects(path));
    Q4T_CHECK(Fds() == fds && Maps(fixture.dir()) == maps);
  }
  const auto path = fixture.Write("!");
  {
    std::fstream file(path, std::ios::binary | std::ios::in | std::ios::out);
    const uint64_t length = UINT64_MAX;
    file.write(reinterpret_cast<const char*>(&length), sizeof(length));
  }
  const auto fds = Fds(), maps = Maps(fixture.dir());
  Q4T_CHECK(Rejects(path));
  Q4T_CHECK(Fds() == fds && Maps(fixture.dir()) == maps);
  return true;
}

Q4T_TEST(safetensors_allocation_exception_releases_fd_and_mapping) {
  Fixture fixture;
  const auto path = fixture.Write(kValid, "ABCD");
  const auto fds = Fds(), maps = Maps(fixture.dir());
  SafetensorsFile* raw = nullptr;
  bool caught = false;
  fail_after_map = true;
  try {
    SafetensorsFile::Open(path, &raw);
  } catch (const std::bad_alloc&) {
    caught = true;
  }
  fail_after_map = fail_new = false;
  Q4T_CHECK(caught && !raw);
  Q4T_CHECK(Fds() == fds && Maps(fixture.dir()) == maps);
  return true;
}
