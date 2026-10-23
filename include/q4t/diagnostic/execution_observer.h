#pragma once

// Bounded, opt-in host spans. No CUDA events or stream drains are introduced.
// Wall - thread CPU is non-running wall, not a measurement of GPU execution.
#include <cuda_profiler_api.h>
#include <nvtx3/nvToolsExt.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>

namespace q4t::diagnostic {

inline bool EnvOne(const char* name) {
  const char* value = std::getenv(name);
  return value != nullptr && std::strcmp(value, "1") == 0;
}

inline bool Enabled() {
  static const bool enabled = EnvOne("Q4T_EXEC_DIAGNOSTIC");
  return enabled;
}

inline bool NvtxEnabled() {
  static const bool enabled = Enabled() && EnvOne("Q4T_EXEC_NVTX");
  return enabled;
}

struct Stamp {
  int64_t wall = 0;
  int64_t cpu = 0;
};

struct Record {
  const char* label = nullptr;
  Stamp begin;
  Stamp end;
  int parent = -1;
  int position = -1;
  int tokens = -1;
  bool closed = false;
  uint64_t value0 = 0;
  uint64_t value1 = 0;
  uint64_t value2 = 0;
};

struct Recorder {
  static constexpr int kCapacity = 4096;
  Record records[kCapacity];
  uint64_t batch_id = 0;
  int count = 0;
  int parent = -1;
  int seq = -1;
  int position = -1;
  int tokens = -1;
  bool overflow = false;
  bool clock_error = false;

  Stamp Now() {
    timespec wall{}, cpu{};
    if (clock_gettime(CLOCK_MONOTONIC, &wall) != 0 ||
        clock_gettime(CLOCK_THREAD_CPUTIME_ID, &cpu) != 0) {
      clock_error = true;
    }
    return {int64_t{wall.tv_sec} * 1000000000 + wall.tv_nsec,
            int64_t{cpu.tv_sec} * 1000000000 + cpu.tv_nsec};
  }
};

inline thread_local std::unique_ptr<Recorder> recorder_storage;
inline thread_local Recorder* active_recorder = nullptr;
inline std::atomic<uint64_t> next_batch_id{1};

// Nested batches share the outer buffer; only its owner flushes. Allocation
// happens once per observed thread, never when diagnostics are disabled.
class Batch {
 public:
  Batch(const char* label, int seq, int position, int tokens) : label_(label) {
    if (!Enabled() || active_recorder != nullptr) return;
    if (!recorder_storage) recorder_storage = std::make_unique<Recorder>();
    recorder_ = recorder_storage.get();
    recorder_->batch_id = next_batch_id.fetch_add(1, std::memory_order_relaxed);
    recorder_->count = 0;
    recorder_->parent = -1;
    recorder_->seq = seq;
    recorder_->position = position;
    recorder_->tokens = tokens;
    recorder_->overflow = false;
    recorder_->clock_error = false;
    active_recorder = recorder_;
  }
  ~Batch() {
    if (!recorder_) return;
    active_recorder = nullptr;
    const Stamp start = recorder_->Now();
    const long tid = syscall(SYS_gettid);
    bool complete = !recorder_->overflow && !recorder_->clock_error &&
                    recorder_->parent == -1;
    for (int i = 0; i < recorder_->count; ++i)
      complete = complete && recorder_->records[i].closed;
    flockfile(stderr);
    std::fprintf(stderr,
        "[exec-batch] pid=%d tid=%ld batch=%llu label=%s seq=%d p=%d t=%d "
        "records=%d capacity=%d bytes=%zu overflow=%d clock_error=%d "
        "complete=%d\n",
        getpid(), tid, static_cast<unsigned long long>(recorder_->batch_id),
        label_, recorder_->seq, recorder_->position, recorder_->tokens,
        recorder_->count, Recorder::kCapacity, sizeof(Recorder),
        recorder_->overflow, recorder_->clock_error, complete);
    for (int i = 0; i < recorder_->count; ++i) {
      const Record& r = recorder_->records[i];
      std::fprintf(stderr,
          "[exec-span] pid=%d tid=%ld batch=%llu id=%d parent=%d phase=%s "
          "seq=%d p=%d t=%d wall_begin_ns=%lld wall_end_ns=%lld "
          "cpu_begin_ns=%lld cpu_end_ns=%lld closed=%d "
          "value0=%llu value1=%llu value2=%llu\n",
          getpid(), tid, static_cast<unsigned long long>(recorder_->batch_id),
          i, r.parent, r.label, recorder_->seq, r.position, r.tokens,
          static_cast<long long>(r.begin.wall),
          static_cast<long long>(r.end.wall),
          static_cast<long long>(r.begin.cpu),
          static_cast<long long>(r.end.cpu), r.closed,
          static_cast<unsigned long long>(r.value0),
          static_cast<unsigned long long>(r.value1),
          static_cast<unsigned long long>(r.value2));
    }
    funlockfile(stderr);
    const Stamp end = recorder_->Now();
    std::fprintf(stderr,
        "[exec-flush] batch=%llu wall_ns=%lld cpu_ns=%lld clock_error=%d\n",
        static_cast<unsigned long long>(recorder_->batch_id),
        static_cast<long long>(end.wall - start.wall),
        static_cast<long long>(end.cpu - start.cpu), recorder_->clock_error);
  }
  Batch(const Batch&) = delete;
  Batch& operator=(const Batch&) = delete;

