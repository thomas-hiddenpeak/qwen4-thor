// Diagnostic LD_PRELOAD observer of existing layer-timing reports. No CUDA
// calls or model edits. The first report establishes a baseline; intervals
// thereafter include observer/report overhead and are not GPU busy time.
#include <dlfcn.h>
#include <linux/perf_event.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

namespace {
uint64_t Clock(clockid_t id) {
  timespec t{};
  if (clock_gettime(id, &t) != 0) std::abort();
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}

struct Counters {
  int fd[3] = {-1, -1, -1};
  int error = 0;
  uint64_t sequence = 0;
  Counters() {
    const uint64_t configs[] = {PERF_COUNT_HW_CPU_CYCLES,
                               PERF_COUNT_HW_INSTRUCTIONS,
                               PERF_COUNT_HW_BRANCH_MISSES};
    for (int i = 0; i < 3; ++i) {
      perf_event_attr attr{};
      attr.size = sizeof(attr);
      attr.type = PERF_TYPE_HARDWARE;
      attr.config = configs[i];
      attr.exclude_kernel = 1;
      attr.exclude_hv = 1;
      attr.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED |
                         PERF_FORMAT_TOTAL_TIME_RUNNING;
      fd[i] = int(syscall(SYS_perf_event_open, &attr, 0, -1,
                          i == 0 ? -1 : fd[0], PERF_FLAG_FD_CLOEXEC));
      if (fd[i] < 0) {
        error = errno;
        break;
      }
    }
  }
  ~Counters() {
    for (int f : fd) {
      if (f >= 0) close(f);
    }
  }
};

void Observe(int tokens) {
  const uint64_t before_init = Clock(CLOCK_MONOTONIC);
  thread_local Counters counters;
  const uint64_t begin = Clock(CLOCK_MONOTONIC);
  const long tid = syscall(SYS_gettid);
  uint64_t values[6]{};  // nr, enabled, running, cycles, instructions, misses.
  int pmu_error = counters.error;
  if (!pmu_error) {
    const ssize_t bytes = read(counters.fd[0], values, sizeof(values));
    if (bytes != sizeof(values) || values[0] != 3) {
      pmu_error = bytes < 0 ? errno : EIO;
    }
  }
  const uint64_t cpu = Clock(CLOCK_THREAD_CPUTIME_ID);
  rusage usage{};
  const int usage_rc = getrusage(RUSAGE_THREAD, &usage);
  char path[128];
  std::snprintf(path, sizeof(path), "/proc/self/task/%ld/schedstat", tid);
  unsigned long long scheduled = 0, waiting = 0, slices = 0;
  int sched_error = 0;
  FILE* f = std::fopen(path, "r");
  if (!f) {
    sched_error = errno;
  } else {
    if (std::fscanf(f, "%llu %llu %llu", &scheduled, &waiting, &slices) != 3) {
      sched_error = EIO;
    }
    std::fclose(f);
  }
  const uint64_t end = Clock(CLOCK_MONOTONIC);
  // No claim that schedstat waiting is enabled merely because its file exists.
  dprintf(STDERR_FILENO,
          "[host-interval] tid=%ld seq=%llu T=%d wall_ns=%llu cpu_ns=%llu "
          "sched_ns=%llu runqueue_ns=%llu slices=%llu "
          "voluntary=%ld involuntary=%ld minflt=%ld majflt=%ld "
          "pmu_error=%d pmu_nr=%llu enabled_ns=%llu running_ns=%llu "
          "cycles=%llu instructions=%llu branch_misses=%llu "
          "sched_error=%d usage_rc=%d init_ns=%llu capture_ns=%llu\n",
          tid, static_cast<unsigned long long>(counters.sequence++), tokens,
          static_cast<unsigned long long>(begin),
          static_cast<unsigned long long>(cpu), scheduled, waiting, slices,
          usage.ru_nvcsw, usage.ru_nivcsw, usage.ru_minflt, usage.ru_majflt,
          pmu_error, static_cast<unsigned long long>(values[0]),
          static_cast<unsigned long long>(values[1]),
          static_cast<unsigned long long>(values[2]),
          static_cast<unsigned long long>(values[3]),
          static_cast<unsigned long long>(values[4]),
          static_cast<unsigned long long>(values[5]), sched_error, usage_rc,
          static_cast<unsigned long long>(begin - before_init),
          static_cast<unsigned long long>(end - begin));
}
}  // namespace

extern "C" int __fprintf_chk(FILE* stream, int flag, const char* format, ...) {
  using Fn = int (*)(FILE*, int, const char*, va_list);
  static Fn real = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, "__vfprintf_chk"));
  if (!real) std::abort();
  va_list args;
  va_start(args, format);
  constexpr char prefix[] = "[layer-timing] T=";
  if (std::strncmp(format, prefix, sizeof(prefix) - 1) == 0) {
    va_list copy;
    va_copy(copy, args);
    const int tokens = va_arg(copy, int);
    va_end(copy);
    if (tokens > 1) Observe(tokens);
  }
  const int result = real(stream, flag, format, args);
  va_end(args);
  return result;
}
