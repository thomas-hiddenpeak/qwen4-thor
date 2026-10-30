// Synthetic shard and failure contracts against the actual residency code.
// The CPU target uses CUDA doubles; the CUDA target calls the real runtime
// through the same allocation/event wrappers and must run under gpu_exclusive.
#include "q4t/quant/moe_residency.h"
#include "q4t/test.h"

#include <pthread.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>

namespace {

using q4t::quant::MoEResidency;
int creation_calls = 0, fail_creation = 0;
int thread_calls = 0, fail_thread = 0, live_threads = 0;
int host_calls = 0, fail_host = 0;
bool fail_drain = false;
int sync_calls = 0;
std::array<void*, 64> resources{};

size_t LiveResources() {
  return std::count_if(resources.begin(), resources.end(),
                       [](void* p) { return p != nullptr; });
}

void Acquired(void* p) {
  auto it = std::find(resources.begin(), resources.end(), nullptr);
  if (!p || it == resources.end()) std::abort();
  *it = p;
}

size_t ResourceSlot(void* p) {
  auto it = std::find(resources.begin(), resources.end(), p);
  if (!p || it == resources.end()) std::abort();
  return it - resources.begin();
}

class Environment {
 public:
  Environment() {
    for (const auto& [key, value] : settings_) {
      const char* old = std::getenv(key.c_str());
      previous_.emplace_back(old ? old : "");
      present_.push_back(old != nullptr);
      setenv(key.c_str(), value.c_str(), 1);
    }
  }
  ~Environment() {
    fail_creation = fail_thread = fail_host = 0;
    fail_drain = false;
    for (size_t i = 0; i < settings_.size(); ++i) {
      const char* name = settings_[i].first.c_str();
      if (present_[i])
        setenv(name, previous_[i].c_str(), 1);
      else
        unsetenv(name);
    }
  }

 private:
  const std::vector<std::pair<std::string, std::string>> settings_ = {
      {"Q4T_MOE_LOAD_THREADS", "2"},      {"Q4T_MOE_L2_SLOTS", "2"},
      {"Q4T_MOE_MIRROR_K", "2"},          {"Q4T_MOE_INLINE_MISS_LIMIT", "0"},
      {"Q4T_MOE_PREAD_MERGE", "1"},       {"Q4T_MOE_PREAD_MERGE_CAP", "4"},
      {"Q4T_RESIDENCY_FAIL_EXPERT", "-1"}};
  std::vector<std::string> previous_;
  std::vector<bool> present_;
};

enum class Split { kNone, kWeights, kScales, kScalars };

// Both shards have valid, matching tensor offsets. For selected tensors the
// index points to b, while a contains distinct poison bytes at those offsets.
// The region-major layout also exercises adjacent-expert preadv merging.
class Fixture {
 public:
  explicit Fixture(Split split = Split::kNone) {
    std::filesystem::create_directories(Q4T_IO_FIXTURE_ROOT);
    directory_ = std::string(Q4T_IO_FIXTURE_ROOT) + "/residency-XXXXXX";
    if (!mkdtemp(directory_.data())) throw std::runtime_error("mkdtemp");
    std::string header = "{", index = "{\"weight_map\":{";
    std::array<std::string, 2> data;
    size_t offset = 0;
    bool first = true;
    const char* projections[] = {"down_proj", "gate_proj", "up_proj"};
    for (int region = 0; region < 3; ++region) {
      for (int e = 0; e < 2; ++e) {
        for (int p = 0; p < 3; ++p) {
          for (int scalar = 0; scalar < (region == 2 ? 2 : 1); ++scalar) {
            const char* suffix = region == 0 ? "weight" : "weight_scale";
            if (region == 2)
              suffix = scalar == 0 ? "input_scale" : "weight_scale_2";
            const std::string name =
                "model.language_model.layers.0.mlp."
                "experts." +
                std::to_string(e) + "." + projections[p] + "." + suffix;
            const size_t bytes = region == 0 ? 4096 : region == 1 ? 512 : 4;
            const bool other =
                (split == Split::kWeights && region == 0 && p > 0) ||
                (split == Split::kScales && region == 1 && p > 0) ||
                (split == Split::kScalars && region == 2);
            if (!first) {
              header += ',';
              index += ',';
            }
            first = false;
            header += "\"" + name + "\":{\"dtype\":\"" +
                      (region == 2 ? "F32" : "U8") + "\",\"shape\":[" +
                      std::to_string(region == 2 ? 1 : bytes) +
                      "],\"data_offsets\":[" + std::to_string(offset) + "," +
                      std::to_string(offset + bytes) + "]}";
            index += "\"" + name + "\":\"" + (other ? "b" : "a") + "\"";
            std::string value(
                bytes, static_cast<char>(17 * (p + 1) + e + 64 * region));
            if (region == 2) {
              const float number = (p == 0 ? 2.0f : 4.0f) + scalar + e;
              value.assign(reinterpret_cast<const char*>(&number), 4);
            }
            data[0] += other ? std::string(bytes, '\x7f') : value;
            data[1] += value;
            offset += bytes;
          }
        }
      }
    }
    header += '}';
    index += "}}";
    for (int shard = 0; shard < 2; ++shard) {
      std::ofstream file(directory_ + (shard ? "/b" : "/a"), std::ios::binary);
      const uint64_t size = header.size();
      file.write(reinterpret_cast<const char*>(&size), sizeof(size));
      file << header << data[shard];
    }
    std::ofstream(directory_ + "/index.json") << index;
    q4t::io::WeightIndex* raw_index = nullptr;
    if (!q4t::io::WeightIndex::Open(directory_ + "/index.json", &raw_index))
      throw std::runtime_error("index open");
    index_.reset(raw_index);
    q4t::io::WeightLoader* raw_loader = nullptr;
    if (!q4t::io::WeightLoader::Create(directory_, *index_, 1, &raw_loader))
      throw std::runtime_error("loader create");
    loader_.reset(raw_loader);
  }
  ~Fixture() {
    loader_.reset();
    index_.reset();
    std::filesystem::remove_all(directory_);
  }
  const q4t::io::WeightLoader& loader() const { return *loader_; }