 private:
  const char* label_;
  Recorder* recorder_ = nullptr;
};

class Scope {
 public:
  Scope(const char* label, int position = -1, int tokens = -1) {
    if (!Enabled() || !active_recorder) return;
    recorder_ = active_recorder;
    if (recorder_->count == Recorder::kCapacity) {
      recorder_->overflow = true;
      recorder_ = nullptr;
      return;
    }
    id_ = recorder_->count++;
    Record& r = recorder_->records[id_];
    r = Record{};
    r.label = label;
    r.parent = recorder_->parent;
    const int parent_position = r.parent < 0 ? recorder_->position
        : recorder_->records[r.parent].position;
    const int parent_tokens = r.parent < 0 ? recorder_->tokens
        : recorder_->records[r.parent].tokens;
    r.position = position < 0 ? parent_position : position;
    r.tokens = tokens < 0 ? parent_tokens : tokens;
    recorder_->parent = id_;
    if (NvtxEnabled()) {
      char name[192];
      std::snprintf(name, sizeof(name),
          "q4t|%llu|%d|%s|seq=%d|p=%d|t=%d",
          static_cast<unsigned long long>(recorder_->batch_id), id_, label,
          recorder_->seq, r.position, r.tokens);
      nvtxRangePushA(name);
    }
    r.begin = recorder_->Now();
  }
  ~Scope() {
    if (!recorder_) return;
    Record& r = recorder_->records[id_];
    r.end = recorder_->Now();
    r.closed = true;
    recorder_->parent = r.parent;
    if (NvtxEnabled()) nvtxRangePop();
  }
  Scope(const Scope&) = delete;
  Scope& operator=(const Scope&) = delete;
  void SetCounters(uint64_t value0, uint64_t value1, uint64_t value2) {
    if (!recorder_) return;
    Record& r = recorder_->records[id_];
    r.value0 = value0;
    r.value1 = value1;
    r.value2 = value2;
  }

 private:
  Recorder* recorder_ = nullptr;
  int id_ = -1;
};

// Capture exactly one selected request per process. Nsight is configured to
// stop collecting without terminating the server. Errors invalidate evidence,
// but never alter inference Status or add a synchronization boundary.
class ProfileCapture {
 public:
  explicit ProfileCapture(int input_tokens) : tokens_(input_tokens) {
    if (!Enabled() || !NvtxEnabled()) return;
    const char* selected = std::getenv("Q4T_EXEC_PROFILE_INPUT");
    if (!selected) return;
    char* end = nullptr;
    const long target = std::strtol(selected, &end, 10);
    if (end == selected || *end != '\0' || target != input_tokens) return;
    static std::atomic<bool> captured{false};
    if (captured.exchange(true, std::memory_order_relaxed)) return;
    active_ = true;
    const cudaError_t error = cudaProfilerStart();
    std::fprintf(stderr,
        "[exec-capture] event=begin pid=%d tid=%ld input_tokens=%d error=%d\n",
        getpid(), syscall(SYS_gettid), tokens_, static_cast<int>(error));
  }
  ~ProfileCapture() {
    if (!active_) return;
    const cudaError_t error = cudaProfilerStop();
    std::fprintf(stderr,
        "[exec-capture] event=end pid=%d tid=%ld input_tokens=%d error=%d\n",
        getpid(), syscall(SYS_gettid), tokens_, static_cast<int>(error));
  }
  ProfileCapture(const ProfileCapture&) = delete;
  ProfileCapture& operator=(const ProfileCapture&) = delete;

 private:
  int tokens_;
  bool active_ = false;
};

}  // namespace q4t::diagnostic
