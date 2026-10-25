// Fixed production BF16 GEMM parameter probe. No model/runtime changes.
#include <cuda_profiler_api.h>
#include <cuda_runtime.h>
#include <cupti.h>
#include <nvtx3/nvToolsExt.h>
#include <dlfcn.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <utility>

#include "q4t/model/linear.h"

namespace {
constexpr int kM = 4, kN = 48, kK = 2560;
constexpr size_t kWorkspace = 32U << 20;
constexpr size_t kRecordLimit = 16384;
constexpr size_t kBufferBytes = 1U << 20;
constexpr size_t kBufferCount = 8;
constexpr size_t kDeviceBufferBytes = 1U << 20;
constexpr size_t kDeviceBufferCount = 4;
constexpr size_t kCollectorLimit = 64U << 20;
constexpr const char* kPhases[] = {"control_zero", "control_nonzero",
                                   "gemm_first", "gemm_cached", "setup"};
constexpr const char* kRanges[] = {
    "mechanism|phase=control_zero", "mechanism|phase=control_nonzero",
    "mechanism|phase=gemm_first", "mechanism|phase=gemm_cached"};
struct Record {
  uint64_t seq = 0, enter_seq = 0, start = 0, end = 0, bytes = 0;
  uint64_t dev_ptr = 0, stream = 0;
  uint32_t correlation = 0, parent_runtime = 0, runtime_correlation = 0;
  uint32_t pid = 0, tid = 0, context = 0, stream_id = 0, device = 0;
  uint32_t cbid = 0;
  int type = 0;  // 1 callback, 2 activity.
  int domain = 0, site = 0, phase = 4, kind = 0, value = 0, retval = -1;
  bool is_memset = false, has_runtime_correlation = false;
  char name[1024]{};
};
struct ErrorRecord {
  char operation[128]{};
  int code = 0;
};
alignas(8) std::array<Record, kRecordLimit> records;
std::array<std::atomic<bool>, kRecordLimit> record_ready{};
alignas(8) uint8_t buffers[kBufferCount][kBufferBytes];
std::array<std::atomic<bool>, kBufferCount> buffer_used{};
std::array<ErrorRecord, 64> error_records;
std::atomic<uint64_t> next_record{0}, errors{0}, overflow{0}, drops{0};
std::atomic<uint64_t> api_enters{0}, api_exits{0}, activities{0};
std::atomic<uint64_t> buffer_requests{0}, buffer_completions{0};
std::atomic<int> phase{4}, callbacks_active{0};
thread_local std::array<uint32_t, 64> runtime_stack{};
thread_local size_t runtime_depth = 0;
CUpti_SubscriberHandle subscriber = nullptr;
bool subscribed = false, activity_enabled = false, flushed = false;
bool collector_finalized = false;
constexpr CUpti_ActivityKind kKinds[] = {
    CUPTI_ACTIVITY_KIND_RUNTIME, CUPTI_ACTIVITY_KIND_DRIVER,
    CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL, CUPTI_ACTIVITY_KIND_MEMCPY,
    CUPTI_ACTIVITY_KIND_MEMSET};
static_assert(sizeof(records) + sizeof(record_ready) + sizeof(buffers) +
                  kDeviceBufferBytes * (kDeviceBufferCount + 1) <
              kCollectorLimit);

void Error(const char* operation, int code) noexcept {
  const auto index = errors.fetch_add(1);
  if (index < error_records.size()) {
    std::snprintf(error_records[index].operation,
                  sizeof(error_records[index].operation), "%s", operation);
    error_records[index].code = code;
  }
}
void Check(cudaError_t code, const char* operation) {
  if (code != cudaSuccess) {
    Error(operation, static_cast<int>(code));
    throw std::runtime_error(operation);
  }
}
void CuptiCheck(CUptiResult code, const char* operation) {
  if (code != CUPTI_SUCCESS) {
    Error(operation, static_cast<int>(code));
    throw std::runtime_error(operation);
  }
}
uint32_t Tid() { return static_cast<uint32_t>(syscall(SYS_gettid)); }
void Name(char* output, size_t capacity, const char* name) noexcept {
  if (name == nullptr) name = "";
  const size_t count = strnlen(name, capacity);
  if (count == capacity) {
    ++overflow;
    Error("record_name_too_long", 1);
  }
  const size_t copy = std::min(count, capacity - 1);
  std::memcpy(output, name, copy);
  output[copy] = '\0';
}
Record* Reserve() noexcept {
  const auto index = next_record.fetch_add(1);
  if (index >= kRecordLimit) {
    ++overflow;
    return nullptr;
  }
  records[index].seq = index + 1;
  return &records[index];
}
void Publish(Record* record) noexcept {
  record_ready[record->seq - 1].store(true, std::memory_order_release);
}
struct Active {
  Active() { ++callbacks_active; }
  ~Active() { --callbacks_active; }
};

void CUPTIAPI Callback(void*, CUpti_CallbackDomain domain, CUpti_CallbackId id,
                       const void* opaque) {
  Active active;
  if (domain != CUPTI_CB_DOMAIN_RUNTIME_API &&
      domain != CUPTI_CB_DOMAIN_DRIVER_API)
    return;
  const auto* data = static_cast<const CUpti_CallbackData*>(opaque);
  const bool runtime = domain == CUPTI_CB_DOMAIN_RUNTIME_API;
  const bool enter = data->callbackSite == CUPTI_API_ENTER;
  Record* r = Reserve();
  if (enter) {
    ++api_enters;
    if (data->correlationData != nullptr)
      *data->correlationData = r == nullptr ? 0 : r->seq;
    if (r != nullptr) {
      r->type = 1;
      r->domain = runtime ? 1 : 2;
      r->site = 1;
      r->phase = phase.load();
      r->pid = static_cast<uint32_t>(getpid());
      r->tid = Tid();
      r->cbid = id;
      r->correlation = data->correlationId;
      r->context = data->contextUid;
      r->enter_seq = r->seq;
      r->parent_runtime = runtime_depth ? runtime_stack[runtime_depth - 1] : 0;
      Name(r->name, sizeof(r->name), data->functionName);
      if (runtime &&
          (id == CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_v3020 ||
           id == CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_ptsz_v7000)) {
        r->is_memset = true;
        if (data->functionParams == nullptr) {
          Error("memset_callback_missing_parameters", 1);
        } else if (id == CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_v3020) {
          const auto* p = static_cast<const cudaMemsetAsync_v3020_params*>(
              data->functionParams);
          r->dev_ptr = reinterpret_cast<uintptr_t>(p->devPtr);
          r->value = p->value;
          r->bytes = p->count;
          r->stream = reinterpret_cast<uintptr_t>(p->stream);
        } else {
          const auto* p = static_cast<const cudaMemsetAsync_ptsz_v7000_params*>(
              data->functionParams);
          r->dev_ptr = reinterpret_cast<uintptr_t>(p->devPtr);
          r->value = p->value;
          r->bytes = p->count;
          r->stream = reinterpret_cast<uintptr_t>(p->stream);
        }
      }
      Publish(r);
    }
    if (runtime) {
      if (runtime_depth < runtime_stack.size())
        runtime_stack[runtime_depth++] = data->correlationId;
      else {
        ++overflow;
        Error("runtime_callback_stack_overflow", 1);
      }
    }
  } else {
    ++api_exits;
    const uint64_t entry =
        data->correlationData == nullptr ? 0 : *data->correlationData;
    if (r != nullptr) {
      const uint64_t seq = r->seq;
      if (entry == 0 || entry > kRecordLimit ||
          !record_ready[entry - 1].load(std::memory_order_acquire)) {
        Error("callback_exit_missing_enter", 1);
        r->type = 1;
        r->domain = runtime ? 1 : 2;
        r->pid = static_cast<uint32_t>(getpid());
        r->tid = Tid();
        Name(r->name, sizeof(r->name), data->functionName);
      } else {
        *r = records[entry - 1];
        r->seq = seq;
        if (r->correlation != data->correlationId || r->tid != Tid())
          Error("callback_exit_identity_changed", 1);
      }
      r->site = 2;
      if (data->functionReturnValue == nullptr)
        Error("callback_missing_return", 1);
      else
        r->retval = *static_cast<const int*>(data->functionReturnValue);
      Publish(r);
    }
    if (runtime) {
      if (!runtime_depth ||
          runtime_stack[runtime_depth - 1] != data->correlationId)
        Error("runtime_callback_stack_mismatch", 1);
      else
        --runtime_depth;
    }
  }
}

void CUPTIAPI BufferRequested(uint8_t** buffer, size_t* size,
                              size_t* max_records) {
  Active active;
  ++buffer_requests;
  *buffer = nullptr;
  *size = 0;
  *max_records = 0;
  for (size_t i = 0; i < kBufferCount; ++i) {
    bool expected = false;
    if (buffer_used[i].compare_exchange_strong(expected, true)) {
      *buffer = buffers[i];
      *size = kBufferBytes;
      return;
    }
  }
  ++overflow;
  Error("activity_buffer_pool_exhausted", 1);
}

void CUPTIAPI BufferComplete(CUcontext context, uint32_t stream_id,
                             uint8_t* buffer, size_t, size_t valid) {
  Active active;
  ++buffer_completions;
  CUpti_Activity* raw = nullptr;
  CUptiResult result;
  while ((result = cuptiActivityGetNextRecord(buffer, valid, &raw)) ==
         CUPTI_SUCCESS) {
    ++activities;
    Record* r = Reserve();
    if (r == nullptr) continue;
    r->type = 2;
    r->kind = static_cast<int>(raw->kind);
    r->pid = static_cast<uint32_t>(getpid());
    if (raw->kind == CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL ||
        raw->kind == CUPTI_ACTIVITY_KIND_KERNEL) {
      const auto* p = reinterpret_cast<const CUpti_ActivityKernel12*>(raw);
      r->start = p->start;
      r->end = p->end;
      r->correlation = p->correlationId;
      r->context = p->contextId;
      r->stream_id = p->streamId;
      r->device = p->deviceId;
      Name(r->name, sizeof(r->name), p->name);
    } else if (raw->kind == CUPTI_ACTIVITY_KIND_MEMSET) {
      const auto* p = reinterpret_cast<const CUpti_ActivityMemset4*>(raw);
      r->start = p->start;
      r->end = p->end;
      r->correlation = p->correlationId;
      r->context = p->contextId;
      r->stream_id = p->streamId;
      r->device = p->deviceId;
      r->bytes = p->bytes;
      r->value = static_cast<int>(p->value);
    } else if (raw->kind == CUPTI_ACTIVITY_KIND_MEMCPY) {
      const auto* p = reinterpret_cast<const CUpti_ActivityMemcpy6*>(raw);
      r->start = p->start;
      r->end = p->end;
      r->correlation = p->correlationId;
      r->context = p->contextId;
      r->stream_id = p->streamId;
      r->device = p->deviceId;
      r->bytes = p->bytes;
      r->runtime_correlation = p->runtimeCorrelationId;
      r->has_runtime_correlation = true;
    } else if (raw->kind == CUPTI_ACTIVITY_KIND_RUNTIME ||
               raw->kind == CUPTI_ACTIVITY_KIND_DRIVER) {
      const auto* p = reinterpret_cast<const CUpti_ActivityAPI*>(raw);
      r->start = p->start;
      r->end = p->end;
      r->correlation = p->correlationId;
      r->pid = p->processId;
      r->tid = p->threadId;
      r->cbid = p->cbid;
      r->retval = static_cast<int>(p->returnValue);
      const char* name = nullptr;
      const auto d = raw->kind == CUPTI_ACTIVITY_KIND_RUNTIME
                         ? CUPTI_CB_DOMAIN_RUNTIME_API
                         : CUPTI_CB_DOMAIN_DRIVER_API;
      const CUptiResult status = cuptiGetCallbackName(d, p->cbid, &name);
      if (status != CUPTI_SUCCESS)
        Error("activity_callback_name", static_cast<int>(status));
      Name(r->name, sizeof(r->name), name);
    } else
      Error("unexpected_activity_kind", static_cast<int>(raw->kind));
    Publish(r);
  }
  if (result != CUPTI_ERROR_MAX_LIMIT_REACHED)
    Error("activity_record_decode", static_cast<int>(result));
  size_t lost = 0;
  const auto lost_status =
      cuptiActivityGetNumDroppedRecords(context, stream_id, &lost);
  if (lost_status != CUPTI_SUCCESS)
    Error("activity_drop_query", static_cast<int>(lost_status));
  drops.fetch_add(lost);
  bool found = false;
  for (size_t i = 0; i < kBufferCount; ++i)
    if (buffer == buffers[i]) {
      found = true;
      buffer_used[i].store(false);
      break;
    }
  if (!found) Error("foreign_activity_buffer", 1);
}

void BeginCollector() {
  CuptiCheck(cuptiSubscribe(&subscriber, Callback, nullptr),
             "cuptiSubscribe_v1");
  subscribed = true;
  for (const auto& setting :
       std::array<std::pair<CUpti_ActivityAttribute, size_t>, 4>{
           {{CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_SIZE, kDeviceBufferBytes},
            {CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_POOL_LIMIT, kDeviceBufferCount},
            {CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_PRE_ALLOCATE_VALUE, 1},
            {CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_SIZE_CDP,
             kDeviceBufferBytes}}}) {
    size_t value = setting.second, size = sizeof(value);
    CuptiCheck(cuptiActivitySetAttribute(setting.first, &size, &value),
               "activity_buffer_attribute");
  }
  CuptiCheck(cuptiActivityRegisterCallbacks(BufferRequested, BufferComplete),
             "activity_register");
  CuptiCheck(cuptiEnableDomain(1, subscriber, CUPTI_CB_DOMAIN_RUNTIME_API),
             "enable_runtime_callbacks");
  CuptiCheck(cuptiEnableDomain(1, subscriber, CUPTI_CB_DOMAIN_DRIVER_API),
             "enable_driver_callbacks");
  for (auto kind : kKinds)
    CuptiCheck(cuptiActivityEnable(kind), "activity_enable");
  activity_enabled = true;
}
void EndCollector() {
  if (activity_enabled) {
    CuptiCheck(cuptiActivityFlushAll(0), "activity_flush_complete");
    for (auto kind : kKinds)
      CuptiCheck(cuptiActivityDisable(kind), "activity_disable");
    CuptiCheck(cuptiActivityFlushAll(0), "activity_final_flush");
    flushed = true;
    activity_enabled = false;
  }
  if (subscribed) {
    CuptiCheck(cuptiEnableDomain(0, subscriber, CUPTI_CB_DOMAIN_RUNTIME_API),
               "disable_runtime_callbacks");
    CuptiCheck(cuptiEnableDomain(0, subscriber, CUPTI_CB_DOMAIN_DRIVER_API),
               "disable_driver_callbacks");
    CuptiCheck(cuptiUnsubscribe(subscriber), "cupti_unsubscribe");
    subscribed = false;
    CuptiCheck(cuptiFinalize(), "cupti_finalize");
    collector_finalized = true;
  }
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(5);
  while (callbacks_active.load() != 0 &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  if (callbacks_active.load() != 0)
    throw std::runtime_error("collector callbacks did not quiesce");
}

std::string Json(const char* input) {
  std::string output = "\"";
  for (const unsigned char c : std::string(input)) {
    if (c == '"' || c == '\\') {
      output += '\\';
      output += static_cast<char>(c);
    } else if (c < 32) {
      char escape[7];
      std::snprintf(escape, sizeof(escape), "\\u%04x", c);
      output += escape;
    } else
      output += static_cast<char>(c);
  }
  return output + '"';
}
const char* Kind(int kind) {
  switch (kind) {
    case CUPTI_ACTIVITY_KIND_RUNTIME:
      return "runtime";
    case CUPTI_ACTIVITY_KIND_DRIVER:
      return "driver";
    case CUPTI_ACTIVITY_KIND_MEMSET:
      return "memset";
    case CUPTI_ACTIVITY_KIND_MEMCPY:
      return "copy";
    case CUPTI_ACTIVITY_KIND_KERNEL:
    case CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL:
      return "kernel";
    default:
      return "unknown";
  }
}
void DumpRecords(bool cupti) {
  const uint64_t count = std::min<uint64_t>(next_record.load(), kRecordLimit);
  for (uint64_t i = 0; i < count; ++i) {
    if (!record_ready[i].load(std::memory_order_acquire)) {
      Error("unpublished_record", 1);
      continue;
    }
    const auto& r = records[i];
    if (r.type == 1) {
      std::printf(
          "[mechanism-event] "
          "{\"event\":\"api\",\"seq\":%llu,\"domain\":\"%s\",\"site\":\"%s\","
          "\"name\":%s,\"pid\":%u,\"tid\":%u,\"correlation_id\":%u,\"enter_"
          "seq\":%llu,\"phase\":\"%s\",\"parent_runtime_correlation_id\":%u,"
          "\"context_uid\":%u,\"cbid\":%u,\"retval\":%d,\"is_memset\":%s,"
          "\"count\":%llu,\"dev_ptr\":%llu,\"value\":%d,\"stream\":%llu}\n",
          static_cast<unsigned long long>(r.seq),
          r.domain == 1 ? "runtime" : "driver", r.site == 1 ? "enter" : "exit",
          Json(r.name).c_str(), r.pid, r.tid, r.correlation,
          static_cast<unsigned long long>(r.enter_seq), kPhases[r.phase],
          r.parent_runtime, r.context, r.cbid, r.retval,
          r.is_memset ? "true" : "false",
          static_cast<unsigned long long>(r.bytes),
          static_cast<unsigned long long>(r.dev_ptr), r.value,
          static_cast<unsigned long long>(r.stream));
    } else {
      std::printf(
          "[mechanism-event] "
          "{\"event\":\"activity\",\"seq\":%llu,\"kind\":\"%s\",\"name\":%s,"
          "\"correlation_id\":%u,\"runtime_correlation_id\":",
          static_cast<unsigned long long>(r.seq), Kind(r.kind),
          Json(r.name).c_str(), r.correlation);
      if (r.has_runtime_correlation)
        std::printf("%u", r.runtime_correlation);
      else
        std::printf("null");
      std::printf(
          ",\"start\":%llu,\"end\":%llu,\"process_id\":%u,\"process_id_"
          "source\":\"%s\",\"thread_id\":%u,\"context_id\":%u,\"device_id\":%u,"
          "\"stream_id\":%u,\"bytes\":%llu,\"value\":%d,\"cbid\":%u,\"return_"
          "value\":%d}\n",
          static_cast<unsigned long long>(r.start),
          static_cast<unsigned long long>(r.end), r.pid,
          (r.kind == CUPTI_ACTIVITY_KIND_RUNTIME ||
           r.kind == CUPTI_ACTIVITY_KIND_DRIVER)
              ? "activity_record"
              : "collector_process",
          r.tid, r.context, r.device, r.stream_id,
          static_cast<unsigned long long>(r.bytes), r.value, r.cbid, r.retval);
    }
  }
  for (size_t i = 0;
       i < std::min<uint64_t>(errors.load(), error_records.size()); ++i)
    std::printf("[mechanism-error] {\"operation\":%s,\"code\":%d}\n",
                Json(error_records[i].operation).c_str(),
                error_records[i].code);
  size_t outstanding = 0;
  for (const auto& used : buffer_used) outstanding += used.load() ? 1 : 0;
  std::printf(
      "[mechanism-event] "
      "{\"event\":\"summary\",\"mode\":\"%s\",\"api_enter_count\":%llu,\"api_"
      "exit_count\":%llu,\"activity_count\":%llu,\"drops\":%llu,\"overflow\":%"
      "llu,\"errors\":%llu,\"flushed\":%s,\"subscriber_used\":%s,\"record_"
      "count\":%llu,\"buffer_requests\":%llu,\"buffer_completions\":%llu,"
      "\"buffers_outstanding\":%zu,\"collector_limit_bytes\":%zu,\"collector_"
      "owned_bytes\":%zu,\"configured_cupti_buffer_bytes\":%zu,\"opaque_"
      "library_allocations_bounded\":false}\n",
      cupti ? "cupti" : "nsys",
      static_cast<unsigned long long>(api_enters.load()),
      static_cast<unsigned long long>(api_exits.load()),
      static_cast<unsigned long long>(activities.load()),
      static_cast<unsigned long long>(drops.load()),
      static_cast<unsigned long long>(overflow.load()),
      static_cast<unsigned long long>(errors.load()),
      flushed ? "true" : "false", cupti ? "true" : "false",
      static_cast<unsigned long long>(count),
      static_cast<unsigned long long>(buffer_requests.load()),
      static_cast<unsigned long long>(buffer_completions.load()), outstanding,
      kCollectorLimit, sizeof(records) + sizeof(record_ready) + sizeof(buffers),
      kDeviceBufferBytes * (kDeviceBufferCount + 1));
}

uint16_t Bf16(int value) {
  return static_cast<uint16_t>(
      std::bit_cast<uint32_t>(static_cast<float>(value)) >> 16);
}
void Library(const char* name, const void* symbol) {
  Dl_info info{};
  if (dladdr(symbol, &info) == 0 || info.dli_fname == nullptr)
    throw std::runtime_error("library identity unavailable");
  const auto path = std::filesystem::canonical(info.dli_fname);
  struct stat status {};
  if (::stat(path.c_str(), &status) != 0)
    throw std::runtime_error("library stat failed");
  std::printf(
      "[mechanism-event] "
      "{\"event\":\"library\",\"name\":%s,\"path\":%s,\"device\":%llu,"
      "\"inode\":%llu,\"bytes\":%llu,\"mtime_ns\":%llu}\n",
      Json(name).c_str(), Json(path.c_str()).c_str(),
      static_cast<unsigned long long>(status.st_dev),
      static_cast<unsigned long long>(status.st_ino),
      static_cast<unsigned long long>(status.st_size),
      static_cast<unsigned long long>(status.st_mtim.tv_sec) * 1000000000ULL +
          static_cast<unsigned long long>(status.st_mtim.tv_nsec));
}
struct Resources {
  uint16_t* x = nullptr;
  uint16_t* w = nullptr;
  uint16_t* y = nullptr;
  uint8_t* control = nullptr;
  void* workspace = nullptr;
  cudaStream_t stream = nullptr;
  bool Release() {
    bool ok = true;
    for (void* p :
         {static_cast<void*>(x), static_cast<void*>(w), static_cast<void*>(y),
          static_cast<void*>(control), workspace}) {
      if (p != nullptr) {
        const auto status = cudaFree(p);
        if (status != cudaSuccess) {
          Error("cudaFree", static_cast<int>(status));
          ok = false;
        }
      }
    }
    x = nullptr;
    w = nullptr;
    y = nullptr;
    control = nullptr;
    workspace = nullptr;
    if (stream != nullptr) {
      const auto status = cudaStreamDestroy(stream);
      if (status != cudaSuccess) {
        Error("cudaStreamDestroy", static_cast<int>(status));
        ok = false;
      }
      stream = nullptr;
    }
    return ok;
  }
};
void PhaseBegin(int ordinal) {
  phase.store(ordinal);
  std::printf(
      "[mechanism-event] "
      "{\"event\":\"phase_begin\",\"phase\":\"%s\",\"ordinal\":%d}\n",
      kPhases[ordinal], ordinal);
  nvtxRangePushA(kRanges[ordinal]);
}
void PhaseEnd(int ordinal) {
  nvtxRangePop();
  std::printf(
      "[mechanism-event] "
      "{\"event\":\"phase_end\",\"phase\":\"%s\",\"ordinal\":%d}\n",
      kPhases[ordinal], ordinal);
  phase.store(4);
}
template <class T>
void CheckWords(const char* name, const std::vector<T>& actual,
                const std::vector<T>& expected) {
  size_t mismatches = 0;
  for (size_t i = 0; i < expected.size(); ++i)
    mismatches += actual[i] != expected[i] ? 1 : 0;
  std::printf(
      "[mechanism-event] "
      "{\"event\":\"check\",\"phase\":\"%s\",\"passed\":%s,\"mismatches\":%zu,"
      "\"expected_words\":[",
      name, mismatches == 0 ? "true" : "false", mismatches);
  for (size_t i = 0; i < expected.size(); ++i)
    std::printf("%s%u", i ? "," : "", static_cast<unsigned>(expected[i]));
  std::printf("],\"actual_words\":[");
  for (size_t i = 0; i < actual.size(); ++i)
    std::printf("%s%u", i ? "," : "", static_cast<unsigned>(actual[i]));
  std::printf("]}\n");
  if (mismatches != 0) throw std::runtime_error("exact output mismatch");
}
size_t CacheSize() {
  std::lock_guard<std::mutex> lock(q4t::quant::LtCacheMutex());
  return q4t::quant::Bf16PlanCache().size();
}
void Algo(const char* name, size_t before) {
  std::lock_guard<std::mutex> lock(q4t::quant::LtCacheMutex());
  auto& cache = q4t::quant::Bf16PlanCache();
  const q4t::quant::LtPlanKey key{kM, kN, kK, kWorkspace};
  const auto found = cache.find(key);
  if (found == cache.end() || !found->second.has_algo || cache.size() != 1)
    throw std::runtime_error(
        "production BF16 plan missing or unexpected cache entry");
  const auto& algo = found->second.algo;
  std::printf(
      "[mechanism-event] "
      "{\"event\":\"algo\",\"phase\":\"%s\",\"cache_before\":%zu,\"cache_"
      "after\":%zu,\"key\":{\"M\":4,\"N\":48,\"K\":2560,\"workspace_bytes\":"
      "33554432,\"out_bits\":0},\"raw_hex\":\"",
      name, before, cache.size());
  const auto* bytes = reinterpret_cast<const unsigned char*>(&algo);
  for (size_t i = 0; i < sizeof(algo); ++i) std::printf("%02x", bytes[i]);
  std::printf("\",\"attrs\":[");
  constexpr cublasLtMatmulAlgoConfigAttributes_t ids[] = {
      CUBLASLT_ALGO_CONFIG_ID,
      CUBLASLT_ALGO_CONFIG_TILE_ID,
      CUBLASLT_ALGO_CONFIG_SPLITK_NUM,
      CUBLASLT_ALGO_CONFIG_REDUCTION_SCHEME,
      CUBLASLT_ALGO_CONFIG_CTA_SWIZZLING,
      CUBLASLT_ALGO_CONFIG_CUSTOM_OPTION,
      CUBLASLT_ALGO_CONFIG_STAGES_ID};
  constexpr const char* names[] = {"id",      "tile",   "split_k", "reduction",
                                   "swizzle", "custom", "stages"};
  for (size_t i = 0; i < std::size(ids); ++i) {
    uint32_t value = 0;
    size_t written = 0;
    const auto status = cublasLtMatmulAlgoConfigGetAttribute(
        &algo, ids[i], &value, sizeof(value), &written);
    std::printf(
        "%s{\"name\":\"%s\",\"status\":%d,\"bytes_written\":%zu,\"value\":%u}",
        i ? "," : "", names[i], static_cast<int>(status), written, value);
    if (status != CUBLAS_STATUS_SUCCESS || written != sizeof(value))
      Error("algo_attribute", static_cast<int>(status));
  }
  std::printf("]}\n");
}
}  // namespace

int main(int argc, char** argv) {
  std::setvbuf(stdout, nullptr, _IOLBF, 0);
  if (argc != 5 || std::strcmp(argv[1], "--mode") != 0 ||
      std::strcmp(argv[3], "--release") != 0 ||
      (std::strcmp(argv[2], "cupti") != 0 &&
       std::strcmp(argv[2], "nsys") != 0)) {
    std::fprintf(
        stderr,
        "usage: mechanism_probe --mode cupti|nsys --release ABSOLUTE_PATH\n");
    return 2;
  }
  const bool cupti = std::strcmp(argv[2], "cupti") == 0;
  const std::filesystem::path release(argv[4]);
  Resources resources;
  bool profiler_started = false, dumped = false, root_range = false;
  int returncode = 1;
  try {
    if (!release.is_absolute() || std::filesystem::exists(release) ||
        std::filesystem::is_symlink(release))
      throw std::runtime_error("release must be absent and absolute");
    if (std::getenv("Q4T_ABLATE_GEMV_T1") != nullptr ||
        std::getenv("Q4T_ABLATE_GEMM_TILE") != nullptr)
      throw std::runtime_error("production ablation environment forbidden");
    if (cupti) BeginCollector();
    // No CUPTI API, subscriber or activity enable call occurs in nsys mode.
    Library("cudart",
            reinterpret_cast<const void*>(
                static_cast<cudaError_t (*)(void**, size_t)>(&cudaMalloc)));
    Library("cublasLt", reinterpret_cast<const void*>(&cublasLtMatmul));
    Library("cupti_linked", reinterpret_cast<const void*>(&cuptiSubscribe));
    int runtime_version = 0, driver_version = 0;
    Check(cudaRuntimeGetVersion(&runtime_version), "runtime_version");
    Check(cudaDriverGetVersion(&driver_version), "driver_version");
    Check(cudaStreamCreateWithFlags(&resources.stream, cudaStreamNonBlocking),
          "stream_create");
    Check(cudaMalloc(&resources.x, kM * kK * sizeof(uint16_t)), "allocate_x");
    Check(cudaMalloc(&resources.w, kN * kK * sizeof(uint16_t)), "allocate_w");
    Check(cudaMalloc(&resources.y, kM * kN * sizeof(uint16_t)), "allocate_y");
    Check(cudaMalloc(&resources.control, 512), "allocate_control");
    Check(cudaMalloc(&resources.workspace, kWorkspace), "allocate_workspace");
    std::vector<uint16_t> x(kM * kK, 0), w(kN * kK), expected(kM * kN);
    for (int m = 0; m < kM; ++m) x[m * kK + m] = Bf16(m + 1);
    for (int n = 0; n < kN; ++n)
      for (int k = 0; k < kK; ++k)
        w[n * kK + k] = Bf16((n % 7 - 3) * (k % 5 + 1));
    for (int m = 0; m < kM; ++m)
      for (int n = 0; n < kN; ++n)
        expected[m * kN + n] = Bf16((n % 7 - 3) * (m % 5 + 1) * (m + 1));
    const std::vector<uint8_t> initial(512, 0xa5);
    Check(cudaMemcpyAsync(resources.x, x.data(), x.size() * sizeof(uint16_t),
                          cudaMemcpyHostToDevice, resources.stream),
          "copy_x");
    Check(cudaMemcpyAsync(resources.w, w.data(), w.size() * sizeof(uint16_t),
                          cudaMemcpyHostToDevice, resources.stream),
          "copy_w");
    Check(cudaMemcpyAsync(resources.control, initial.data(), initial.size(),
                          cudaMemcpyHostToDevice, resources.stream),
          "initialize_control");
    Check(cudaStreamSynchronize(resources.stream), "initial_sync");
    if (CacheSize() != 0)
      throw std::runtime_error("BF16 cache must initially be empty");
    std::printf(
        "[mechanism-config] "
        "{\"schema\":1,\"pid\":%d,\"tid\":%u,\"mode\":\"%s\",\"M\":4,\"N\":48,"
        "\"K\":2560,\"workspace_bytes\":33554432,\"runtime_version\":%d,"
        "\"driver_version\":%d,\"cupti_header_api_version\":%d,\"cublasLt_"
        "version\":%zu,\"control_bytes\":512,\"control_initial_value\":165,"
        "\"control_value\":90,\"x\":%llu,\"w\":%llu,\"y\":%llu,\"control\":%"
        "llu,\"workspace\":%llu,\"stream\":%llu,\"x_alignment_mod256\":%zu,\"w_"
        "alignment_mod256\":%zu,\"y_alignment_mod256\":%zu,\"workspace_"
        "alignment_mod256\":%zu,\"release_timeout_seconds\":60}\n",
        getpid(), Tid(), argv[2], runtime_version, driver_version,
        CUPTI_API_VERSION, cublasLtGetVersion(),
        static_cast<unsigned long long>(
            reinterpret_cast<uintptr_t>(resources.x)),
        static_cast<unsigned long long>(
            reinterpret_cast<uintptr_t>(resources.w)),
        static_cast<unsigned long long>(
            reinterpret_cast<uintptr_t>(resources.y)),
        static_cast<unsigned long long>(
            reinterpret_cast<uintptr_t>(resources.control)),
        static_cast<unsigned long long>(
            reinterpret_cast<uintptr_t>(resources.workspace)),
        static_cast<unsigned long long>(
            reinterpret_cast<uintptr_t>(resources.stream)),
        reinterpret_cast<uintptr_t>(resources.x) % 256,
        reinterpret_cast<uintptr_t>(resources.w) % 256,
        reinterpret_cast<uintptr_t>(resources.y) % 256,
        reinterpret_cast<uintptr_t>(resources.workspace) % 256);
    Check(cudaProfilerStart(), "profiler_start");
    profiler_started = true;
    nvtxRangePushA("mechanism|root");
    root_range = true;
    for (int ordinal = 0; ordinal < 2; ++ordinal) {
      PhaseBegin(ordinal);
      Check(cudaMemsetAsync(resources.control, 0x5a, ordinal == 0 ? 0 : 512,
                            resources.stream),
            "control_memset");
      Check(cudaStreamSynchronize(resources.stream), "control_sync");
      std::vector<uint8_t> actual(512);
      Check(cudaMemcpy(actual.data(), resources.control, actual.size(),
                       cudaMemcpyDeviceToHost),
            "control_readback");
      CheckWords(kPhases[ordinal], actual,
                 std::vector<uint8_t>(512, ordinal == 0 ? 0xa5 : 0x5a));
      PhaseEnd(ordinal);
    }
    for (int ordinal = 2; ordinal < 4; ++ordinal) {
      PhaseBegin(ordinal);
      const size_t before = CacheSize();
      if (before != static_cast<size_t>(ordinal - 2))
        throw std::runtime_error("unexpected cache miss/hit order");
      const auto result = q4t::model::Bf16Gemm(
          resources.x, resources.w, resources.y, kM, kN, kK, 1.0f, 0.0f,
          resources.workspace, kWorkspace, resources.stream);
      std::printf(
          "[mechanism-event] "
          "{\"event\":\"gemm_return\",\"phase\":\"%s\",\"status\":%d,\"has_"
          "algo\":%s}\n",
          kPhases[ordinal], static_cast<int>(result.status),
          result.has_algo ? "true" : "false");
      if (result.status != CUBLAS_STATUS_SUCCESS || !result.has_algo)
        throw std::runtime_error("production BF16 GEMM failed");
      Check(cudaStreamSynchronize(resources.stream), "gemm_sync");
      Algo(kPhases[ordinal], before);
      std::vector<uint16_t> actual(kM * kN);
      Check(
          cudaMemcpy(actual.data(), resources.y,
                     actual.size() * sizeof(uint16_t), cudaMemcpyDeviceToHost),
          "gemm_readback");
      CheckWords(kPhases[ordinal], actual, expected);
      PhaseEnd(ordinal);
    }
    Check(cudaDeviceSynchronize(), "final_sync");
    nvtxRangePop();
    root_range = false;
    Check(cudaProfilerStop(), "profiler_stop");
    profiler_started = false;
    if (cupti) {
      EndCollector();
      if (!collector_finalized) Error("collector_not_finalized", 1);
      for (const auto& used : buffer_used)
        if (used.load()) Error("activity_buffer_not_returned", 1);
    }
    DumpRecords(cupti);
    dumped = true;
    if (errors.load() || overflow.load() || drops.load() ||
        api_enters.load() != api_exits.load())
      throw std::runtime_error("collector integrity failed");
    std::printf(
        "[mechanism-ready] "
        "{\"pid\":%d,\"tid\":%u,\"mode\":\"%s\",\"passed\":true,\"gemm_calls\":"
        "2,\"controls\":2,\"M\":4,\"N\":48,\"K\":2560,\"workspace_bytes\":"
        "33554432,\"profiler_start_error\":0,\"profiler_stop_error\":0,\"final_"
        "sync_error\":0}\n",
        getpid(), Tid(), argv[2]);
    const auto end =
        std::chrono::steady_clock::now() + std::chrono::seconds(60);
    while (!std::filesystem::exists(release)) {
      if (std::chrono::steady_clock::now() >= end)
        throw std::runtime_error("release deadline exceeded");
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (std::chrono::steady_clock::now() >= end ||
        !std::filesystem::is_regular_file(release) ||
        std::filesystem::is_symlink(release))
      throw std::runtime_error("late or nonregular release");
    returncode = resources.Release() ? 0 : 1;
  } catch (const std::exception& error) {
    std::printf("[mechanism-error] {\"operation\":\"main\",\"message\":%s}\n",
                Json(error.what()).c_str());
    if (root_range) nvtxRangePop();
    if (profiler_started) {
      const auto status = cudaProfilerStop();
      if (status != cudaSuccess)
        Error("failure_profiler_stop", static_cast<int>(status));
    }
    if (cupti && subscribed) {
      try {
        EndCollector();
      } catch (const std::exception& cleanup) {
        std::printf(
            "[mechanism-error] "
            "{\"operation\":\"collector_cleanup\",\"message\":%s}\n",
            Json(cleanup.what()).c_str());
      }
    }
    if (!dumped && !subscribed && !activity_enabled &&
        callbacks_active.load() == 0)
      DumpRecords(cupti);
    else if (!dumped)
      std::printf(
          "[mechanism-error] "
          "{\"operation\":\"unclosed_collector_dump_skipped\"}\n");
    resources.Release();
  }
  std::printf(
      "[mechanism-exit] "
      "{\"pid\":%d,\"tid\":%u,\"released\":%s,\"returncode\":%d}\n",
      getpid(), Tid(), returncode == 0 ? "true" : "false", returncode);
  return returncode;
}