 private:
  std::string directory_;
  std::unique_ptr<q4t::io::WeightIndex> index_;
  std::unique_ptr<q4t::io::WeightLoader> loader_;
};

struct Owner {
  MoEResidency residency;
  ~Owner() {
    fail_drain = false;
    residency.Free();
  }
  q4t::Status Init(const Fixture& fixture) {
    return residency.Init(fixture.loader(), 0, 2, 128, 64, 2, nullptr);
  }
};

bool BytesEqual(const void* device, const void* expected, size_t size) {
#ifdef Q4T_RESIDENCY_REAL_CUDA
  std::vector<unsigned char> actual(size);
  if (cudaMemcpy(actual.data(), device, size, cudaMemcpyDeviceToHost) !=
      cudaSuccess)
    return false;
  return std::memcmp(actual.data(), expected, size) == 0;
#else
  return std::memcmp(device, expected, size) == 0;
#endif
}

bool PayloadMatches(const MoEResidency& residency, int e, int slot) {
  const auto& layout = residency.Layout();
  const std::vector<uint8_t> down(4096, 17 + e), gate(4096, 34 + e),
      up(4096, 51 + e), down_sf(512, 81 + e);
  std::vector<uint8_t> gate_up_sf(1024);
  // For 128 rows and K=128, each 16-byte SF lane contains two 4-byte
  // gate row groups followed by two up row groups (no padding).
  for (size_t i = 0; i < gate_up_sf.size(); ++i)
    gate_up_sf[i] = (i % 16 < 8 ? 98 : 115) + e;
  Q4T_CHECK(BytesEqual(layout.dn_packed_expert(slot), down.data(), 4096));
  Q4T_CHECK(BytesEqual(layout.gu_packed_expert(slot), gate.data(), 4096));
  Q4T_CHECK(BytesEqual(layout.gu_packed_expert(slot) + 4096, up.data(), 4096));
  Q4T_CHECK(BytesEqual(layout.dn_sf_expert(slot), down_sf.data(), 512));
  Q4T_CHECK(BytesEqual(layout.gu_sf_expert(slot), gate_up_sf.data(), 1024));
  const float scales[] = {5.0f + e, 4.0f + e, 3.0f + e, 2.0f + e};
  const float* device[] = {layout.gu_w_scale2, layout.gu_input_scale,
                           layout.dn_w_scale2, layout.dn_input_scale};
  for (int i = 0; i < 4; ++i)
    Q4T_CHECK(BytesEqual(device[i] + slot, scales + i, sizeof(float)));
  Q4T_CHECK(layout.gu_w_scale2_h[slot] == scales[0]);
  Q4T_CHECK(layout.gu_input_scale_h[slot] == scales[1]);
  Q4T_CHECK(layout.dn_w_scale2_h[slot] == scales[2]);
  Q4T_CHECK(layout.dn_input_scale_h[slot] == scales[3]);
  return true;
}

}  // namespace

