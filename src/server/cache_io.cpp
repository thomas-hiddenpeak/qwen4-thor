#include "q4t/server/cache_io.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

#include "cache_protocol.h"

namespace q4t::server {
namespace {
using namespace cache_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
std::atomic<bool>& ControllerOwned(IsolatedWorkerDomain domain) {
  static auto* sessions = new std::atomic<bool>(false);
  static auto* prefixes = new std::atomic<bool>(false);
  return domain == IsolatedWorkerDomain::kPrefixCache ? *prefixes : *sessions;
}
std::atomic<uint64_t> next_generation{0};
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
std::atomic<bool> defer_spawn{false}, fail_controller{false},
    fail_thread{false};
std::mutex entry_mutex;
std::string test_entry;
#endif
bool Valid(const CacheIoOptions& o) {
  return !o.directory.empty() && o.directory.size() <= 4096 &&
         o.directory.find('\0') == std::string::npos && o.timeout_ms > 0 &&
         o.timeout_ms <= 2000 && o.restart_limit > 0 && o.restart_limit <= 3 &&
         o.restart_window_ms >= 100 && o.backoff_ms >= 0 &&
         o.backoff_ms <= o.restart_window_ms &&
         (o.domain == IsolatedWorkerDomain::kSessionCache ||
          o.domain == IsolatedWorkerDomain::kPrefixCache);
}
void Prune(std::array<Clock::time_point, 3>& events, size_t* count,
           Clock::time_point now, int window_ms) {
  size_t kept = 0;
  for (size_t i = 0; i < *count; ++i)
    if (now - events[i] < std::chrono::milliseconds(window_ms))
      events[kept++] = events[i];
  *count = kept;
}
Status Unavailable() { return Status::Fail("cache storage unavailable"); }
}  // namespace

struct CacheIo::State {
  struct Job {
    Frame request;
    Reply reply;
    std::vector<uint8_t> payload;
    Clock::time_point deadline;
    std::atomic<bool> abandoned{false};
    bool started = false;
    bool complete = false;
    bool ok = false;
  };
  CacheIoOptions options;
  std::string entry = "--q4t-cache-worker";
  std::mutex mutex;
  std::condition_variable changed;
  std::shared_ptr<Job> pending;
  std::atomic<bool> stop{false}, invalidated{false};
  bool done = false, controller_active = false, owns_controller = false;
  bool available = false;
  std::string_view health = "starting";
  uint64_t generation = 0, controller_failures = 0;
  int pid = -1;
  std::array<Clock::time_point, 3> starts{}, failures{};
  size_t start_count = 0, failure_count = 0;
  Clock::time_point retry_after{}, controller_retry{};
  std::shared_ptr<GrammarWorkerCounters> counters =
      std::make_shared<GrammarWorkerCounters>();

