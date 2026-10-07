// Synthetic WeightLoader contracts. tests/io_host links syscall wrappers and
// a CUDA host double; these checks require no weights or CUDA device.
#include "q4t/io/weight_loader.h"
#include "q4t/test.h"

#include <unistd.h>

#include <array>
#include <atomic>
#include <barrier>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <future>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

using q4t::io::Dtype;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;

constexpr int kShardCount = 32;
constexpr size_t kTensorBytes = 64;
using Bytes = std::array<uint8_t, kTensorBytes>;

class Fixture {
 public:
  Fixture() {
    std::filesystem::create_directories(Q4T_IO_FIXTURE_ROOT);
    dir_ = std::string(Q4T_IO_FIXTURE_ROOT) + "/weight-loader-XXXXXX";
    if (!mkdtemp(dir_.data())) throw std::runtime_error("mkdtemp failed");
    std::ofstream index;
    index.exceptions(std::ios::failbit | std::ios::badbit);
    index.open(dir_ + "/index.json");
    index << R"({"weight_map":{)";
    for (int i = 0; i < kShardCount; ++i) {
      const std::string name = "tensor-" + std::to_string(i);
      const std::string shard = "shard-" + std::to_string(i);
      if (i) index << ',';
      index << '"' << name << "\":\"" << shard << '"';
      const std::string header =
          "{\"" + name +
          R"(":{"dtype":"U8","shape":[64],"data_offsets":[0,64]}})";
      std::ofstream file;
      file.exceptions(std::ios::failbit | std::ios::badbit);
      file.open(dir_ + "/" + shard, std::ios::binary);
      const uint64_t len = header.size();
      file.write(reinterpret_cast<const char*>(&len), sizeof(len));
      file.write(header.data(), header.size());
      for (size_t j = 0; j < kTensorBytes; ++j) {
        file.put(static_cast<char>(i + j));
      }
    }
    index << R"(,"missing-tensor":"shard-0",)"
          << R"("missing-file":"absent-shard"}})";
  }

  ~Fixture() {
    std::error_code ignored;
    std::filesystem::remove_all(dir_, ignored);
  }

  const std::string& dir() const { return dir_; }

 private:
  std::string dir_;
};

bool OpenLoader(const Fixture& fixture, std::unique_ptr<WeightIndex>* index,
                std::unique_ptr<WeightLoader>* loader) {
  WeightIndex* raw_index = nullptr;
  if (!WeightIndex::Open(fixture.dir() + "/index.json", &raw_index).ok()) {
    return false;
  }
  index->reset(raw_index);
  WeightLoader* raw_loader = nullptr;
  if (!WeightLoader::Create(fixture.dir(), **index, 1, &raw_loader).ok()) {
    return false;
  }
  loader->reset(raw_loader);
  return true;
}

bool BytesMatch(const Bytes& bytes, int shard) {
  for (size_t i = 0; i < bytes.size(); ++i) {
    if (bytes[i] != static_cast<uint8_t>(shard + i)) return false;
  }
  return true;
}

enum class PauseAt { kRead, kCopy };

struct ReadGate {
  explicit ReadGate(PauseAt pause_at) : pause_at(pause_at) {}
  PauseAt pause_at;
  std::mutex mu;
  std::condition_variable cv;
  bool entered = false;
  bool release = false;
  std::atomic<int> fd{-1};
  std::atomic<int> closes{0};
};

struct CopyRecord {
  int calls = 0;
  const void* src = nullptr;
  void* dst = nullptr;
  size_t bytes = 0;
  cudaMemcpyKind kind = cudaMemcpyDefault;
  cudaStream_t stream = nullptr;
};

thread_local ReadGate* read_gate = nullptr;
thread_local CopyRecord* copy_record = nullptr;
std::atomic<ReadGate*> close_gate{nullptr};

void BlockAt(PauseAt point) {
  if (!read_gate || read_gate->pause_at != point) return;
  std::unique_lock<std::mutex> lock(read_gate->mu);
  read_gate->entered = true;
  read_gate->cv.notify_one();
  read_gate->cv.wait(lock, [] { return read_gate->release; });
}

