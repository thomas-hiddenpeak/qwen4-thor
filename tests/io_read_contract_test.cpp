// Standalone CPU contracts. Link with --wrap=pread,--wrap=preadv,--wrap=close;
// tests/io_host builds only the reader and does not call CUDA or model code.
#include "q4t/io/safetensors.h"
#include "q4t/io/weight_loader.h"
#include "q4t/test.h"

#include <fcntl.h>
#include <sys/uio.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::SafetensorsFile;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;

struct ReadScript {
  // Positive entries cap a real read; zero injects EOF; negatives set errno.
  std::vector<ssize_t> results;
  size_t calls = 0;
};

thread_local ReadScript* read_script = nullptr;

class ScriptScope {
 public:
  explicit ScriptScope(ReadScript* script) { read_script = script; }
  ~ScriptScope() { read_script = nullptr; }
};

struct ReadGate {
  std::mutex mu;
  std::condition_variable cv;
  bool entered = false;
  bool release = false;
  std::atomic<int> fd{-1};
  std::atomic<bool> closed{false};
};

thread_local ReadGate* read_gate = nullptr;
std::atomic<ReadGate*> close_gate{nullptr};

void BlockRead(int fd) {
  if (!read_gate) return;
  std::unique_lock<std::mutex> lock(read_gate->mu);
  read_gate->fd = fd;
  read_gate->entered = true;
  read_gate->cv.notify_one();
  read_gate->cv.wait(lock, [] { return read_gate->release; });
}

// Every fixture and build artifact stays under .q4t-work/ or build/.
class Fixture {
 public:
  explicit Fixture(size_t payload_bytes = 64) {
    static int sequence = 0;
    dir = ".q4t-work/io-contract-fixtures-" + std::to_string(getpid()) +
          "-" + std::to_string(sequence++);
    std::filesystem::create_directories(dir);
    std::ofstream index(dir + "/index.json");
    index << R"({"weight_map":{)";
    for (int i = 0; i < 16; ++i) {
      const std::string name = "tensor-" + std::to_string(i);
      const std::string shard = "shard-" + std::to_string(i);
      if (i) index << ',';
      index << '"' << name << "\":\"" << shard << '"';
      const std::string header = "{\"" + name +
          R"(":{"dtype":"U8","shape":[)" +
          std::to_string(payload_bytes) + R"(],"data_offsets":[0,)" +
          std::to_string(payload_bytes) + "]}}";
      std::ofstream file(dir + "/" + shard, std::ios::binary);
      const uint64_t len = header.size();
      file.write(reinterpret_cast<const char*>(&len), sizeof(len));
      file.write(header.data(), header.size());
      for (size_t j = 0; j < payload_bytes; ++j) {
        file.put(static_cast<char>(i + j));
      }
    }
    index << "}}";
  }
  ~Fixture() { std::filesystem::remove_all(dir); }
  std::string dir;
};

std::unique_ptr<SafetensorsFile> OpenFile(const Fixture& fixture) {
  SafetensorsFile* file = nullptr;
  if (!SafetensorsFile::Open(fixture.dir + "/shard-0", &file).ok()) {
    return nullptr;
  }
  return std::unique_ptr<SafetensorsFile>(file);
}

bool OpenLoader(const Fixture& fixture, std::unique_ptr<WeightIndex>* index,
                std::unique_ptr<WeightLoader>* loader) {
  WeightIndex* raw_index = nullptr;
  if (!WeightIndex::Open(fixture.dir + "/index.json", &raw_index).ok()) {
    return false;
  }
  index->reset(raw_index);
  WeightLoader* raw_loader = nullptr;
  if (!WeightLoader::Create(fixture.dir, **index, 1, &raw_loader).ok()) {
    return false;
  }
  loader->reset(raw_loader);
  return true;
}

bool BytesMatch(const std::array<uint8_t, 64>& bytes, int shard) {
  for (size_t i = 0; i < bytes.size(); ++i) {
    if (bytes[i] != static_cast<uint8_t>(shard + i)) return false;
  }
  return true;
}

}  // namespace

extern "C" ssize_t __real_pread(int, void*, size_t, off_t);
extern "C" ssize_t __real_preadv(int, const struct iovec*, int, off_t);
extern "C" int __real_close(int);

extern "C" ssize_t __wrap_pread(int fd, void* dst, size_t len, off_t offset) {
  BlockRead(fd);
  return __real_pread(fd, dst, len, offset);
}

