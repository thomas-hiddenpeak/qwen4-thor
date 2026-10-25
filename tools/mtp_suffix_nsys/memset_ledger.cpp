// One-range, opt-in CUPTI parameter ledger. This is not a model modification.
// No CUDA synchronization or cuptiFinalize is added. Fixed buffers live until
// process exit because CUPTI activity callbacks must never see freed storage.
#include <cuda_profiler_api.h>
#include <cuda_runtime.h>
#include <cupti.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <utility>

namespace {
constexpr size_t kCapacity = 131072;
constexpr size_t kBufferBytes = 1U << 20;
constexpr size_t kBufferCount = 8;
constexpr size_t kDeviceBufferBytes = 1U << 20;
constexpr size_t kDeviceBufferCount = 4;
constexpr size_t kLimit = 64U << 20;
constexpr size_t kTerminalReserve = 32768;
constexpr CUpti_CallbackId kRuntimeIds[] = {
    CUPTI_RUNTIME_TRACE_CBID_cudaMemset_v3020,
    CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_v3020,
    CUPTI_RUNTIME_TRACE_CBID_cudaMemset_ptds_v7000,
    CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_ptsz_v7000};
// All executing cu[64]Memset families in this pinned header, including legacy,
// v2 and per-thread variants. Graph construction/query callbacks are not work.
constexpr CUpti_CallbackId kDriverIds[] = {
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD8,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD8,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD16,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD16,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD32,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD32,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D8,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD2D8,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D16,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD2D16,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D32,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD2D32,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD8Async,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD8Async,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD16Async,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD16Async,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD32Async,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD32Async,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D8Async,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD2D8Async,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D16Async,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD2D16Async,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D32Async,
    CUPTI_DRIVER_TRACE_CBID_cu64MemsetD2D32Async,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD8_v2,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD16_v2,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD32_v2,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D8_v2,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D16_v2,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D32_v2,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD8_v2_ptds,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD16_v2_ptds,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD32_v2_ptds,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D8_v2_ptds,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D16_v2_ptds,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D32_v2_ptds,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD8Async_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD16Async_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD32Async_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D8Async_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D16Async_ptsz,
    CUPTI_DRIVER_TRACE_CBID_cuMemsetD2D32Async_ptsz};

struct Record {
  uint64_t seq = 0, enter_seq = 0, parent_entry = 0;
  uint64_t start = 0, end = 0, bytes = 0, dev_ptr = 0, stream = 0;
  uint64_t graph_node = 0;
  uint32_t correlation = 0, parent_runtime = 0, pid = 0, tid = 0;
  uint32_t context = 0, stream_id = 0, device = 0, cbid = 0, graph = 0;
  int type = 0, domain = 0, site = 0, kind = 0, value = 0, retval = -1;
  bool is_memset = false, has_stream_parameter = false;
  char name[96]{};
};
struct ErrorRecord {
  char operation[96]{};
  int code = 0;
};
struct Frame {
  uint64_t entry = 0;
  uint32_t correlation = 0;
};
alignas(8) std::array<Record, kCapacity> records;
std::array<std::atomic<bool>, kCapacity> published{};
alignas(8) uint8_t buffers[kBufferCount][kBufferBytes];
std::array<std::atomic<bool>, kBufferCount> buffer_used{};
std::array<ErrorRecord, 64> error_records;
std::atomic<uint64_t> next_record{0}, errors{0}, overflow{0}, drops{0};
std::atomic<uint64_t> api_enters{0}, api_exits{0}, activities{0};
std::atomic<uint64_t> buffer_requests{0}, buffer_completions{0};
std::atomic<int> active_callbacks{0};
thread_local std::array<Frame, 64> runtime_stack{};
thread_local size_t runtime_depth = 0;
std::mutex boundary_mutex;
CUpti_SubscriberHandle subscriber = nullptr;
bool subscribed = false, activities_enabled = false, disabled = false;
bool unsubscribed = false, flushed = false, closed = false;
bool ever_subscribed = false;
bool start_attempted = false, stop_attempted = false;
int log_fd = -1;
uint64_t log_bytes = 0, start_calls = 0, stop_calls = 0;
int start_return = -1, stop_return = -1;
uint32_t owner_pid = 0;
constexpr size_t kOwnedBytes = sizeof(records) + sizeof(published) +
                               sizeof(buffers) + sizeof(buffer_used) +
                               sizeof(error_records);
constexpr size_t kConfiguredBytes =
    kDeviceBufferBytes * (kDeviceBufferCount + 1);
static_assert(kOwnedBytes + kConfiguredBytes < kLimit);

uint32_t Tid() { return static_cast<uint32_t>(syscall(SYS_gettid)); }

void Error(const char* operation, int code) noexcept {
  const uint64_t index = errors.fetch_add(1);
  if (index >= error_records.size()) return;
  auto& row = error_records[index];
  const size_t n = std::min(std::strlen(operation), sizeof(row.operation) - 1);
  std::memcpy(row.operation, operation, n);
  row.operation[n] = '\0';
  row.code = code;
}

bool Check(CUptiResult result, const char* operation) {
  if (result == CUPTI_SUCCESS) return true;
  Error(operation, static_cast<int>(result));
  return false;
}

void StderrError(const char* operation, int code) {
  std::fprintf(stderr, "[memset-ledger-error] %s code=%d\n", operation, code);
}

Record* Reserve() {
  const uint64_t index = next_record.fetch_add(1);
  if (index >= kCapacity) {
    ++overflow;
    return nullptr;
  }
  Record* row = &records[index];
  row->seq = index + 1;
  return row;
}

void Publish(Record* row) {
  if (row)
    published[row - records.data()].store(true, std::memory_order_release);
}

void Name(Record* row, const char* name) {
  if (!name) {
    Error("missing_callback_name", 1);
    return;
  }
  const size_t n = std::strlen(name);
  if (n >= sizeof(row->name)) {
    Error("callback_name_too_long", 1);
    return;
  }
  for (size_t i = 0; i < n; ++i) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    if (c < 32 || c == '"' || c == '\\') {
      Error("callback_name_not_plain", 1);
      return;
    }
  }
  std::memcpy(row->name, name, n + 1);
}

