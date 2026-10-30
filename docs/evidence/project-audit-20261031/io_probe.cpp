#include "q4t/io/safetensors.h"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>

namespace fs = std::filesystem;
using q4t::io::SafetensorsFile;

void Write(const std::string& path, const std::string& header,
           const std::string& body) {
  std::ofstream f(path, std::ios::binary);
  uint64_t n = header.size();
  f.write(reinterpret_cast<const char*>(&n), sizeof(n));
  f << header << body;
}

size_t Fds() {
  size_t n = 0;
  for (const auto& unused : fs::directory_iterator("/proc/self/fd")) {
    (void)unused;
    ++n;
  }
  return n;
}

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  const std::string dir = argv[1];
  fs::create_directories(dir);
  const std::string header =
      R"({"a":{"dtype":"U8","shape":[4],"data_offsets":[0,1]},"b":{"dtype":"U8","shape":[3],"data_offsets":[1,4]}})";
  Write(dir + "/overlap", header, "ABCD");
  SafetensorsFile* raw = nullptr;
  auto status = SafetensorsFile::Open(dir + "/overlap", &raw);
  std::unique_ptr<SafetensorsFile> file(raw);
  char bytes[4]{};
  auto read = file->ReadTensor(*file->Find("a"), bytes);
  std::cout << "mismatched_extent open=" << status.ok()
            << " read=" << read.ok() << " declared_bytes=1 actual_read="
            << std::string(bytes, 4) << '\n';
  uint64_t got_header = 0;
  const uint64_t offset = uint64_t{0} - (header.size() + 8);
  auto wrapped = file->ReadRange(offset, sizeof(got_header), &got_header);
  std::cout << "wrapped_range accepted=" << wrapped.ok()
            << " read_header=" << (got_header == header.size()) << '\n';

  Write(dir + "/bad-json", "!", "");
  const auto before = Fds();
  for (int i = 0; i < 12; ++i) {
    raw = nullptr;
    status = SafetensorsFile::Open(dir + "/bad-json", &raw);
    if (status.ok() || raw) return 3;
  }
  std::cout << "failed_opens=12 leaked_fds=" << Fds() - before << '\n';

  {
    std::ofstream f(dir + "/header-overflow", std::ios::binary);
    uint64_t n = UINT64_MAX;
    f.write(reinterpret_cast<const char*>(&n), sizeof(n));
  }
  try {
    raw = nullptr;
    status = SafetensorsFile::Open(dir + "/header-overflow", &raw);
    delete raw;
    std::cout << "header_overflow returned_status=" << status.ok() << '\n';
  } catch (const std::exception& error) {
    std::cout << "header_overflow escaped_exception=" << error.what() << '\n';
  }
}
