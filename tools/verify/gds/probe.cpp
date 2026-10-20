// A single read-only cuFile availability/byte-correctness probe, not a
// benchmark. Every invocation (including argument errors) belongs inside the
// GPU lease.
#include <cuda.h>
#include <cuda_runtime_api.h>
#include <cufile.h>

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {

using Fields = std::vector<std::pair<std::string, std::string>>;
using Clock = std::chrono::steady_clock;
constexpr uint64_t kMaxBytes = 8 * 1024 * 1024;
constexpr uint64_t kAlignment = 4096;
volatile sig_atomic_t g_signal = 0;

void OnSignal(int signal) { g_signal = signal; }

int64_t MonoNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             Clock::now().time_since_epoch())
      .count();
}

int64_t ElapsedUs(Clock::time_point begin) {
  return std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() -
                                                               begin)
      .count();
}

std::string Quote(std::string_view value) {
  constexpr char kHex[] = "0123456789abcdef";
  std::string out = "\"";
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') {
      out += '\\';
      out += static_cast<char>(c);
    } else if (c < 32 || c >= 127) {
      out += "\\u00";
      out += kHex[c >> 4];
      out += kHex[c & 15];
    } else {
      out += static_cast<char>(c);
    }
  }
  return out + '"';
}

std::string Object(const Fields& fields) {
  std::string out = "{";
  for (const auto& [key, value] : fields) {
    if (out.size() != 1) out += ',';
    out += Quote(key) + ':' + value;
  }
  return out + '}';
}

std::string Array(const std::vector<std::string>& values) {
  std::string out = "[";
  for (const auto& value : values) {
    if (out.size() != 1) out += ',';
    out += value;
  }
  return out + ']';
}

std::string Bool(bool value) { return value ? "true" : "false"; }

std::string Hex(const char* bytes, size_t count) {
  constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  for (size_t i = 0; i < count; ++i) {
    const auto c = static_cast<unsigned char>(bytes[i]);
    out += kHex[c >> 4];
    out += kHex[c & 15];
  }
  return out;
}

class Reporter {
 public:
  void Emit(std::string_view event, Fields fields = {}) {
    fields.emplace_back("event", Quote(event));
    fields.emplace_back("seq", std::to_string(++seq_));
    fields.emplace_back("pid", std::to_string(getpid()));
    fields.emplace_back("mono_ns", std::to_string(MonoNs()));
    std::cout << Object(fields) << '\n' << std::flush;
    if (!std::cout) failed_ = true;
  }

  bool Cuda(std::string_view name, cudaError_t rc, Clock::time_point begin,
            std::string_view phase = "work") {
    Emit("api", {{"name", Quote(name)},
                 {"phase", Quote(phase)},
                 {"rc", std::to_string(rc)},
                 {"elapsed_us", std::to_string(ElapsedUs(begin))},
                 {"error_name", Quote(cudaGetErrorName(rc))},
                 {"error", Quote(cudaGetErrorString(rc))}});
    return rc == cudaSuccess;
  }

  bool Driver(std::string_view name, CUresult rc, Clock::time_point begin,
              std::string_view phase = "work") {
    const char* error_name = nullptr;
    const char* error = nullptr;
    const CUresult name_rc = cuGetErrorName(rc, &error_name);
    const CUresult string_rc = cuGetErrorString(rc, &error);
    Emit("api",
         {{"name", Quote(name)},
          {"phase", Quote(phase)},
          {"rc", std::to_string(rc)},
          {"elapsed_us", std::to_string(ElapsedUs(begin))},
          {"error_name",
           name_rc == CUDA_SUCCESS && error_name ? Quote(error_name) : "null"},
          {"error",
           string_rc == CUDA_SUCCESS && error ? Quote(error) : "null"}});
    return rc == CUDA_SUCCESS;
  }

  bool File(std::string_view name, CUfileError_t rc, Clock::time_point begin,
            std::string_view phase = "work") {
    Emit("api",
         {{"name", Quote(name)},
          {"phase", Quote(phase)},
          {"rc", std::to_string(rc.err)},
          {"cuda_rc", IS_CUDA_ERR(rc) ? std::to_string(rc.cu_err) : "null"},
          {"elapsed_us", std::to_string(ElapsedUs(begin))},
          {"error", Quote(CUFILE_ERRSTR(rc.err))}});
    return rc.err == CU_FILE_SUCCESS;
  }