struct Active {
  Active() { ++active_callbacks; }
  ~Active() { --active_callbacks; }
};

template <class Params>
void Parameters(Record* row, const void* pointer) {
  const auto* p = static_cast<const Params*>(pointer);
  row->dev_ptr = reinterpret_cast<uintptr_t>(p->devPtr);
  row->bytes = p->count;
  row->value = p->value;
  if constexpr (requires { p->stream; }) {
    row->stream = reinterpret_cast<uintptr_t>(p->stream);
    row->has_stream_parameter = true;
  }
}

void CUPTIAPI Callback(void*, CUpti_CallbackDomain domain, CUpti_CallbackId id,
                       const void* opaque) {
  Active active;
  if (domain != CUPTI_CB_DOMAIN_RUNTIME_API &&
      domain != CUPTI_CB_DOMAIN_DRIVER_API) {
    Error("unexpected_callback_domain", static_cast<int>(domain));
    return;
  }
  if (!opaque) {
    Error("missing_callback_data", 1);
    return;
  }
  const auto* data = static_cast<const CUpti_CallbackData*>(opaque);
  const bool runtime = domain == CUPTI_CB_DOMAIN_RUNTIME_API;
  const bool enter = data->callbackSite == CUPTI_API_ENTER;
  if (!enter && data->callbackSite != CUPTI_API_EXIT) {
    Error("unknown_callback_site", 1);
    return;
  }
  Record* row = Reserve();
  if (enter) {
    ++api_enters;
    if (!data->correlationData)
      Error("missing_correlation_data", 1);
    else
      *data->correlationData = row ? row->seq : 0;
    if (row) {
      row->type = 1;
      row->domain = runtime ? 1 : 2;
      row->site = 1;
      row->pid = static_cast<uint32_t>(getpid());
      row->tid = Tid();
      row->cbid = id;
      row->correlation = data->correlationId;
      row->context = data->contextUid;
      row->enter_seq = row->seq;
      row->parent_runtime =
          runtime_depth ? runtime_stack[runtime_depth - 1].correlation : 0;
      row->parent_entry =
          runtime_depth ? runtime_stack[runtime_depth - 1].entry : 0;
      Name(row, data->functionName);
      if (runtime) {
        row->is_memset = true;
        if (!data->functionParams)
          Error("missing_memset_parameters", 1);
        else
          switch (id) {
            case CUPTI_RUNTIME_TRACE_CBID_cudaMemset_v3020:
              Parameters<cudaMemset_v3020_params>(row, data->functionParams);
              break;
            case CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_v3020:
              Parameters<cudaMemsetAsync_v3020_params>(row,
                                                       data->functionParams);
              break;
            case CUPTI_RUNTIME_TRACE_CBID_cudaMemset_ptds_v7000:
              Parameters<cudaMemset_ptds_v7000_params>(row,
                                                       data->functionParams);
              break;
            case CUPTI_RUNTIME_TRACE_CBID_cudaMemsetAsync_ptsz_v7000:
              Parameters<cudaMemsetAsync_ptsz_v7000_params>(
                  row, data->functionParams);
              break;
            default:
              Error("unexpected_runtime_cbid", static_cast<int>(id));
          }
      } else if (!runtime_depth) {
        // No fabricated count is attached to a direct driver operation.
        Error("driver_memset_without_selected_runtime_owner",
              static_cast<int>(id));
      }
      Publish(row);
    }
    if (runtime) {
      if (runtime_depth == runtime_stack.size()) {
        ++overflow;
        Error("runtime_stack_overflow", 1);
      } else {
        runtime_stack[runtime_depth++] = {row ? row->seq : 0,
                                          data->correlationId};
      }
    }
  } else {
    ++api_exits;
    const uint64_t entry = data->correlationData ? *data->correlationData : 0;
    if (row) {
      const uint64_t seq = row->seq;
      if (!entry || entry > kCapacity ||
          !published[entry - 1].load(std::memory_order_acquire)) {
        Error("callback_exit_missing_enter", 1);
        row->type = 1;
        row->domain = runtime ? 1 : 2;
        row->pid = static_cast<uint32_t>(getpid());
        row->tid = Tid();
        row->cbid = id;
        Name(row, data->functionName);
      } else {
        *row = records[entry - 1];
        row->seq = seq;
        if (row->correlation != data->correlationId || row->tid != Tid() ||
            row->pid != static_cast<uint32_t>(getpid()) || row->cbid != id ||
            row->domain != (runtime ? 1 : 2))
          Error("callback_exit_identity_changed", 1);
      }
      row->site = 2;
      if (!data->functionReturnValue)
        Error("callback_missing_return", 1);
      else
        row->retval = *static_cast<const int*>(data->functionReturnValue);
      Publish(row);
    }
    if (runtime) {
      if (!runtime_depth || runtime_stack[runtime_depth - 1].entry != entry ||
          runtime_stack[runtime_depth - 1].correlation != data->correlationId)
        Error("runtime_stack_exit_mismatch", 1);
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
                             uint8_t* buffer, size_t size, size_t valid) {
  Active active;
  ++buffer_completions;
  size_t index = kBufferCount;
  for (size_t i = 0; i < kBufferCount; ++i)
    if (buffer == buffers[i]) index = i;
  if (index == kBufferCount || size != kBufferBytes || valid > size) {
    Error("foreign_or_bad_activity_buffer", 1);
    return;
  }
  CUpti_Activity* raw = nullptr;
  CUptiResult status;
  while ((status = cuptiActivityGetNextRecord(buffer, valid, &raw)) ==
         CUPTI_SUCCESS) {
    ++activities;
    Record* row = Reserve();
    if (!row) continue;
    row->type = 2;
    row->pid = static_cast<uint32_t>(getpid());
    if (raw->kind == CUPTI_ACTIVITY_KIND_RUNTIME) {
      const auto* p = reinterpret_cast<const CUpti_ActivityAPI*>(raw);
      row->kind = 1;
      row->start = p->start;
      row->end = p->end;
      row->correlation = p->correlationId;
      row->pid = p->processId;
      row->tid = p->threadId;
      row->cbid = p->cbid;
      row->retval = static_cast<int>(p->returnValue);
      const char* name = nullptr;
      if (Check(
              cuptiGetCallbackName(CUPTI_CB_DOMAIN_RUNTIME_API, p->cbid, &name),
              "activity_callback_name"))
        Name(row, name);
    } else if (raw->kind == CUPTI_ACTIVITY_KIND_MEMSET) {
      const auto* p = reinterpret_cast<const CUpti_ActivityMemset4*>(raw);
      row->kind = 2;
      row->start = p->start;
      row->end = p->end;
      row->correlation = p->correlationId;
      row->bytes = p->bytes;
      row->value = static_cast<int>(p->value);
      row->context = p->contextId;
      row->device = p->deviceId;
      row->stream_id = p->streamId;
      row->graph = p->graphId;
      row->graph_node = p->graphNodeId;
    } else {
      Error("unexpected_activity_kind", static_cast<int>(raw->kind));
    }
    Publish(row);
  }
  if (status != CUPTI_ERROR_MAX_LIMIT_REACHED)
    Error("activity_record_decode", static_cast<int>(status));
  size_t lost = 0;
  if (Check(cuptiActivityGetNumDroppedRecords(context, stream_id, &lost),
            "activity_drop_query"))
    drops.fetch_add(lost);
  buffer_used[index].store(false);
}

bool Write(bool terminal, const char* format, ...) {
  if (log_fd < 0) return false;
  char text[16384];
  va_list args;
  va_start(args, format);
  const int size = std::vsnprintf(text, sizeof(text), format, args);
  va_end(args);
  const size_t limit = terminal ? kLimit : kLimit - kTerminalReserve;
  if (size < 0 || static_cast<size_t>(size) >= sizeof(text) ||
      log_bytes + static_cast<size_t>(std::max(size, 0)) > limit) {
    Error("log_capacity_or_format", 1);
    return false;
  }
  size_t written = 0;
  while (written < static_cast<size_t>(size)) {
    const ssize_t n = write(log_fd, text + written, size - written);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      Error("log_write", n < 0 ? errno : EIO);
      return false;
    }
    written += static_cast<size_t>(n);
    log_bytes += static_cast<size_t>(n);
  }
  return true;
}

bool Quote(const char* source, char* output, size_t capacity) {
  if (!source || capacity < 3) return false;
  size_t used = 0;
  output[used++] = '"';
  for (const unsigned char* p = reinterpret_cast<const unsigned char*>(source);
       *p; ++p) {
    if (*p < 32 || used + 3 >= capacity) return false;
    if (*p == '"' || *p == '\\') output[used++] = '\\';
    output[used++] = static_cast<char>(*p);
  }
  output[used++] = '"';
  output[used] = '\0';
  return true;
}

bool LibraryPath(void* symbol, char* encoded, size_t capacity) {
  Dl_info info{};
  char path[PATH_MAX];
  if (!symbol || !dladdr(symbol, &info) || !info.dli_fname ||
      !realpath(info.dli_fname, path) || !Quote(path, encoded, capacity)) {
    Error("library_metadata", 1);
    return false;
  }
  return true;
}

using ProfilerFunction = cudaError_t (*)();
ProfilerFunction original_start = nullptr, original_stop = nullptr;

bool Resolve() {
  dlerror();
  void* begin = dlsym(RTLD_NEXT, "cudaProfilerStart");
  const bool begin_ok = begin && !dlerror();
  dlerror();
  void* end = dlsym(RTLD_NEXT, "cudaProfilerStop");
  const bool end_ok = end && !dlerror();
  if (!begin_ok || !end_ok) {
    Error("original_profiler_symbol", 1);
    return false;
  }
  static_assert(sizeof(begin) == sizeof(original_start));
  std::memcpy(&original_start, &begin, sizeof(begin));
  std::memcpy(&original_stop, &end, sizeof(end));
  return true;
}

bool OpenLog() {
  const char* path = std::getenv("Q4T_MEMSET_LEDGER_PATH");
  if (!path || path[0] != '/') {
    Error("missing_absolute_ledger_path", 1);
    return false;
  }
  log_fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
                S_IRUSR | S_IWUSR);
  if (log_fd < 0) {
    Error("ledger_open_exclusive", errno);
    return false;
  }
  struct stat info {};
  if (fstat(log_fd, &info) != 0 || !S_ISREG(info.st_mode) ||
      info.st_nlink != 1) {
    Error("ledger_file_identity", errno);
    return false;
  }
  return true;
}