extern "C" {
#ifndef Q4T_RESIDENCY_REAL_CUDA
cudaError_t __real_cudaMalloc(void** p, size_t n) {
  *p = std::malloc(n);
  return *p ? cudaSuccess : cudaErrorMemoryAllocation;
}
cudaError_t __real_cudaHostAlloc(void** p, size_t n, unsigned) {
  return __real_cudaMalloc(p, n);
}
cudaError_t __real_cudaFree(void* p) {
  std::free(p);
  return cudaSuccess;
}
cudaError_t __real_cudaFreeHost(void* p) { return __real_cudaFree(p); }
cudaError_t __real_cudaEventCreateWithFlags(cudaEvent_t* e, unsigned) {
  *e = reinterpret_cast<cudaEvent_t>(std::malloc(1));
  return *e ? cudaSuccess : cudaErrorMemoryAllocation;
}
cudaError_t __real_cudaEventDestroy(cudaEvent_t e) {
  return __real_cudaFree(e);
}
cudaError_t __real_cudaStreamSynchronize(cudaStream_t) { return cudaSuccess; }
cudaError_t cudaEventQuery(cudaEvent_t) { return cudaSuccess; }
cudaError_t cudaEventRecord(cudaEvent_t, cudaStream_t) { return cudaSuccess; }
cudaError_t cudaEventSynchronize(cudaEvent_t) { return cudaSuccess; }
cudaError_t cudaMemcpyAsync(void* d, const void* s, size_t n, cudaMemcpyKind,
                            cudaStream_t) {
  std::memcpy(d, s, n);
  return cudaSuccess;
}
const char* cudaGetErrorString(cudaError_t) { return "CPU injected failure"; }
#else
cudaError_t __real_cudaMalloc(void**, size_t);
cudaError_t __real_cudaHostAlloc(void**, size_t, unsigned);
cudaError_t __real_cudaFree(void*);
cudaError_t __real_cudaFreeHost(void*);
cudaError_t __real_cudaEventCreateWithFlags(cudaEvent_t*, unsigned);
cudaError_t __real_cudaEventDestroy(cudaEvent_t);
cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
#endif

cudaError_t __wrap_cudaMalloc(void** p, size_t n) {
  if (++creation_calls == fail_creation) return cudaErrorMemoryAllocation;
  const auto result = __real_cudaMalloc(p, n);
  if (result == cudaSuccess) Acquired(*p);
  return result;
}
cudaError_t __wrap_cudaHostAlloc(void** p, size_t n, unsigned flags) {
  if (++creation_calls == fail_creation) return cudaErrorMemoryAllocation;
  const auto result = __real_cudaHostAlloc(p, n, flags);
  if (result == cudaSuccess) Acquired(*p);
  return result;
}
cudaError_t __wrap_cudaEventCreateWithFlags(cudaEvent_t* p, unsigned flags) {
  if (++creation_calls == fail_creation) return cudaErrorMemoryAllocation;
  const auto result = __real_cudaEventCreateWithFlags(p, flags);
  if (result == cudaSuccess) Acquired(*p);
  return result;
}
cudaError_t __wrap_cudaFree(void* p) {
  const auto slot = ResourceSlot(p);
  const auto result = __real_cudaFree(p);
  if (result == cudaSuccess) resources[slot] = nullptr;
  return result;
}
cudaError_t __wrap_cudaFreeHost(void* p) {
  const auto slot = ResourceSlot(p);
  const auto result = __real_cudaFreeHost(p);
  if (result == cudaSuccess) resources[slot] = nullptr;
  return result;
}
cudaError_t __wrap_cudaEventDestroy(cudaEvent_t p) {
  const auto slot = ResourceSlot(p);
  const auto result = __real_cudaEventDestroy(p);
  if (result == cudaSuccess) resources[slot] = nullptr;
  return result;
}
cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t s) {
  ++sync_calls;
  return fail_drain ? cudaErrorUnknown : __real_cudaStreamSynchronize(s);
}

int __real_pthread_create(pthread_t*, const pthread_attr_t*, void* (*)(void*),
                          void*);