  bool Posix(std::string_view name, int64_t rc, int error,
             Clock::time_point begin, std::string_view phase = "work") {
    Emit("api", {{"name", Quote(name)},
                 {"phase", Quote(phase)},
                 {"rc", std::to_string(rc)},
                 {"errno", std::to_string(error)},
                 {"error", error ? Quote(std::strerror(error)) : "null"},
                 {"elapsed_us", std::to_string(ElapsedUs(begin))}});
    return rc >= 0;
  }

  bool Failed() const { return failed_; }

 private:
  uint64_t seq_ = 0;
  bool failed_ = false;
};

struct Options {
  std::string path;
  uint64_t offset = 0;
  uint64_t bytes = 0;
  uint64_t hold_seconds = 0;
  bool register_buffer = false;
};

uint64_t Number(std::string_view text) {
  uint64_t out = 0;
  auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), out);
  if (text.empty() || error != std::errc() ||
      end != text.data() + text.size()) {
    throw std::runtime_error("expected an unsigned decimal integer");
  }
  return out;
}

Options Parse(int argc, char** argv) {
  Options out;
  std::set<std::string> seen;
  for (int i = 1; i < argc; ++i) {
    const std::string key(argv[i]);
    if (!seen.insert(key).second) {
      throw std::runtime_error("duplicate argument: " + key);
    }
    if (key == "--register-buffer") {
      out.register_buffer = true;
      continue;
    }
    if (key != "--path" && key != "--offset" && key != "--bytes" &&
        key != "--hold-seconds") {
      throw std::runtime_error("unknown argument: " + key);
    }
    if (++i == argc) throw std::runtime_error("missing argument: " + key);
    const std::string_view value(argv[i]);
    if (key == "--path") out.path = value;
    if (key == "--offset") out.offset = Number(value);
    if (key == "--bytes") out.bytes = Number(value);
    if (key == "--hold-seconds") out.hold_seconds = Number(value);
  }
  if (!seen.contains("--path") || !seen.contains("--offset") ||
      !seen.contains("--bytes") || out.path.empty() || out.bytes == 0 ||
      out.bytes > kMaxBytes || out.hold_seconds > 3 ||
      out.offset > static_cast<uint64_t>(std::numeric_limits<off_t>::max()) -
                       out.bytes) {
    throw std::runtime_error(
        "require path, offset, 1..8388608 bytes, "
        "hold 0..3 seconds, and a valid off_t interval");
  }
  return out;
}

void Require(bool value, std::string_view message) {
  if (!value) throw std::runtime_error(std::string(message));
}

void CheckContinue(const Reporter& report) {
  Require(g_signal == 0, "interrupted");
  Require(!report.Failed(), "stdout logging failed");
}

struct IoSnapshot {
  bool valid = false;
  uint64_t read_bytes = 0;
  uint64_t rchar = 0;
  int64_t mono_ns = 0;
};

IoSnapshot ReadIo() {
  IoSnapshot out;
  std::ifstream input("/proc/self/io");
  std::string key;
  uint64_t value = 0;
  bool have_read = false;
  bool have_rchar = false;
  while (input >> key >> value) {
    if (key == "read_bytes:") {
      out.read_bytes = value;
      have_read = true;
    }
    if (key == "rchar:") {
      out.rchar = value;
      have_rchar = true;
    }
  }
  out.mono_ns = MonoNs();
  out.valid = have_read && have_rchar && !input.bad();
  return out;
}

std::string IoJson(const IoSnapshot& value) {
  return Object(
      {{"known", Bool(value.valid)},
       {"mono_ns", std::to_string(value.mono_ns)},
       {"read_bytes", value.valid ? std::to_string(value.read_bytes) : "null"},
       {"rchar", value.valid ? std::to_string(value.rchar) : "null"}});
}

std::string Counter(const CUfileOpCounter_t& value) {
  return Object(
      {{"ok", std::to_string(value.ok)}, {"err", std::to_string(value.err)}});
}