void Config() {
  int runtime_version = 0, driver_version = 0;
  uint32_t cupti_version = 0;
  const auto runtime_result = cudaRuntimeGetVersion(&runtime_version);
  const auto driver_result = cudaDriverGetVersion(&driver_version);
  if (runtime_result != cudaSuccess) Error("runtime_version", runtime_result);
  if (driver_result != cudaSuccess) Error("driver_version", driver_result);
  Check(cuptiGetVersion(&cupti_version), "cupti_version");
  void* cublas_symbol = dlsym(RTLD_DEFAULT, "cublasLtGetVersion");
  size_t (*get_cublas_version)() = nullptr;
  static_assert(sizeof(cublas_symbol) == sizeof(get_cublas_version));
  std::memcpy(&get_cublas_version, &cublas_symbol, sizeof(cublas_symbol));
  const size_t cublas_version = get_cublas_version ? get_cublas_version() : 0;
  if (!cublas_version) Error("cublas_version", 1);
  char cudart[PATH_MAX * 2 + 3] = "null";
  char cupti[PATH_MAX * 2 + 3] = "null";
  char cublas[PATH_MAX * 2 + 3] = "null";
  LibraryPath(reinterpret_cast<void*>(original_start), cudart, sizeof(cudart));
  LibraryPath(reinterpret_cast<void*>(&cuptiGetVersion), cupti, sizeof(cupti));
  LibraryPath(cublas_symbol, cublas, sizeof(cublas));
  Write(false,
        "{\"event\":\"config\",\"schema\":1,\"mode\":\"memset_ledger\","
        "\"pid\":%u,\"tid\":%u,\"runtime_cbids\":[49,51,233,235],"
        "\"record_capacity\":%zu,\"thread_id_type\":\"SYSTEM\","
        "\"runtime_version\":%d,\"driver_version\":%d,\"cupti_version\":%u,"
        "\"cupti_header_api_version\":%d,\"cublasLt_version\":%zu,"
        "\"collector_limit_bytes\":%zu,\"collector_owned_bytes\":%zu,"
        "\"configured_cupti_buffer_bytes\":%zu,\"log_limit_bytes\":%zu,"
        "\"host_buffer_count\":%zu,\"host_buffer_bytes\":%zu,"
        "\"device_buffer_count\":%zu,\"device_buffer_bytes\":%zu,"
        "\"libraries\":[{\"name\":\"cudart\",\"path\":%s},"
        "{\"name\":\"cupti_linked\",\"path\":%s},"
        "{\"name\":\"cublasLt\",\"path\":%s}]}\n",
        owner_pid, Tid(), kCapacity, runtime_version, driver_version,
        cupti_version, CUPTI_API_VERSION, cublas_version, kLimit, kOwnedBytes,
        kConfiguredBytes, kLimit, kBufferCount, kBufferBytes,
        kDeviceBufferCount, kDeviceBufferBytes, cudart, cupti, cublas);
}