  void FailPending() noexcept {
    available = false;
    pid = -1;
    if (pending) {
      pending->ok = false;
      pending->complete = true;
      pending.reset();
    }
  }
  void RecordFailure() noexcept {
    const auto now = Clock::now();
    Prune(failures, &failure_count, now, options.restart_window_ms);
    if (failure_count < failures.size()) failures[failure_count++] = now;
    controller_retry =
        now + std::chrono::milliseconds(std::max(1, options.backoff_ms));
    if (failure_count >= static_cast<size_t>(options.restart_limit))
      controller_retry = std::max(
          controller_retry,
          failures[0] + std::chrono::milliseconds(options.restart_window_ms));
    ++controller_failures;
    health = "controller_backoff";
  }
  void Retire() noexcept {
    FailPending();
    health = "stopped";
    done = true;
    controller_active = false;
    if (owns_controller) {
      owns_controller = false;
      ControllerOwned(options.domain).store(false);
    }
  }
  void Launch(const std::shared_ptr<State>& self) {
    // No filesystem or spawn under this mutex. A stalled controller retains
    // its process-wide slot even after its CacheIo object is destroyed.
    if (controller_active || done || stop || Clock::now() < controller_retry)
      return;
    try {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      if (fail_thread.exchange(false)) throw std::bad_alloc();
#endif
      std::thread([self] { self->Supervise(); }).detach();
      controller_active = true;
      health = "starting";
    } catch (...) {
      RecordFailure();
    }
  }
  void Supervise() noexcept {
    while (!stop.load()) {
      try {
        Run();
      } catch (...) {
        const std::lock_guard lock(mutex);
        FailPending();
        RecordFailure();
      }
      changed.notify_all();
      std::unique_lock lock(mutex);
      if (stop.load()) break;
      changed.wait_until(lock, controller_retry, [&] { return stop.load(); });
    }
    {
      const std::lock_guard lock(mutex);
      Retire();
    }
    changed.notify_all();
  }
  void SetHealth(std::string_view value, bool ready = false, int child = -1) {
    const std::lock_guard lock(mutex);
    health = value;
    available = ready && !invalidated.load();
    pid = child;
  }
  bool Exchange(detail::GrammarProcess& process, Frame frame,
                std::vector<uint8_t>& payload, const Job& job, Reply* reply) {
    const auto cancelled = [&] { return stop.load() || job.abandoned.load(); };
    if (!process.Transfer(&frame, sizeof(frame), true, job.deadline,
                          cancelled) ||
        ((frame.operation == kInit || frame.operation == kWrite) &&
         !process.Transfer(payload.data(), payload.size(), true, job.deadline,
                           cancelled)) ||
        !process.Transfer(reply, sizeof(*reply), false, job.deadline,
                          cancelled))
      return false;
    if (reply->magic != kMagic || reply->sequence != frame.sequence ||
        reply->generation != frame.generation || reply->code > kFull ||
        reply->bytes > CacheIo::kBlockBytes ||
        reply->span > CacheIo::kMaxFileSpan ||
        reply->allocated > CacheIo::kMaxFileSpan + (64ULL << 20) ||
        reply->rss > kGrammarAddressSpaceLimit ||
        (reply->code == kOk &&
         ((frame.operation == kRead && reply->bytes != frame.bytes) ||
          (frame.operation != kRead && reply->bytes) ||
          (frame.operation == kCreate && !reply->file) ||
          (frame.file && reply->file != frame.file))))
      return false;
    if (reply->code != kOk) return false;
    if (frame.operation == kRead &&
        !process.Transfer(payload.data(), payload.size(), false, job.deadline,
                          cancelled))
      return false;
    process.ReportRss(reply->rss);
    auto old = counters->reported_rss_bytes.load();
    while (
        old < reply->rss &&
        !counters->reported_rss_bytes.compare_exchange_weak(old, reply->rss)) {
    }
    return true;
  }
  void Run() {
    std::unique_ptr<detail::GrammarProcess> process;
    uint64_t sequence = 0, current_generation = 0;
    for (;;) {
      std::shared_ptr<Job> job;
      bool discard;
      {
        std::unique_lock lock(mutex);
        changed.wait(lock, [&] { return stop || pending || invalidated; });
        if (stop) break;
        discard = invalidated.exchange(false);
        job = pending;
        if (job) job->started = true;
      }
      if (discard) {
        process.reset();
        SetHealth("unavailable");
        retry_after =
            Clock::now() + std::chrono::milliseconds(options.backoff_ms);
      }
      if (!job) continue;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      if (fail_controller.exchange(false)) throw std::bad_alloc();
#endif
      bool ok = false;
      const auto now = Clock::now();
      if (!job->abandoned && now < job->deadline) {
        if (!process && !job->request.generation) {
          Prune(starts, &start_count, now, options.restart_window_ms);
          if (detail::IsolatedCleanupPending(options.domain)) {
            SetHealth("cleanup_pending");
          } else if (now < retry_after ||
                     start_count >=
                         static_cast<size_t>(options.restart_limit)) {
            SetHealth("backoff");
          } else {
            starts[start_count++] = now;
            SetHealth("starting");
            GrammarControl control;
            control.domain = options.domain;
            control.counters = counters;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
            while (defer_spawn.load()) std::this_thread::sleep_for(1ms);
#endif
            if (!stop && !job->abandoned && Clock::now() < job->deadline &&
                detail::GrammarProcess::Start(entry.c_str(), control,
                                              &process)) {
              current_generation = next_generation.fetch_add(1) + 1;
              sequence = 0;
              Frame init;
              init.sequence = ++sequence;
              init.generation = current_generation;
              init.operation = kInit;
              init.bytes = options.directory.size();
              std::vector<uint8_t> path(options.directory.begin(),
                                        options.directory.end());
              Reply reply;
              if (Exchange(*process, init, path, *job, &reply)) {
                const std::lock_guard lock(mutex);
                generation = current_generation;
              } else {
                process.reset();
              }
            }
            if (!process) {
              retry_after =
                  Clock::now() + std::chrono::milliseconds(options.backoff_ms);
              SetHealth("unavailable");
            }
          }
        }
        if (process && (!job->request.generation ||
                        job->request.generation == current_generation)) {
          if (job->request.operation == kInit) {
            job->reply.generation = current_generation;
            ok = true;
          } else {
            auto frame = job->request;
            frame.generation = current_generation;
            frame.sequence = ++sequence;
            ok = Exchange(*process, frame, job->payload, *job, &job->reply);
          }
          if (!ok || job->abandoned || stop) {
            ok = false;
            process.reset();
            retry_after =
                Clock::now() + std::chrono::milliseconds(options.backoff_ms);
            SetHealth("unavailable");
          } else {
            SetHealth("ready", true, process->Pid());
          }
        }
      }
      {
        const std::lock_guard lock(mutex);
        job->ok = ok && !job->abandoned && !invalidated && !stop;
        job->complete = true;
        pending.reset();
      }
      changed.notify_all();
    }
  }
};

CacheIo::CacheIo(CacheIoOptions options) : state_(std::make_shared<State>()) {
  state_->options = std::move(options);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  {
    const std::lock_guard lock(entry_mutex);
    if (!test_entry.empty()) state_->entry = test_entry;
  }
#endif
  if (!Valid(state_->options)) {
    state_->health = "invalid_configuration";
    state_->done = true;
    return;
  }
  bool expected = false;
  if (!ControllerOwned(state_->options.domain)
           .compare_exchange_strong(expected, true)) {
    state_->health = "controller_owned";
    state_->done = true;
    return;
  }
  state_->owns_controller = true;
  const std::lock_guard lock(state_->mutex);
  state_->Launch(state_);
}
CacheIo::~CacheIo() {
  state_->stop.store(true);
  state_->changed.notify_all();
  std::unique_lock lock(state_->mutex);
  if (!state_->controller_active && !state_->done) state_->Retire();
  state_->changed.wait_for(lock, 100ms, [&] { return state_->done; });
}

Status CacheIo::Dispatch(uint64_t operation, CacheFile file, uint64_t offset,
                         std::span<const uint8_t> input,
                         std::span<uint8_t> output, CacheFile* created,
                         CacheFileStat* stat, const Cancel& cancel) {
  // A request already cancelled before dispatch has no unknown file mutation.
  try {
    if (cancel && cancel()) return Status::Fail("cache storage cancelled");
  } catch (...) {
    return Status::Fail("cache storage cancelled");
  }
  const size_t bytes = std::max(input.size(), output.size());
  if ((operation == kRead || operation == kWrite) &&
      (!file.id || !file.generation || !bytes || bytes > kBlockBytes ||
       bytes % kAlignment || offset % kAlignment ||
       offset > kMaxFileSpan - bytes))
    return Status::Fail("cache storage invalid range");
  std::unique_lock lock(state_->mutex);
  if (state_->stop || state_->done || state_->pending) return Unavailable();
  state_->Launch(state_);
  if (!state_->controller_active) return Unavailable();
  if (file.id && (!state_->available || state_->invalidated ||
                  file.generation != state_->generation))
    return Unavailable();
  auto job = std::make_shared<State::Job>();
  job->deadline =
      Clock::now() + std::chrono::milliseconds(state_->options.timeout_ms);
  job->request.operation = operation;
  job->request.generation = file.generation;
  job->request.file = file.id;
  job->request.offset = offset;
  job->request.bytes = bytes;
  job->payload.resize(bytes);
  if (!input.empty()) std::memcpy(job->payload.data(), input.data(), bytes);
  state_->pending = job;
  state_->changed.notify_all();
  while (!job->complete) {
    // Cancellation belongs to this waiting caller, never the controller.
    bool cancelled = false;
    try {
      if (cancel) cancelled = cancel();
    } catch (...) {
      cancelled = true;
    }
    if (cancelled || state_->stop || Clock::now() >= job->deadline) {
      job->abandoned.store(true);
      state_->invalidated.store(true);
      state_->available = false;
      state_->health = "unavailable";
      state_->changed.notify_all();
      return Status::Fail(cancelled ? "cache storage cancelled"
                                    : "cache storage time limit exceeded");
    }
    state_->changed.wait_until(lock,
                               std::min(job->deadline, Clock::now() + 5ms));
  }
  if (!job->ok) return Unavailable();
  if (created) *created = {job->reply.generation, job->reply.file};
  if (stat) *stat = {job->reply.allocated, job->reply.span};
  if (!output.empty()) std::memcpy(output.data(), job->payload.data(), bytes);
  return Status();
}

Status CacheIo::Initialize(const Cancel& cancel) {
  return Dispatch(kInit, {}, 0, {}, {}, nullptr, nullptr, cancel);
}
Status CacheIo::Create(CacheFile* file, const Cancel& cancel) {
  if (!file) return Status::Fail("cache storage missing handle");
  *file = {};
  return Dispatch(kCreate, {}, 0, {}, {}, file, nullptr, cancel);
}
Status CacheIo::Read(CacheFile file, uint64_t offset, std::span<uint8_t> output,
                     const Cancel& cancel) {
  return Dispatch(kRead, file, offset, {}, output, nullptr, nullptr, cancel);
}
Status CacheIo::Write(CacheFile file, uint64_t offset,
                      std::span<const uint8_t> input, const Cancel& cancel) {
  return Dispatch(kWrite, file, offset, input, {}, nullptr, nullptr, cancel);
}
Status CacheIo::Stat(CacheFile file, CacheFileStat* stat,
                     const Cancel& cancel) {
  if (!stat || !file.id || !file.generation)
    return Status::Fail("cache storage missing stat handle");
  return Dispatch(kStat, file, 0, {}, {}, nullptr, stat, cancel);
}
void CacheIo::Close(CacheFile file) noexcept {
  if (!file.id) return;
  try {
    // A failed close receipt cannot leak files until the worker fills up.
    // Invalidate only the matching generation; stale handles are harmless.
    if (Health().generation == file.generation &&
        !Dispatch(kClose, file, 0, {}, {}, nullptr, nullptr, {})) {
      const std::lock_guard lock(state_->mutex);
      if (state_->generation == file.generation)
        state_->invalidated.store(true);
      state_->changed.notify_all();
    }
  } catch (...) {
    const std::lock_guard lock(state_->mutex);
    if (state_->generation == file.generation) state_->invalidated.store(true);
    state_->changed.notify_all();
  }
}
CacheIoHealth CacheIo::Health() const {
  const std::lock_guard lock(state_->mutex);
  return {state_->health,
          true,
          state_->available && !state_->invalidated,
          state_->generation,
          state_->counters->started.load(),
          state_->counters->reaped.load(),
          state_->counters->reported_rss_bytes.load(),
          state_->controller_failures,
          state_->counters->active.load(),
          state_->pid};
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Status CacheIo::TruncateForTesting(CacheFile file) {
  return Dispatch(kTestTruncate, file, 0, {}, {}, nullptr, nullptr, {});
}
Status CacheIo::FailNextWriteForTesting() {
  return Dispatch(kTestShortWrite, {}, 0, {}, {}, nullptr, nullptr, {});
}
void DeferCacheSpawnForTesting(bool enabled) { defer_spawn.store(enabled); }
void FailCacheControllerForTesting() { fail_controller.store(true); }
void FailCacheThreadStartForTesting() { fail_thread.store(true); }
void CacheWorkerEntryForTesting(std::string entry) {
  const std::lock_guard lock(entry_mutex);
  test_entry = std::move(entry);
}
#endif
}  // namespace q4t::server