void Stats(Reporter& report, std::string_view point, bool collection_ready) {
  CUfileStatsLevel3_t stats{};
  auto begin = Clock::now();
  const bool ok = report.File("cuFileGetStatsL3", cuFileGetStatsL3(&stats),
                              begin, "diagnostic");
  const bool valid = collection_ready && ok &&
                     stats.num_gpus <= std::size(stats.per_gpu_stats);
  if (!valid) {
    report.Emit(
        "stats",
        {{"point", Quote(point)},
         {"known", "false"},
         {"value", "null"},
         {"reason", Quote(!collection_ready ? "level 3 collection not confirmed"
                          : ok              ? "num_gpus out of bounds"
                                            : "getter failed")}});
    return;
  }
  std::vector<std::string> gpus;
  for (uint32_t i = 0; i < stats.num_gpus; ++i) {
    const auto& gpu = stats.per_gpu_stats[i];
    gpus.push_back(
        Object({{"uuid_hex", Quote(Hex(gpu.uuid, sizeof(gpu.uuid)))},
                {"read_bytes", std::to_string(gpu.read_bytes)},
                {"read_duration_us", std::to_string(gpu.read_duration_us)},
                {"n_total_reads", std::to_string(gpu.n_total_reads)},
                {"n_p2p_reads", std::to_string(gpu.n_p2p_reads)},
                {"n_nvfs_reads", std::to_string(gpu.n_nvfs_reads)},
                {"n_posix_reads", std::to_string(gpu.n_posix_reads)},
                {"n_unaligned_reads", std::to_string(gpu.n_unaligned_reads)},
                {"n_dr_reads", std::to_string(gpu.n_dr_reads)},
                {"n_sparse_regions", std::to_string(gpu.n_sparse_regions)},
                {"n_inline_regions", std::to_string(gpu.n_inline_regions)},
                {"n_reads_err", std::to_string(gpu.n_reads_err)},
                {"n_mmap", std::to_string(gpu.n_mmap)},
                {"n_mmap_ok", std::to_string(gpu.n_mmap_ok)},
                {"n_mmap_err", std::to_string(gpu.n_mmap_err)},
                {"n_mmap_free", std::to_string(gpu.n_mmap_free)},
                {"reg_bytes", std::to_string(gpu.reg_bytes)}}));
  }
  const auto& basic = stats.detailed.basic;
  report.Emit(
      "stats",
      {{"point", Quote(point)},
       {"known", "true"},
       {"read_ops", Counter(basic.read_ops)},
       {"read_bytes", std::to_string(basic.read_bytes)},
       {"hdl_register_ops", Counter(basic.hdl_register_ops)},
       {"hdl_deregister_ops", Counter(basic.hdl_deregister_ops)},
       {"buf_register_ops", Counter(basic.buf_register_ops)},
       {"buf_deregister_ops", Counter(basic.buf_deregister_ops)},
       {"num_gpus", std::to_string(stats.num_gpus)},
       {"per_gpu", Array(gpus)},
       {"actual_internal_pool_bytes", "null"},
       {"pool_usage_reason", Quote("not exposed by CUfileStatsLevel3_t")}});
}