void Begin() {
  if (!Check(cuptiSubscribe(&subscriber, Callback, nullptr), "cupti_subscribe"))
    return;
  subscribed = true;
  ever_subscribed = true;
  Check(cuptiSetThreadIdType(CUPTI_ACTIVITY_THREAD_ID_TYPE_SYSTEM),
        "set_system_thread_id");
  CUpti_ActivityThreadIdType thread_type =
      CUPTI_ACTIVITY_THREAD_ID_TYPE_DEFAULT;
  if (Check(cuptiGetThreadIdType(&thread_type), "get_thread_id_type") &&
      thread_type != CUPTI_ACTIVITY_THREAD_ID_TYPE_SYSTEM)
    Error("thread_id_type_not_system", static_cast<int>(thread_type));
  for (const auto& setting :
       std::array<std::pair<CUpti_ActivityAttribute, size_t>, 4>{
           {{CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_SIZE, kDeviceBufferBytes},
            {CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_POOL_LIMIT, kDeviceBufferCount},
            {CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_PRE_ALLOCATE_VALUE, 1},
            {CUPTI_ACTIVITY_ATTR_DEVICE_BUFFER_SIZE_CDP,
             kDeviceBufferBytes}}}) {
    size_t value = setting.second, size = sizeof(value);
    Check(cuptiActivitySetAttribute(setting.first, &size, &value),
          "activity_buffer_attribute");
  }
  Check(cuptiActivityRegisterCallbacks(BufferRequested, BufferComplete),
        "register_activity_buffers");
  for (auto id : kRuntimeIds) {
    Check(cuptiEnableCallback(1, subscriber, CUPTI_CB_DOMAIN_RUNTIME_API, id),
          "enable_runtime_callback");
    Check(cuptiActivityEnableRuntimeApi(id, 1), "enable_runtime_activity");
  }
  for (auto id : kDriverIds)
    Check(cuptiEnableCallback(1, subscriber, CUPTI_CB_DOMAIN_DRIVER_API, id),
          "enable_driver_bridge");
  Check(cuptiActivityEnable(CUPTI_ACTIVITY_KIND_MEMSET),
        "enable_memset_activity");
  activities_enabled = true;
}

