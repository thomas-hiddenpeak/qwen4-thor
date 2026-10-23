// Run after the real HTTP gate. Expected vectors come from Python hashlib;
// compare automatic dispatch, forced portable, incremental and file paths.
#include "q4t/trace/sha256.h"

#include <array>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using q4t::trace::Digest;
using q4t::trace::HexDigest;
void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

std::vector<uint8_t> Bytes(const std::string& type, size_t length) {
  std::vector<uint8_t> bytes(length);
  const std::string nist =
      "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
  for (size_t i = 0; i < length; ++i) {
    if (type == "pattern")
      bytes[i] = (i * 131 + i / 7) & 255;
    else if (type == "a")
      bytes[i] = 'a';
    else if (type == "abc")
      bytes[i] = "abc"[i % 3];
    else if (type == "nist")
      bytes[i] = nist[i % nist.size()];
    else
      throw std::runtime_error("unknown vector type");
  }
  return bytes;
}

void FileCheck(const std::filesystem::path& path, const std::string& expected,
               bool portable) {
  Digest digest{};
  const auto start = std::chrono::steady_clock::now();
  const auto status =
      portable ? q4t::trace::HashFilePortableForTest(path.string(), &digest)
               : q4t::trace::HashFile(path.string(), &digest);
  const double seconds =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - start)
          .count();
  Require(status.ok(), status.message());
  Require(HexDigest(digest) == expected, "file digest: " + path.string());
  std::cout << "file=" << path << " portable=" << portable
            << " seconds=" << seconds << " sha256=" << HexDigest(digest)
            << '\n';
}
}  // namespace

int main(int argc, char** argv) {
  try {
    Require(argc >= 3 && (argc - 3) % 2 == 0,
            "usage: contract vectors.tsv output-dir [file sha256]...");
    std::ifstream vectors(argv[1]);
    Require(vectors.good(), "cannot open vectors");
    const std::filesystem::path output(argv[2]);
    Require(std::filesystem::create_directory(output), "output already exists");
    const bool hardware = q4t::trace::Sha256HardwareForTest();
    std::cout << "hardware_sha2=" << hardware << '\n';
#if defined(__aarch64__) && defined(__linux__)
    Require(hardware, "Thor acceptance requires the hardware path");
#endif
    std::string type, expected;
    size_t length = 0, checked = 0, vector_count = 0;
    while (vectors >> type >> length >> expected) {
      const auto bytes = Bytes(type, length);
      for (size_t offset : {0, 1, 7, 31}) {
        std::vector<uint8_t> storage(offset, 0x5a);
        storage.insert(storage.end(), bytes.begin(), bytes.end());
        const auto span = std::span<const uint8_t>(storage).subspan(offset);
        Require(HexDigest(q4t::trace::Sha256(span)) == expected,
                "automatic digest length=" + std::to_string(length));
        ++checked;
        for (bool portable : {false, true}) {
          for (size_t chunk : {0, 1, 7, 63, 64, 65, 65536}) {
            Require(HexDigest(q4t::trace::Sha256ChunksForTest(
                        span, chunk, portable)) == expected,
                    "chunk digest length=" + std::to_string(length));
            ++checked;
          }
        }
      }
      const auto file_path =
          output / ("vector-" + std::to_string(vector_count));
      {
        std::ofstream file(file_path, std::ios::binary);
        file.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
        Require(file.good(), "fixture write failed");
      }
      FileCheck(file_path, expected, false);
      FileCheck(file_path, expected, true);
      std::array<std::future<Digest>, 4> jobs;
      for (auto& job : jobs) {
        job = std::async(std::launch::async,
                         [&bytes] { return q4t::trace::Sha256(bytes); });
      }
      for (auto& job : jobs) {
        Require(HexDigest(job.get()) == expected, "concurrent digest");
        ++checked;
      }
      ++vector_count;
    }
    Require(vectors.eof() && vector_count >= 20, "incomplete vector list");
    Digest sentinel;
    sentinel.fill(0xa5);
    for (const auto& path : {output / "missing", output}) {
      for (bool portable : {false, true}) {
        Digest digest = sentinel;
        const auto status =
            portable
                ? q4t::trace::HashFilePortableForTest(path.string(), &digest)
                : q4t::trace::HashFile(path.string(), &digest);
        Require(!status.ok() && digest == sentinel, "file failure contract");
      }
    }
    for (int i = 3; i < argc; i += 2) {
      FileCheck(argv[i], argv[i + 1], false);
      FileCheck(argv[i], argv[i + 1], true);
    }
    std::cout << "PASS vectors=" << vector_count << " digests=" << checked
              << " file_errors=4\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