void Configuration(Reporter& report) {
  CUfileDrvProps_t props{};
  auto begin = Clock::now();
  const bool ok =
      report.File("cuFileDriverGetProperties",
                  cuFileDriverGetProperties(&props), begin, "diagnostic");
  report.Emit(
      "driver_properties",
      {{"known", Bool(ok)},
       {"value",
        ok ? Object({{"nvfs_major_version",
                      std::to_string(props.nvfs.major_version)},
                     {"nvfs_minor_version",
                      std::to_string(props.nvfs.minor_version)},
                     {"nvfs_poll_thresh_size",
                      std::to_string(props.nvfs.poll_thresh_size)},
                     {"nvfs_max_direct_io_size",
                      std::to_string(props.nvfs.max_direct_io_size)},
                     {"nvfs_dstatusflags",
                      std::to_string(props.nvfs.dstatusflags)},
                     {"nvfs_dcontrolflags",
                      std::to_string(props.nvfs.dcontrolflags)},
                     {"fflags", std::to_string(props.fflags)},
                     {"max_device_cache_size",
                      std::to_string(props.max_device_cache_size)},
                     {"per_buffer_cache_size",
                      std::to_string(props.per_buffer_cache_size)},
                     {"max_device_pinned_mem_size",
                      std::to_string(props.max_device_pinned_mem_size)}})
           : "null"}});
  CUfileP2PFlags_t flags{};
  begin = Clock::now();
  const bool flags_ok =
      report.File("cuFileDriverGetP2PFlags(NVME)",
                  cuFileDriverGetP2PFlags(CU_FILE_NVME_SUPPORTED, &flags),
                  begin, "diagnostic");
  report.Emit(
      "p2p_flags",
      {{"known", Bool(flags_ok)},
       {"flags", flags_ok ? std::to_string(flags) : "null"},
       {"scope",
        Quote("NVME capability/configuration, not observed read path")}});
  constexpr std::pair<const char*, CUFileBoolConfigParameter_t> kBools[] = {
      {"allow_compat_mode", CUFILE_PARAM_PROPERTIES_ALLOW_COMPAT_MODE},
      {"force_compat_mode", CUFILE_PARAM_FORCE_COMPAT_MODE},
      {"allow_system_memory", CUFILE_PARAM_PROPERTIES_ALLOW_SYSTEM_MEMORY},
      {"use_pcip2pdma", CUFILE_PARAM_USE_PCIP2PDMA},
      {"prefer_io_uring", CUFILE_PARAM_PREFER_IO_URING},
      {"force_odirect_mode", CUFILE_PARAM_FORCE_ODIRECT_MODE}};
  for (const auto& [name, param] : kBools) {
    bool value = false;
    begin = Clock::now();
    const bool known =
        report.File(std::string("cuFileGetParameterBool:") + name,
                    cuFileGetParameterBool(param, &value), begin, "diagnostic");
    report.Emit("configuration", {{"name", Quote(name)},
                                  {"known", Bool(known)},
                                  {"value", known ? Bool(value) : "null"}});
  }
  constexpr std::pair<const char*, CUFileSizeTConfigParameter_t> kSizes[] = {
      {"max_direct_io_size_kb", CUFILE_PARAM_PROPERTIES_MAX_DIRECT_IO_SIZE_KB},
      {"max_device_cache_size_kb",
       CUFILE_PARAM_PROPERTIES_MAX_DEVICE_CACHE_SIZE_KB},
      {"per_buffer_cache_size_kb",
       CUFILE_PARAM_PROPERTIES_PER_BUFFER_CACHE_SIZE_KB},
      {"max_device_pinned_mem_size_kb",
       CUFILE_PARAM_PROPERTIES_MAX_DEVICE_PINNED_MEM_SIZE_KB}};
  for (const auto& [name, param] : kSizes) {
    size_t value = 0;
    begin = Clock::now();
    const bool known = report.File(
        std::string("cuFileGetParameterSizeT:") + name,
        cuFileGetParameterSizeT(param, &value), begin, "diagnostic");
    report.Emit(
        "configuration",
        {{"name", Quote(name)},
         {"known", Bool(known)},
         {"value", known ? std::to_string(value) : "null"},
         {"scope", Quote("configured limit, not measured allocated RAM")}});
  }
}

bool SameFile(const struct stat& a, const struct stat& b) {
  return a.st_dev == b.st_dev && a.st_ino == b.st_ino &&
         a.st_size == b.st_size && a.st_mtim.tv_sec == b.st_mtim.tv_sec &&
         a.st_mtim.tv_nsec == b.st_mtim.tv_nsec &&
         a.st_ctim.tv_sec == b.st_ctim.tv_sec &&
         a.st_ctim.tv_nsec == b.st_ctim.tv_nsec;
}

struct Resources {
  int direct_fd = -1;
  int truth_fd = -1;
  void* buffer = nullptr;
  CUfileHandle_t handle = nullptr;
  bool runtime_open = false;
  bool driver_open = false;
  bool handle_registered = false;
  bool buffer_registered = false;
  bool stats_started = false;