uint64_t Timestamp() {
  uint64_t value = 0;
  Check(cuptiGetTimestamp(&value), "boundary_timestamp");
  if (!value) Error("zero_boundary_timestamp", 1);
  return value;
}

void End() {
  if (activities_enabled) {
    const bool first = Check(cuptiActivityFlushAll(0), "activity_flush_0");
    bool all_disabled = Check(cuptiActivityDisable(CUPTI_ACTIVITY_KIND_MEMSET),
                              "disable_memset_activity");
    for (auto id : kRuntimeIds)
      all_disabled &= Check(cuptiActivityEnableRuntimeApi(id, 0),
                            "disable_runtime_activity");
    for (auto id : kRuntimeIds)
      all_disabled &= Check(
          cuptiEnableCallback(0, subscriber, CUPTI_CB_DOMAIN_RUNTIME_API, id),
          "disable_runtime_callback");
    for (auto id : kDriverIds)
      all_disabled &= Check(
          cuptiEnableCallback(0, subscriber, CUPTI_CB_DOMAIN_DRIVER_API, id),
          "disable_driver_callback");
    disabled = all_disabled;
    const bool final =
        Check(cuptiActivityFlushAll(CUPTI_ACTIVITY_FLAG_FLUSH_FORCED),
              "activity_flush_1");
    flushed = first && final;
    activities_enabled = false;
  }
  if (subscribed) {
    unsubscribed = Check(cuptiUnsubscribe(subscriber), "cupti_unsubscribe");
    subscribed = !unsubscribed;
  }
  // No cuptiFinalize: its non-callback contract requires prior CUDA sync.
  // Disabled fixed buffers remain owned until process exit, never dlclose.
  if (active_callbacks.load() != 0) Error("callbacks_not_quiescent", 1);
}