extern "C" ssize_t __wrap_preadv(int fd, const struct iovec* iov, int count,
                                off_t offset) {
  BlockRead(fd);
  if (!read_script) return __real_preadv(fd, iov, count, offset);
  const size_t call = read_script->calls++;
  if (call >= read_script->results.size()) {
    return __real_preadv(fd, iov, count, offset);
  }
  const ssize_t result = read_script->results[call];
  if (result < 0) {
    errno = static_cast<int>(-result);
    return -1;
  }
  if (result == 0) return 0;
  std::vector<struct iovec> limited;
  size_t remaining = static_cast<size_t>(result);
  for (int i = 0; i < count && remaining > 0; ++i) {
    const size_t len = std::min(remaining, iov[i].iov_len);
    limited.push_back({iov[i].iov_base, len});
    remaining -= len;
  }
  return __real_preadv(fd, limited.data(), static_cast<int>(limited.size()),
                      offset);
}

extern "C" int __wrap_close(int fd) {
  ReadGate* gate = close_gate.load();
  if (gate && gate->fd == fd) gate->closed = true;
  return __real_close(fd);
}

Q4T_TEST(io_contract_scatter_short_reads_and_eintr) {
  Fixture fixture;
  auto file = OpenFile(fixture);
  Q4T_CHECK(file);
  std::array<uint8_t, 70> bytes;
  bytes.fill(0xCC);
  const void* dsts[] = {nullptr, bytes.data() + 1, bytes.data() + 19,
                        bytes.data() + 43, nullptr};
  const size_t lens[] = {0, 17, 23, 24, 0};
  // Ends both within iovecs and exactly at a boundary, with interrupts
  // before the first byte and after data has already been consumed.
  ReadScript script{{-EINTR, 3, -EINTR, 14, 1, 5, 30, 2}};
  ScriptScope scope(&script);
  Q4T_CHECK(file->ReadRangev(0, 5, dsts, lens).ok());
  size_t value = 0;
  for (size_t i = 0; i < 5; ++i) {
    const auto* data = static_cast<const uint8_t*>(dsts[i]);
    for (size_t j = 0; j < lens[i]; ++j) Q4T_CHECK(data[j] == value++);
  }
  Q4T_CHECK(script.calls == 9);
  for (size_t i : {0, 18, 42, 67, 68, 69}) Q4T_CHECK(bytes[i] == 0xCC);
  return true;
}

Q4T_TEST(io_contract_scatter_eof_and_error) {
  Fixture fixture;
  auto file = OpenFile(fixture);
  Q4T_CHECK(file);
  for (ssize_t failure : {ssize_t{0}, ssize_t{-EIO}}) {
    std::array<uint8_t, 16> bytes;
    bytes.fill(0xCC);
    const void* dsts[] = {bytes.data(), bytes.data() + 8};
    const size_t lens[] = {8, 8};
    ReadScript script{{3, -EINTR, failure}};
    ScriptScope scope(&script);
    Q4T_CHECK(!file->ReadRangev(0, 2, dsts, lens).ok());
    Q4T_CHECK(script.calls == 3);
    for (size_t i = 0; i < bytes.size(); ++i) {
      Q4T_CHECK(bytes[i] == (i < 3 ? i : 0xCC));
    }
  }
  return true;
}

Q4T_TEST(io_contract_scatter_iovec_limit) {
  const long limit = sysconf(_SC_IOV_MAX);
  Q4T_CHECK(limit > 3);
  const size_t count = static_cast<size_t>(limit) + 3;
  Fixture fixture(count);
  auto file = OpenFile(fixture);
  Q4T_CHECK(file);
  std::vector<uint8_t> bytes(count);
  std::vector<const void*> dsts(count);
  std::vector<size_t> lens(count, 1);
  for (size_t i = 0; i < count; ++i) dsts[i] = &bytes[i];
  ReadScript script{{limit - 3, -EINTR, 7}};
  ScriptScope scope(&script);
  Q4T_CHECK(file->ReadRangev(0, count, dsts.data(), lens.data()).ok());
  Q4T_CHECK(script.calls == 3);
  for (size_t i = 0; i < count; ++i) {
    Q4T_CHECK(bytes[i] == static_cast<uint8_t>(i));
  }
  return true;
}

Q4T_TEST(io_contract_scatter_bounds) {
  Fixture fixture;
  auto file = OpenFile(fixture);
  Q4T_CHECK(file);
  uint8_t byte = 0;
  const void* dsts[] = {&byte, &byte};
  const size_t lens[] = {1, 1};
  const size_t overflow[] = {std::numeric_limits<size_t>::max(), 1};
  Q4T_CHECK(file->ReadRangev(0, 0, nullptr, nullptr).ok());
  Q4T_CHECK(!file->ReadRangev(0, 1, nullptr, lens).ok());
  Q4T_CHECK(!file->ReadRangev(0, 1, dsts, nullptr).ok());
  Q4T_CHECK(!file->ReadRangev(0, 2, dsts, overflow).ok());
  Q4T_CHECK(!file->ReadRangev(UINT64_MAX, 2, dsts, lens).ok());
  Q4T_CHECK(!file->ReadRangev(63, 2, dsts, lens).ok());
  Q4T_CHECK(file->ReadRangev(63, 1, dsts, lens).ok());
  Q4T_CHECK(byte == 63);
  return true;
}