  bool Cleanup(Reporter& report) {
    bool ok = true;
    auto begin = Clock::now();
    if (stats_started) {
      // A diagnostic failure does not invalidate data or resource release.
      report.File("cuFileStatsStop", cuFileStatsStop(), begin, "diagnostic");
      stats_started = false;
    }
    if (buffer_registered) {
      begin = Clock::now();
      ok &= report.File("cuFileBufDeregister", cuFileBufDeregister(buffer),
                        begin, "cleanup");
      buffer_registered = false;
    }
    if (handle_registered) {
      begin = Clock::now();
      cuFileHandleDeregister(handle);
      report.Emit("api", {{"name", Quote("cuFileHandleDeregister")},
                          {"phase", Quote("cleanup")},
                          {"rc", "null"},
                          {"returned", "true"},
                          {"note", Quote("void API; no status available")},
                          {"elapsed_us", std::to_string(ElapsedUs(begin))}});
      handle_registered = false;
    }
    if (driver_open) {
      begin = Clock::now();
      ok &= report.File("cuFileDriverClose", cuFileDriverClose(), begin,
                        "cleanup");
      driver_open = false;
    }
    if (buffer) {
      begin = Clock::now();
      ok &= report.Cuda("cudaFree", cudaFree(buffer), begin, "cleanup");
      buffer = nullptr;
    }
    if (runtime_open) {
      begin = Clock::now();
      ok &= report.Cuda("cudaDeviceReset", cudaDeviceReset(), begin, "cleanup");
      runtime_open = false;
    }
    for (auto* fd : {&truth_fd, &direct_fd}) {
      if (*fd < 0) continue;
      begin = Clock::now();
      const int rc = close(*fd);
      const int error = rc < 0 ? errno : 0;
      ok &= report.Posix("close", rc, error, begin, "cleanup");
      *fd = -1;  // Linux close must not be retried after an error.
    }
    return ok;
  }

  ~Resources() {
    // Only a fallback if an exception interrupted explicit, reported cleanup.
    if (buffer_registered) cuFileBufDeregister(buffer);
    if (handle_registered) cuFileHandleDeregister(handle);
    if (driver_open) cuFileDriverClose();
    if (buffer) cudaFree(buffer);
    if (runtime_open) cudaDeviceReset();
    if (truth_fd >= 0) close(truth_fd);
    if (direct_fd >= 0) close(direct_fd);
  }
};

void DeviceInfo(Reporter& report) {
  CUdevice device{};
  auto begin = Clock::now();
  Require(report.Driver("cuDeviceGet", cuDeviceGet(&device, 0), begin),
          "cuDeviceGet failed");
  char name[256]{};
  begin = Clock::now();
  const bool name_ok = report.Driver(
      "cuDeviceGetName", cuDeviceGetName(name, sizeof(name), device), begin,
      "diagnostic");
  CUuuid uuid{};
  begin = Clock::now();
  const bool uuid_ok = report.Driver(
      "cuDeviceGetUuid", cuDeviceGetUuid(&uuid, device), begin, "diagnostic");
  report.Emit("device",
              {{"ordinal", "0"},
               {"name", name_ok ? Quote(std::string_view(
                                      name, strnlen(name, sizeof(name))))
                                : "null"},
               {"uuid_hex",
                uuid_ok ? Quote(Hex(uuid.bytes, sizeof(uuid.bytes))) : "null"},
               {"cuda_header_version", std::to_string(CUDA_VERSION)}});
  constexpr std::pair<const char*, CUdevice_attribute> kAttributes[] = {
      {"DMA_BUF_SUPPORTED", CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED},
      {"HOST_ALLOC_DMA_BUF_SUPPORTED",
       CU_DEVICE_ATTRIBUTE_HOST_ALLOC_DMA_BUF_SUPPORTED},
      {"INTEGRATED", CU_DEVICE_ATTRIBUTE_INTEGRATED},
      {"COMPUTE_CAPABILITY_MAJOR",
       CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR},
      {"COMPUTE_CAPABILITY_MINOR",
       CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR}};
  for (const auto& [attr_name, attr] : kAttributes) {
    int value = 0;
    begin = Clock::now();
    const bool known = report.Driver(
        std::string("cuDeviceGetAttribute:") + attr_name,
        cuDeviceGetAttribute(&value, attr, device), begin, "diagnostic");
    report.Emit("device_attribute",
                {{"name", Quote(attr_name)},
                 {"known", Bool(known)},
                 {"value", known ? std::to_string(value) : "null"}});
  }
  int version = 0;
  begin = Clock::now();
  bool known = report.Driver("cuDriverGetVersion", cuDriverGetVersion(&version),
                             begin, "diagnostic");
  report.Emit("version", {{"name", Quote("CUDA driver")},
                          {"value", known ? std::to_string(version) : "null"}});
  begin = Clock::now();
  known = report.Cuda("cudaRuntimeGetVersion", cudaRuntimeGetVersion(&version),
                      begin, "diagnostic");
  report.Emit("version", {{"name", Quote("CUDA runtime")},
                          {"value", known ? std::to_string(version) : "null"}});
  begin = Clock::now();
  known = report.File("cuFileGetVersion", cuFileGetVersion(&version), begin,
                      "diagnostic");
  report.Emit("version", {{"name", Quote("cuFile")},
                          {"value", known ? std::to_string(version) : "null"}});
}