void Dump() {
  const uint64_t count = std::min<uint64_t>(next_record.load(), kCapacity);
  for (uint64_t i = 0; i < count; ++i) {
    if (!published[i].load(std::memory_order_acquire)) {
      Error("unpublished_record", 1);
      continue;
    }
    const Record& r = records[i];
    if (r.type == 1) {
      if (!Write(false,
                 "{\"event\":\"api\",\"seq\":%llu,\"enter_seq\":%llu,"
                 "\"domain\":\"%s\",\"site\":\"%s\",\"name\":\"%s\","
                 "\"pid\":%u,\"tid\":%u,\"correlation_id\":%u,"
                 "\"parent_runtime_correlation_id\":%u,\"parent_runtime_enter_"
                 "seq\":%llu,"
                 "\"context_uid\":%u,\"cbid\":%u,\"retval\":%d,"
                 "\"is_memset\":%s,\"count\":%llu,\"dev_ptr\":%llu,"
                 "\"value\":%d,\"stream\":%llu,\"stream_parameter_present\":%s,"
                 "\"phase\":\"capture\"}\n",
                 static_cast<unsigned long long>(r.seq),
                 static_cast<unsigned long long>(r.enter_seq),
                 r.domain == 1 ? "runtime" : "driver",
                 r.site == 1 ? "enter" : "exit", r.name, r.pid, r.tid,
                 r.correlation, r.parent_runtime,
                 static_cast<unsigned long long>(r.parent_entry), r.context,
                 r.cbid, r.retval, r.is_memset ? "true" : "false",
                 static_cast<unsigned long long>(r.bytes),
                 static_cast<unsigned long long>(r.dev_ptr), r.value,
                 static_cast<unsigned long long>(r.stream),
                 r.has_stream_parameter ? "true" : "false"))
        break;
    } else {
      if (!Write(
              false,
              "{\"event\":\"activity\",\"seq\":%llu,\"kind\":\"%s\","
              "\"name\":\"%s\",\"correlation_id\":%u,\"runtime_correlation_"
              "id\":null,"
              "\"start\":%llu,\"end\":%llu,\"process_id\":%u,"
              "\"process_id_source\":\"%s\",\"thread_id\":%u,"
              "\"context_id\":%u,\"device_id\":%u,\"stream_id\":%u,"
              "\"bytes\":%llu,\"value\":%d,\"cbid\":%u,\"return_value\":%d,"
              "\"graph_id\":%u,\"graph_node_id\":%llu,\"phase\":\"capture\"}\n",
              static_cast<unsigned long long>(r.seq),
              r.kind == 1 ? "runtime" : "memset", r.name, r.correlation,
              static_cast<unsigned long long>(r.start),
              static_cast<unsigned long long>(r.end), r.pid,
              r.kind == 1 ? "activity_record" : "collector_process", r.tid,
              r.context, r.device, r.stream_id,
              static_cast<unsigned long long>(r.bytes), r.value, r.cbid,
              r.retval, r.graph, static_cast<unsigned long long>(r.graph_node)))
        break;
    }
  }
}