int __wrap_pthread_create(pthread_t* t, const pthread_attr_t* a,
                          void* (*f)(void*), void* v) {
  if (++thread_calls == fail_thread) return EAGAIN;
  const int result = __real_pthread_create(t, a, f, v);
  if (!result) ++live_threads;
  return result;
}
int __real_pthread_join(pthread_t, void**);
int __wrap_pthread_join(pthread_t t, void** p) {
  const int result = __real_pthread_join(t, p);
  if (!result) --live_threads;
  return result;
}
void* __real__ZnwmRKSt9nothrow_t(size_t, const std::nothrow_t&) noexcept;
void* __wrap__ZnwmRKSt9nothrow_t(size_t n, const std::nothrow_t& tag) noexcept {
  if (++host_calls == fail_host) return nullptr;
  return __real__ZnwmRKSt9nothrow_t(n, tag);
}
}

Q4T_TEST(residency_synthetic_same_and_split_shards_match_all_bytes) {
  Environment environment;
  for (auto split :
       {Split::kNone, Split::kWeights, Split::kScales, Split::kScalars}) {
    Fixture fixture(split);
    Owner owner;
    Q4T_CHECK(owner.Init(fixture).ok());
    int32_t needed[] = {0, 1}, slots[2];
    Q4T_CHECK(owner.residency.Resolve(needed, 2, slots, nullptr).ok());
    Q4T_CHECK(cudaStreamSynchronize(nullptr) == cudaSuccess);
    Q4T_CHECK(PayloadMatches(owner.residency, 0, slots[0]));
    Q4T_CHECK(PayloadMatches(owner.residency, 1, slots[1]));
    const auto& stats = owner.residency.GetStats();
    Q4T_CHECK(stats.pread_merge_runs == (split == Split::kNone ? 1 : 0));
    owner.residency.Free();
    Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  }
  return true;
}

Q4T_TEST(residency_every_cuda_init_failure_rolls_back_and_retries) {
  Environment environment;
  Fixture fixture;
  Owner owner;
  creation_calls = 0;
  Q4T_CHECK(owner.Init(fixture).ok());
  const int count = creation_calls;
  Q4T_CHECK(count == 11);  // 5 device, 2 pinned, 4 events.
  owner.residency.Free();
  for (int n = 1; n <= count; ++n) {
    creation_calls = sync_calls = 0;
    fail_creation = n;
    Q4T_CHECK(!owner.Init(fixture).ok());
    Q4T_CHECK(creation_calls == n);
    Q4T_CHECK(LiveResources() == 0 && live_threads == 0 && sync_calls == 0);
    owner.residency.Free();
    owner.residency.Free();
    fail_creation = 0;
    Q4T_CHECK(owner.Init(fixture).ok());
    Q4T_CHECK(owner.residency.InitHot({0}, nullptr).ok());
    owner.residency.Free();
    Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  }
  return true;
}

Q4T_TEST(residency_mutex_and_worker_allocation_failures_roll_back) {
  Environment environment;
  Fixture fixture;
  Owner owner;
  for (int n = 1; n <= 3; ++n) {
    host_calls = sync_calls = 0;
    fail_host = n;
    Q4T_CHECK(!owner.Init(fixture).ok());
    Q4T_CHECK(host_calls == n);
    Q4T_CHECK(LiveResources() == 0 && live_threads == 0 && sync_calls == 0);
    fail_host = 0;
    Q4T_CHECK(owner.Init(fixture).ok());
    owner.residency.Free();
    Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  }
  return true;
}

Q4T_TEST(residency_later_thread_creation_exception_joins_prior_worker) {
  Environment environment;
  Fixture fixture;
  Owner owner;
  thread_calls = sync_calls = 0;
  fail_thread = 2;
  bool caught = false;
  try {
    owner.Init(fixture);
  } catch (const std::system_error&) {
    caught = true;
  }
  Q4T_CHECK(caught && thread_calls == 2);
  Q4T_CHECK(LiveResources() == 0 && live_threads == 0 && sync_calls == 0);
  fail_thread = 0;
  Q4T_CHECK(owner.Init(fixture).ok());
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  return true;
}

Q4T_TEST(residency_drain_failure_retains_payloads_until_completion) {
  Environment environment;
  Fixture fixture;
  Owner owner;
  Q4T_CHECK(owner.Init(fixture).ok());
  Q4T_CHECK(owner.residency.InitHot({0}, nullptr).ok());
  const auto live = LiveResources();
  fail_drain = true;
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == live && live_threads == 0);
  Q4T_CHECK(!owner.Init(fixture).ok());
  fail_drain = false;
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == 0);
  Q4T_CHECK(owner.Init(fixture).ok());
  Q4T_CHECK(owner.residency.InitHot({1}, nullptr).ok());
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  return true;
}