Q4T_TEST(io_contract_metadata_survives_eviction) {
  Fixture fixture;
  std::unique_ptr<WeightIndex> index;
  std::unique_ptr<WeightLoader> loader;
  Q4T_CHECK(OpenLoader(fixture, &index, &loader));
  const auto* first = loader->FindTensor("tensor-0");
  Q4T_CHECK(first);
  for (int i = 1; i < 16; ++i) {
    Q4T_CHECK(loader->FindTensor("tensor-" + std::to_string(i)));
    Q4T_CHECK(loader->open_shards() == 1);
  }
  Q4T_CHECK(first->name == "tensor-0");
  Q4T_CHECK(first->shape == std::vector<int64_t>{64});
  Q4T_CHECK(first->data_start == 0 && first->data_end == 64);
  Q4T_CHECK(loader->FindTensor("tensor-0") == first);
  Q4T_CHECK(!loader->FindTensor("absent"));
  return true;
}

Q4T_TEST(io_contract_active_read_survives_eviction) {
  Fixture fixture;
  for (int kind = 0; kind < 4; ++kind) {
    std::unique_ptr<WeightIndex> index;
    std::unique_ptr<WeightLoader> loader;
    Q4T_CHECK(OpenLoader(fixture, &index, &loader));
    ReadGate gate;
    close_gate = &gate;
    std::array<uint8_t, 64> first{}, second{};
    bool first_ok = false;
    std::thread reader([&] {
      read_gate = &gate;
      Status status;
      if (kind == 0) {
        status = loader->ReadTensor("tensor-0", first.data());
      } else if (kind == 1) {
        status = loader->ReadRange("tensor-0", 0, 64, first.data());
      } else if (kind == 2) {
        status = loader->ReadRangeShard("shard-0", 0, 64, first.data());
      } else {
        const void* dsts[] = {first.data(), first.data() + 19};
        const size_t lens[] = {19, 45};
        status = loader->ReadRangevShard("shard-0", 0, 2, dsts, lens);
      }
      first_ok = status.ok();
      read_gate = nullptr;
    });
    bool entered = false;
    {
      std::unique_lock<std::mutex> lock(gate.mu);
      entered = gate.cv.wait_for(lock, std::chrono::seconds(2),
                                [&] { return gate.entered; });
    }
    // A second read must finish while the first syscall is blocked. This
    // rejects a fix that holds the global loader lock across a read.
    auto evict = std::async(std::launch::async, [&] {
      return loader->ReadTensor("tensor-1", second.data()).ok();
    });
    const bool concurrent =
        evict.wait_for(std::chrono::seconds(2)) == std::future_status::ready;
    const bool closed_during_read = gate.closed;
    {
      std::lock_guard<std::mutex> lock(gate.mu);
      gate.release = true;
    }
    gate.cv.notify_one();
    reader.join();
    const bool second_ok = evict.get();
    close_gate = nullptr;
    Q4T_CHECK(entered && concurrent && !closed_during_read);
    Q4T_CHECK(first_ok && second_ok);
    Q4T_CHECK(BytesMatch(first, 0) && BytesMatch(second, 1));
    Q4T_CHECK(gate.closed);  // Last reader releases the evicted shard.
    Q4T_CHECK(loader->open_shards() == 1);
  }
  return true;
}

Q4T_TEST(io_contract_concurrent_small_cache) {
  Fixture fixture;
  std::unique_ptr<WeightIndex> index;
  std::unique_ptr<WeightLoader> loader;
  Q4T_CHECK(OpenLoader(fixture, &index, &loader));
  std::atomic<bool> ok{true};
  std::vector<std::thread> readers;
  for (int thread = 0; thread < 8; ++thread) {
    readers.emplace_back([&, thread] {
      for (int step = 0; step < 64; ++step) {
        const int shard = (step + thread * 3) % 16;
        const std::string name = "tensor-" + std::to_string(shard);
        const auto* info = loader->FindTensor(name);
        std::array<uint8_t, 64> bytes{};
        if (!info || !loader->ReadTensor(name, bytes.data()).ok() ||
            !BytesMatch(bytes, shard) || info->name != name ||
            info->byte_size() != 64 || loader->open_shards() > 1) {
          ok = false;
        }
      }
    });
  }
  for (auto& reader : readers) reader.join();
  Q4T_CHECK(ok);
  return true;
}