void Finish(uint64_t timestamp) {
  Dump();
  Write(true,
        "{\"event\":\"boundary\",\"site\":\"end\",\"original_return\":%d,"
        "\"timestamp\":%llu,\"pid\":%u,\"tid\":%u}\n",
        stop_return, static_cast<unsigned long long>(timestamp), owner_pid,
        Tid());
  uint64_t outstanding = 0;
  for (auto& used : buffer_used) outstanding += used.load() ? 1 : 0;
  if (outstanding) Error("buffers_outstanding", static_cast<int>(outstanding));
  if (log_fd >= 0 && fsync(log_fd) != 0)
    Error("log_fsync_before_summary", errno);
  for (uint64_t i = 0;
       i < std::min<uint64_t>(errors.load(), error_records.size()); ++i) {
    const auto& e = error_records[i];
    Write(true, "{\"event\":\"error\",\"operation\":\"%s\",\"code\":%d}\n",
          e.operation, e.code);
  }
  const bool complete =
      errors.load() == 0 && overflow.load() == 0 && drops.load() == 0 &&
      start_calls == 1 && stop_calls == 1 && start_return == 0 &&
      stop_return == 0 && disabled && unsubscribed && flushed &&
      outstanding == 0 && active_callbacks.load() == 0;
  Write(
      true,
      "{\"event\":\"summary\",\"mode\":\"memset_ledger\","
      "\"pid\":%u,\"tid\":%u,\"api_enter_count\":%llu,\"api_exit_count\":%llu,"
      "\"activity_count\":%llu,\"record_count\":%llu,\"drops\":%llu,"
      "\"overflow\":%llu,\"errors\":%llu,\"buffer_requests\":%llu,"
      "\"buffer_completions\":%llu,\"buffers_outstanding\":%llu,"
      "\"collector_limit_bytes\":%zu,\"collector_owned_bytes\":%zu,"
      "\"configured_cupti_buffer_bytes\":%zu,\"original_start_calls\":%llu,"
      "\"original_stop_calls\":%llu,\"flush_flags\":[0,1],"
      "\"disabled\":%s,\"unsubscribed\":%s,\"finalized\":false,"
      "\"cuda_sync_added\":false,\"flushed\":%s,\"subscriber_used\":%s,"
      "\"completed\":%s}\n",
      owner_pid, Tid(), static_cast<unsigned long long>(api_enters.load()),
      static_cast<unsigned long long>(api_exits.load()),
      static_cast<unsigned long long>(activities.load()),
      static_cast<unsigned long long>(next_record.load()),
      static_cast<unsigned long long>(drops.load()),
      static_cast<unsigned long long>(overflow.load()),
      static_cast<unsigned long long>(errors.load()),
      static_cast<unsigned long long>(buffer_requests.load()),
      static_cast<unsigned long long>(buffer_completions.load()),
      static_cast<unsigned long long>(outstanding), kLimit, kOwnedBytes,
      kConfiguredBytes, static_cast<unsigned long long>(start_calls),
      static_cast<unsigned long long>(stop_calls), disabled ? "true" : "false",
      unsubscribed ? "true" : "false", flushed ? "true" : "false",
      ever_subscribed ? "true" : "false", complete ? "true" : "false");
  if (!complete) StderrError("invalid_ledger", static_cast<int>(errors.load()));
  if (log_fd >= 0) {
    if (fsync(log_fd) != 0) {
      const int saved = errno;
      // Preserve an explicit trailing failure so a previously written summary
      // can never silently qualify a failed final write barrier.
      Write(true,
            "{\"event\":\"error\",\"operation\":\"final_fsync\",\"code\":%d}\n",
            saved);
      StderrError("final_fsync", saved);
    }
    if (close(log_fd) != 0) StderrError("log_close", errno);
    log_fd = -1;
  }
  closed = true;
}