bool ReadSurvivesEviction(bool to_device, PauseAt pause_at) {
  Fixture fixture;
  std::unique_ptr<WeightIndex> index;
  std::unique_ptr<WeightLoader> loader;
  Q4T_CHECK(OpenLoader(fixture, &index, &loader));
  ReadGate gate(pause_at);
  CopyRecord copy;
  // The host double verifies stream forwarding without dereferencing it.
  int stream_token = 0;
  const auto stream = reinterpret_cast<cudaStream_t>(&stream_token);
  Bytes first{}, second{}, device{};
  bool first_ok = false;
  close_gate = &gate;
  std::thread reader([&] {
    read_gate = &gate;
    copy_record = &copy;
    if (to_device) {
      first_ok = loader
                     ->ReadTensorToDevice("tensor-0", first.data(),
                                          device.data(), stream)
                     .ok();
    } else {
      first_ok = loader->ReadTensor("tensor-0", first.data()).ok();
    }
    copy_record = nullptr;
    read_gate = nullptr;
  });
  bool entered = false;
  {
    std::unique_lock<std::mutex> lock(gate.mu);
    entered = gate.cv.wait_for(lock, std::chrono::seconds(5),
                               [&] { return gate.entered; });
  }
  // Capacity one guarantees this read evicts tensor-0's shard. Completion
  // before release also rejects holding the loader's global lock during IO.
  auto evict = std::async(std::launch::async, [&] {
    return loader->ReadTensor("tensor-1", second.data()).ok();
  });
  const bool concurrent =
      evict.wait_for(std::chrono::seconds(5)) == std::future_status::ready;
  const int closes_while_blocked = gate.closes;
  {
    std::lock_guard<std::mutex> lock(gate.mu);
    gate.release = true;
  }
  gate.cv.notify_one();
  reader.join();
  const bool second_ok = evict.get();
  close_gate = nullptr;
  // All threads are joined before checks, including on a failing contract.
  Q4T_CHECK(entered && concurrent && closes_while_blocked == 0);
  Q4T_CHECK(first_ok && second_ok);
  Q4T_CHECK(BytesMatch(first, 0) && BytesMatch(second, 1));
  Q4T_CHECK(gate.closes == 1);  // The last reader closes the evicted shard.
  Q4T_CHECK(loader->open_shards() == 1);
  if (to_device) {
    Q4T_CHECK(BytesMatch(device, 0));
    Q4T_CHECK(copy.calls == 1 && copy.src == first.data());
    Q4T_CHECK(copy.dst == device.data() && copy.bytes == kTensorBytes);
    Q4T_CHECK(copy.kind == cudaMemcpyHostToDevice && copy.stream == stream);
  } else {
    Q4T_CHECK(copy.calls == 0);
  }
  return true;
}

}  // namespace

extern "C" ssize_t __real_pread(int, void*, size_t, off_t);
extern "C" int __real_close(int);

extern "C" ssize_t __wrap_pread(int fd, void* dst, size_t len, off_t offset) {
  if (read_gate) read_gate->fd = fd;
  BlockAt(PauseAt::kRead);
  return __real_pread(fd, dst, len, offset);
}

extern "C" int __wrap_close(int fd) {
  ReadGate* gate = close_gate.load();
  if (gate && gate->fd == fd) ++gate->closes;
  return __real_close(fd);
}

extern "C" cudaError_t __wrap_cudaMemcpyAsync(void* dst, const void* src,
                                              size_t bytes, cudaMemcpyKind kind,
                                              cudaStream_t stream) {
  if (!copy_record) return cudaErrorInvalidValue;
  ++copy_record->calls;
  copy_record->src = src;
  copy_record->dst = dst;
  copy_record->bytes = bytes;
  copy_record->kind = kind;
  copy_record->stream = stream;
  BlockAt(PauseAt::kCopy);
  // This checks only host orchestration and argument forwarding, not CUDA
  // transfer semantics, staging lifetime after enqueue, or device completion.
  std::memcpy(dst, src, bytes);
  return cudaSuccess;
}