int Run(int argc, char** argv) {
  Reporter report;
  Resources owned;
  Options options;
  bool matched = false;
  bool unchanged = false;
  bool read_attempted = false;
  ssize_t read_result = -1;
  std::string failure;
  try {
    options = Parse(argc, argv);
    const size_t allocation =
        (options.bytes + kAlignment - 1) / kAlignment * kAlignment;
    // These are ordinary host buffers, never cudaHostAlloc/managed allocations.
    std::vector<unsigned char> actual(options.bytes);
    std::vector<unsigned char> expected(options.bytes);
    report.Emit(
        "start",
        {{"path", Quote(options.path)},
         {"offset", std::to_string(options.offset)},
         {"bytes", std::to_string(options.bytes)},
         {"register_buffer", Bool(options.register_buffer)},
         {"device_allocation_bytes", std::to_string(allocation)},
         {"application_host_buffer_bytes", std::to_string(options.bytes * 2)},
         {"hold_seconds", std::to_string(options.hold_seconds)},
         {"offset_mod_4096", std::to_string(options.offset % kAlignment)},
         {"bytes_mod_4096", std::to_string(options.bytes % kAlignment)},
         {"native_path", Quote("UNPROVEN")}});
    auto begin = Clock::now();
    owned.direct_fd =
        open(options.path.c_str(), O_RDONLY | O_DIRECT | O_CLOEXEC);
    int error = owned.direct_fd < 0 ? errno : 0;
    Require(
        report.Posix("open(O_RDONLY|O_DIRECT)", owned.direct_fd, error, begin),
        "direct open failed");
    begin = Clock::now();
    owned.truth_fd = open(options.path.c_str(), O_RDONLY | O_CLOEXEC);
    error = owned.truth_fd < 0 ? errno : 0;
    Require(report.Posix("open(O_RDONLY truth)", owned.truth_fd, error, begin),
            "truth open failed");
    struct stat initial {};
    struct stat truth_stat {};
    begin = Clock::now();
    int rc = fstat(owned.direct_fd, &initial);
    error = rc < 0 ? errno : 0;
    Require(report.Posix("fstat(direct)", rc, error, begin), "fstat failed");
    begin = Clock::now();
    rc = fstat(owned.truth_fd, &truth_stat);
    error = rc < 0 ? errno : 0;
    Require(report.Posix("fstat(truth)", rc, error, begin), "fstat failed");
    Require(S_ISREG(initial.st_mode) && SameFile(initial, truth_stat) &&
                initial.st_size >= 0 &&
                options.offset + options.bytes <=
                    static_cast<uint64_t>(initial.st_size),
            "input must be the same regular file with the entire interval");
    report.Emit("file_identity",
                {{"device", std::to_string(initial.st_dev)},
                 {"inode", std::to_string(initial.st_ino)},
                 {"size", std::to_string(initial.st_size)},
                 {"mtime_sec", std::to_string(initial.st_mtim.tv_sec)},
                 {"mtime_nsec", std::to_string(initial.st_mtim.tv_nsec)},
                 {"ctime_sec", std::to_string(initial.st_ctim.tv_sec)},
                 {"ctime_nsec", std::to_string(initial.st_ctim.tv_nsec)}});
    CheckContinue(report);
    begin = Clock::now();
    Require(report.Driver("cuInit", cuInit(0), begin), "cuInit failed");
    DeviceInfo(report);
    begin = Clock::now();
    owned.runtime_open = report.Cuda("cudaSetDevice", cudaSetDevice(0), begin);
    Require(owned.runtime_open, "cudaSetDevice failed");
    begin = Clock::now();
    Require(
        report.Cuda("cudaMalloc", cudaMalloc(&owned.buffer, allocation), begin),
        "cudaMalloc failed");
    report.Emit("buffer",
                {{"bytes", std::to_string(allocation)},
                 {"address_mod_4096",
                  std::to_string(reinterpret_cast<uintptr_t>(owned.buffer) %
                                 kAlignment)}});
    CheckContinue(report);
    begin = Clock::now();
    owned.driver_open =
        report.File("cuFileDriverOpen", cuFileDriverOpen(), begin);
    Require(owned.driver_open, "cuFileDriverOpen failed");
    Configuration(report);
    begin = Clock::now();
    const bool stats_level_ok = report.File(
        "cuFileSetStatsLevel(3)", cuFileSetStatsLevel(3), begin, "diagnostic");
    if (stats_level_ok) {
      begin = Clock::now();
      owned.stats_started = report.File("cuFileStatsStart", cuFileStatsStart(),
                                        begin, "diagnostic");
    }
    int stats_level = 0;
    begin = Clock::now();
    const bool level_known =
        report.File("cuFileGetStatsLevel", cuFileGetStatsLevel(&stats_level),
                    begin, "diagnostic");
    report.Emit(
        "stats_collection",
        {{"started", Bool(owned.stats_started)},
         {"level", level_known ? std::to_string(stats_level) : "null"}});
    CUfileDescr_t description{};
    description.type = CU_FILE_HANDLE_TYPE_OPAQUE_FD;
    description.handle.fd = owned.direct_fd;
    begin = Clock::now();
    owned.handle_registered =
        report.File("cuFileHandleRegister",
                    cuFileHandleRegister(&owned.handle, &description), begin);
    Require(owned.handle_registered, "cuFileHandleRegister failed");
    if (options.register_buffer) {
      begin = Clock::now();
      owned.buffer_registered =
          report.File("cuFileBufRegister",
                      cuFileBufRegister(owned.buffer, allocation, 0), begin);
      Require(owned.buffer_registered, "cuFileBufRegister failed; no fallback");
    }
    const bool collection_ready = stats_level_ok && owned.stats_started &&
                                  level_known && stats_level == 3;
    Stats(report, "before_read", collection_ready);
    CheckContinue(report);
    report.Emit("before_read", {{"offset", std::to_string(options.offset)},
                                {"bytes", std::to_string(options.bytes)},
                                {"attempt", "1"}});
    CheckContinue(report);
    const IoSnapshot io_before = ReadIo();
    begin = Clock::now();
    errno = 0;
    read_attempted = true;
    read_result = cuFileRead(owned.handle, owned.buffer, options.bytes,
                             static_cast<off_t>(options.offset), 0);
    const int read_errno = errno;
    const int64_t read_us = ElapsedUs(begin);
    const IoSnapshot io_after = ReadIo();
    report.Emit(
        "api", {{"name", Quote("cuFileRead")},
                {"phase", Quote("work")},
                {"returned_bytes", std::to_string(read_result)},
                {"elapsed_us", std::to_string(read_us)},
                {"errno_raw", std::to_string(read_errno)},
                {"error", read_result == -1  ? Quote(std::strerror(read_errno))
                          : read_result < -1 ? Quote(CUFILE_ERRSTR(read_result))
                                             : "null"}});
    const bool io_known = io_before.valid && io_after.valid &&
                          io_after.read_bytes >= io_before.read_bytes &&
                          io_after.rchar >= io_before.rchar;
    report.Emit(
        "process_io",
        {{"before", IoJson(io_before)},
         {"after", IoJson(io_after)},
         {"delta_known", Bool(io_known)},
         {"read_bytes_delta",
          io_known ? std::to_string(io_after.read_bytes - io_before.read_bytes)
                   : "null"},
         {"rchar_delta",
          io_known ? std::to_string(io_after.rchar - io_before.rchar) : "null"},
         {"scope",
          Quote("process cuFileRead window; includes observer/library "
                "I/O, excludes later truth pread; not SSD attribution")}});
    Stats(report, "after_read", collection_ready);
    Require(read_result == static_cast<ssize_t>(options.bytes),
            "cuFileRead error or short read; no retry");
    CheckContinue(report);
    begin = Clock::now();
    Require(report.Cuda("cudaMemcpy(D2H)",
                        cudaMemcpy(actual.data(), owned.buffer, options.bytes,
                                   cudaMemcpyDeviceToHost),
                        begin),
            "D2H failed");
    begin = Clock::now();
    const ssize_t truth_result =
        pread(owned.truth_fd, expected.data(), options.bytes,
              static_cast<off_t>(options.offset));
    error = truth_result < 0 ? errno : 0;
    report.Posix("pread(truth after cuFileRead)", truth_result, error, begin);
    Require(truth_result == static_cast<ssize_t>(options.bytes),
            "truth pread error or short read; no retry");
    begin = Clock::now();
    uint64_t mismatch_count = 0;
    uint64_t first_mismatch = 0;
    for (uint64_t i = 0; i < options.bytes; ++i) {
      if (actual[i] == expected[i]) continue;
      if (mismatch_count == 0) first_mismatch = i;
      ++mismatch_count;
    }
    matched = mismatch_count == 0;
    report.Emit("compare", {{"matched", Bool(matched)},
                            {"compared_bytes", std::to_string(options.bytes)},
                            {"mismatch_count", std::to_string(mismatch_count)},
                            {"first_mismatch",
                             matched ? "null" : std::to_string(first_mismatch)},
                            {"elapsed_us", std::to_string(ElapsedUs(begin))}});
    struct stat after {};
    begin = Clock::now();
    rc = fstat(owned.direct_fd, &after);
    error = rc < 0 ? errno : 0;
    Require(report.Posix("fstat(after read)", rc, error, begin),
            "fstat failed");
    unchanged = SameFile(initial, after);
    Require(matched && unchanged, "byte mismatch or input metadata changed");
    CheckContinue(report);
    report.Emit("ready",
                {{"data_match", "true"},
                 {"buffers_alive", "true"},
                 {"native_path", Quote("UNPROVEN")},
                 {"hold_seconds", std::to_string(options.hold_seconds)}});
    CheckContinue(report);
    const auto deadline =
        Clock::now() + std::chrono::seconds(options.hold_seconds);
    while (Clock::now() < deadline) {
      const auto remaining =
          std::chrono::duration_cast<std::chrono::nanoseconds>(deadline -
                                                               Clock::now())
              .count();
      if (remaining <= 0) break;
      const timespec delay{remaining / 1000000000, remaining % 1000000000};
      if (nanosleep(&delay, nullptr) != 0 && errno != EINTR) {
        throw std::runtime_error("hold nanosleep failed");
      }
      CheckContinue(report);
    }
  } catch (const std::exception& error) {
    failure = error.what();
    report.Emit("failure", {{"error", Quote(failure)}});
  }
  const bool cleanup_ok = owned.Cleanup(report);
  const bool success = failure.empty() && matched && unchanged && cleanup_ok &&
                       !report.Failed() && g_signal == 0;
  report.Emit("result",
              {{"status", Quote(success ? "PASS" : "FAILED")},
               {"failure", failure.empty() ? "null" : Quote(failure)},
               {"read_attempted", Bool(read_attempted)},
               {"returned_bytes",
                read_attempted ? std::to_string(read_result) : "null"},
               {"requested_bytes", std::to_string(options.bytes)},
               {"data_match", Bool(matched)},
               {"file_unchanged", Bool(unchanged)},
               {"cleanup_ok", Bool(cleanup_ok)},
               {"logging_ok", Bool(!report.Failed())},
               {"signal", std::to_string(g_signal)},
               {"native_path", Quote("UNPROVEN")},
               {"performance_accepted", "false"},
               {"actual_internal_pool_bytes", "null"}});
  return success && !report.Failed() ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  struct sigaction action {};
  action.sa_handler = OnSignal;
  sigemptyset(&action.sa_mask);
  sigaction(SIGINT, &action, nullptr);
  sigaction(SIGTERM, &action, nullptr);
  signal(SIGPIPE, SIG_IGN);
  try {
    return Run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "probe exception during reporting/cleanup: " << error.what()
              << '\n';
    return 2;
  }
}