struct MissingStop {
  ~MissingStop() {
    if (start_attempted && !closed) {
      Error("missing_profiler_stop", 1);
      StderrError("missing_profiler_stop", 1);
      if (log_fd >= 0) {
        Write(true,
              "{\"event\":\"error\",\"operation\":\"missing_profiler_stop\","
              "\"code\":1}\n");
        close(log_fd);
        log_fd = -1;
      }
    }
    // Deliberately no CUDA/CUPTI calls from the process-exit path.
  }
} missing_stop;
}  // namespace

extern "C" __attribute__((visibility("default"))) cudaError_t CUDARTAPI
cudaProfilerStart() {
  std::lock_guard<std::mutex> lock(boundary_mutex);
  if (!original_start && !Resolve()) {
    StderrError("original_profiler_symbol", 1);
    return cudaErrorUnknown;
  }
  ++start_calls;
  if (start_attempted) {
    Error("repeated_profiler_start", 1);
    StderrError("repeated_profiler_start", 1);
    return original_start();
  }
  start_attempted = true;
  owner_pid = static_cast<uint32_t>(getpid());
  if (OpenLog()) {
    Config();
    Begin();
  } else {
    StderrError("ledger_open_failed", 1);
  }
  const cudaError_t result = original_start();
  start_return = static_cast<int>(result);
  if (result != cudaSuccess) Error("original_start_return", start_return);
  const uint64_t timestamp = log_fd >= 0 ? Timestamp() : 0;
  Write(false,
        "{\"event\":\"boundary\",\"site\":\"start\",\"original_return\":%d,"
        "\"timestamp\":%llu,\"pid\":%u,\"tid\":%u}\n",
        start_return, static_cast<unsigned long long>(timestamp), owner_pid,
        Tid());
  return result;
}

extern "C" __attribute__((visibility("default"))) cudaError_t CUDARTAPI
cudaProfilerStop() {
  std::lock_guard<std::mutex> lock(boundary_mutex);
  if (!original_stop && !Resolve()) {
    StderrError("original_profiler_symbol", 1);
    return cudaErrorUnknown;
  }
  ++stop_calls;
  const cudaError_t result = original_stop();
  if (!start_attempted || stop_attempted ||
      owner_pid != static_cast<uint32_t>(getpid())) {
    Error("missing_repeated_or_foreign_profiler_stop", 1);
    StderrError("missing_repeated_or_foreign_profiler_stop", 1);
    return result;
  }
  stop_attempted = true;
  stop_return = static_cast<int>(result);
  if (result != cudaSuccess) Error("original_stop_return", stop_return);
  const uint64_t timestamp = log_fd >= 0 ? Timestamp() : 0;
  End();
  Finish(timestamp);
  return result;
}