Q4T_TEST(weight_loader_metadata_survives_eviction) {
  Fixture fixture;
  std::unique_ptr<WeightIndex> index;
  std::unique_ptr<WeightLoader> loader;
  Q4T_CHECK(OpenLoader(fixture, &index, &loader));
  const auto* first = loader->FindTensor("tensor-0");
  Q4T_CHECK(first);
  for (int i = 1; i < kShardCount; ++i) {
    Q4T_CHECK(loader->FindTensor("tensor-" + std::to_string(i)));
    Q4T_CHECK(loader->open_shards() == 1);
  }
  Q4T_CHECK(first->name == "tensor-0" && first->dtype == Dtype::kU8);
  Q4T_CHECK(first->shape == std::vector<int64_t>{64});
  Q4T_CHECK(first->data_start == 0 && first->data_end == kTensorBytes);
  Q4T_CHECK(first->byte_size() == kTensorBytes);
  Q4T_CHECK(loader->FindTensor("tensor-0") == first);
  return true;
}

Q4T_TEST(weight_loader_read_survives_eviction) {
  return ReadSurvivesEviction(false, PauseAt::kRead);
}

Q4T_TEST(weight_loader_device_read_survives_eviction) {
  return ReadSurvivesEviction(true, PauseAt::kRead);
}

Q4T_TEST(weight_loader_device_enqueue_survives_eviction) {
  return ReadSurvivesEviction(true, PauseAt::kCopy);
}

Q4T_TEST(weight_loader_missing_tensor_and_shard) {
  Fixture fixture;
  std::unique_ptr<WeightIndex> index;
  std::unique_ptr<WeightLoader> loader;
  Q4T_CHECK(OpenLoader(fixture, &index, &loader));
  Bytes host{}, device{};
  host.fill(0xCC);
  device.fill(0xCC);
  for (const char* name : {"absent", "missing-tensor", "missing-file"}) {
    Q4T_CHECK(loader->FindTensor(name) == nullptr);
    Q4T_CHECK(!loader->ReadTensor(name, host.data()).ok());
    CopyRecord copy;
    copy_record = &copy;
    const auto status =
        loader->ReadTensorToDevice(name, host.data(), device.data(), nullptr);
    copy_record = nullptr;
    Q4T_CHECK(!status.ok() && copy.calls == 0);
    Q4T_CHECK(loader->open_shards() <= 1);
    for (size_t i = 0; i < kTensorBytes; ++i) {
      Q4T_CHECK(host[i] == 0xCC && device[i] == 0xCC);
    }
  }
  Q4T_CHECK(loader->ReadTensor("tensor-0", host.data()).ok());
  Q4T_CHECK(BytesMatch(host, 0));
  return true;
}

Q4T_TEST(weight_loader_concurrent_small_cache) {
  Fixture fixture;
  std::unique_ptr<WeightIndex> index;
  std::unique_ptr<WeightLoader> loader;
  Q4T_CHECK(OpenLoader(fixture, &index, &loader));
  constexpr int kReaders = 8;
  std::barrier start(kReaders + 1);
  std::atomic<bool> ok{true};
  std::atomic<int> finished{0};
  std::vector<std::thread> readers;
  for (int thread = 0; thread < kReaders; ++thread) {
    readers.emplace_back([&, thread] {
      start.arrive_and_wait();
      for (int step = 0; step < 128; ++step) {
        const int shard = (step + thread * 3) % kShardCount;
        const std::string name = "tensor-" + std::to_string(shard);
        const auto* info = loader->FindTensor(name);
        Bytes bytes{};
        if (!info || !loader->ReadTensor(name, bytes.data()).ok() ||
            !BytesMatch(bytes, shard) || info->name != name ||
            info->byte_size() != kTensorBytes || loader->open_shards() > 1) {
          ok = false;
        }
      }
      ++finished;
    });
  }
  start.arrive_and_wait();
  // Query diagnostics while worker threads repeatedly mutate the LRU. A
  // race detector can directly exercise the open_shards synchronization.
  do {
    if (loader->open_shards() > 1) ok = false;
    std::this_thread::yield();
  } while (finished != kReaders);
  for (auto& reader : readers) reader.join();
  Q4T_CHECK(ok);
  Q4T_CHECK(loader->open_shards() == 1);
  return true;
}
