#include "q4t/server/grammar_process.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <optional>
#include <thread>
#include <utility>

namespace q4t::server::detail {
namespace {
constexpr int kSocket = 3;

struct PendingWorker {
  int pid = -1;
  int pid_fd = -1;
  uint64_t reported_rss_bytes = 0;
  std::shared_ptr<GrammarWorkerCounters> counters;
};

struct WorkerOwner {
  std::mutex mutex;
  bool reserved = false;
  std::optional<PendingWorker> pending;
  ~WorkerOwner() {
    // Process exit is not proof that a kernel-stalled child was reaped. The
    // outer service owner still reconciles descendants and retains its lease.
    if (pending && pending->pid_fd >= 0) close(pending->pid_fd);
  }
};

WorkerOwner& Owner(IsolatedWorkerDomain domain) {
  // Process-lifetime domains may still be referenced by a bounded-shutdown
  // object controller blocked in exec. No static destruction races with it;
  // the OS closes the descriptors at process exit.
  static auto* grammar = new WorkerOwner;
  static auto* objects = new WorkerOwner;
  static auto* sessions = new WorkerOwner;
  static auto* prefixes = new WorkerOwner;
  static auto* files = new WorkerOwner;
  static auto* documents = new WorkerOwner;
  switch (domain) {
    case IsolatedWorkerDomain::kObjects: return *objects;
    case IsolatedWorkerDomain::kSessionCache: return *sessions;
    case IsolatedWorkerDomain::kPrefixCache: return *prefixes;
    case IsolatedWorkerDomain::kFileFetch: return *files;
    case IsolatedWorkerDomain::kFileParse: return *documents;
    default: return *grammar;
  }
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
std::atomic<bool> defer_reaping{false};
#endif

int WaitWorker(int pid, int* status, rusage* usage) {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (defer_reaping.load()) return 0;
#endif
  return wait4(pid, status, WNOHANG, usage);
}

void Maximum(std::atomic<uint64_t>& value, uint64_t observed) {
  auto previous = value.load();
  while (previous < observed &&
         !value.compare_exchange_weak(previous, observed)) {
  }
}

void CountReaped(const std::shared_ptr<GrammarWorkerCounters>& counters,
                 const GrammarWorkerReport& report) {
  if (!counters) return;
  counters->active.fetch_sub(1);
  counters->reaped.fetch_add(1);
  Maximum(counters->max_rss_bytes, report.max_rss_bytes);
  if (report.reported_rss_bytes) {
    counters->rss_reports.fetch_add(1);
    Maximum(counters->reported_rss_bytes, report.reported_rss_bytes);
  }
}

void PollLocked(WorkerOwner& owner) {
  if (!owner.pending) return;
  auto& pending = *owner.pending;
  rusage usage{};
  GrammarWorkerReport report;
  const int result = WaitWorker(pending.pid, &report.exit_status, &usage);
  // ECHILD/other errors are unknown ownership, not permission for replacement.
  if (result != pending.pid) return;
  report.reaped = true;
  report.max_rss_bytes = static_cast<uint64_t>(usage.ru_maxrss) * 1024;
  report.reported_rss_bytes = pending.reported_rss_bytes;
  CountReaped(pending.counters, report);
  if (pending.pid_fd >= 0) close(pending.pid_fd);
  owner.pending.reset();
  owner.reserved = false;
}

bool ReserveWorker(IsolatedWorkerDomain domain) {
  auto& owner = Owner(domain);
  const std::lock_guard lock(owner.mutex);
  PollLocked(owner);
  if (owner.reserved) return false;
  owner.reserved = true;
  return true;
}

void ReleaseWorker(IsolatedWorkerDomain domain) {
  auto& owner = Owner(domain);
  const std::lock_guard lock(owner.mutex);
  owner.reserved = false;
}

bool Number(const char* text, uint64_t* number) {
  const char* end = text + std::strlen(text);
  auto result = std::from_chars(text, end, *number);
  return result.ec == std::errc{} && result.ptr == end;
}
bool Limit(int resource, uint64_t bytes) {
  const rlimit limit{bytes, bytes};
  return setrlimit(resource, &limit) == 0;
}
}  // namespace

void PollGrammarWorkers() {
  PollIsolatedWorkers(IsolatedWorkerDomain::kGrammar);
}

void PollRequestWorkers() {
  PollGrammarWorkers();
  PollIsolatedWorkers(IsolatedWorkerDomain::kFileFetch);
  PollIsolatedWorkers(IsolatedWorkerDomain::kFileParse);
}

void PollIsolatedWorkers(IsolatedWorkerDomain domain) {
  auto& owner = Owner(domain);
  const std::lock_guard lock(owner.mutex);
  PollLocked(owner);
}

bool GrammarCleanupPending() {
  return IsolatedCleanupPending(IsolatedWorkerDomain::kGrammar);
}

bool IsolatedCleanupPending(IsolatedWorkerDomain domain) {
  auto& owner = Owner(domain);
  const std::lock_guard lock(owner.mutex);
  PollLocked(owner);
  return owner.pending.has_value();
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void DeferGrammarReapingForTesting(bool enabled) {
  defer_reaping.store(enabled);
}
#endif

GrammarProcess::GrammarProcess(GrammarControl control)
    : control_(std::move(control)) {}
GrammarProcess::~GrammarProcess() { Stop(); }

Status GrammarProcess::Start(const char* entry, const GrammarControl& control,
                             std::unique_ptr<GrammarProcess>* out,
                             GrammarFailure* failure) {
  out->reset();
  if (failure) *failure = GrammarFailure::kNone;
  if (!control.address_space_bytes ||
      control.address_space_bytes > kGrammarAddressSpaceLimit)
    return Status::Fail("invalid grammar worker memory ceiling");
  if (control.domain != IsolatedWorkerDomain::kGrammar &&
      control.domain != IsolatedWorkerDomain::kObjects &&
      control.domain != IsolatedWorkerDomain::kSessionCache &&
      control.domain != IsolatedWorkerDomain::kPrefixCache &&
      control.domain != IsolatedWorkerDomain::kFileFetch &&
      control.domain != IsolatedWorkerDomain::kFileParse)
    return Status::Fail("invalid isolated worker domain");
  struct sigaction disposition {};
  if (sigaction(SIGCHLD, nullptr, &disposition) ||
      disposition.sa_handler != SIG_DFL ||
      (disposition.sa_flags & SA_NOCLDWAIT))
    return Status::Fail("grammar worker requires owned child reaping");
  auto process = std::unique_ptr<GrammarProcess>(new GrammarProcess(control));
  if (!ReserveWorker(control.domain)) {
    if (failure) *failure = GrammarFailure::kUnavailable;
    return Status::Fail(
        "grammar worker capacity retained until confirmed reap");
  }
  process->slot_owned_ = true;
  int pair[2];
  if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair))
    return Status::Fail("grammar worker socket creation failed");
  process->socket_ = pair[0];
  // Avoid dup2(fd, fd) and closefrom ordering ambiguities at the fixed fd 3.
  const int child_socket = fcntl(pair[1], F_DUPFD_CLOEXEC, 4);
  close(pair[1]);
  if (child_socket < 0)
    return Status::Fail("grammar worker socket duplication failed");
  posix_spawn_file_actions_t actions;
  posix_spawnattr_t attributes;
  int error = posix_spawn_file_actions_init(&actions);
  if (error) {
    close(child_socket);
    return Status::Fail("grammar worker spawn actions failed");
  }
  const int attribute_error = posix_spawnattr_init(&attributes);
  if (!attribute_error) {
    sigset_t empty, defaults;
    sigemptyset(&empty);
    sigfillset(&defaults);
    error = posix_spawnattr_setsigmask(&attributes, &empty);
    error |= posix_spawnattr_setsigdefault(&attributes, &defaults);
    error |= posix_spawnattr_setflags(
        &attributes, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    error |= posix_spawn_file_actions_adddup2(&actions, child_socket, kSocket);
    error |= posix_spawn_file_actions_addclosefrom_np(&actions, 4);
    for (int fd = 0; fd < 3; ++fd)
      error |= posix_spawn_file_actions_addopen(
          &actions, fd, "/dev/null", fd == 0 ? O_RDONLY : O_WRONLY, 0);
    auto parent = std::to_string(getpid());
    auto limit = std::to_string(control.address_space_bytes);
    char grammar_name[] = "q4t-grammar-worker";
    char objects_name[] = "q4t-object-worker";
    char cache_name[] = "q4t-cache-worker";
    char fetch_name[] = "q4t-file-fetch";
    char document_name[] = "q4t-file-document";
    char* name = control.domain == IsolatedWorkerDomain::kObjects
                     ? objects_name : grammar_name;
    if (control.domain == IsolatedWorkerDomain::kSessionCache ||
        control.domain == IsolatedWorkerDomain::kPrefixCache)
      name = cache_name;
    if (control.domain == IsolatedWorkerDomain::kFileFetch) name = fetch_name;
    if (control.domain == IsolatedWorkerDomain::kFileParse) name = document_name;
    char* argv[] = {name, const_cast<char*>(entry), parent.data(), limit.data(),
                    nullptr};
    char locale[] = "LC_ALL=C";
    char arenas[] = "MALLOC_ARENA_MAX=1";
    char* env[] = {locale, arenas, nullptr};
    if (!error)
      error = posix_spawn(&process->pid_, "/proc/self/exe", &actions,
                          &attributes, argv, env);
    posix_spawnattr_destroy(&attributes);
  } else {
    error = attribute_error;
  }
  posix_spawn_file_actions_destroy(&actions);
  close(child_socket);
  if (error) return Status::Fail("grammar worker exec failed");
  if (control.counters) {
    control.counters->started.fetch_add(1);
    control.counters->active.fetch_add(1);
  }
  if (control.worker_started) control.worker_started();
  process->pid_fd_ = syscall(SYS_pidfd_open, process->pid_, 0);
  if (process->pid_fd_ < 0)
    return Status::Fail("grammar worker pidfd unavailable");
  *out = std::move(process);
  return Status();
}

void GrammarProcess::Stop() {
  if (socket_ >= 0) close(std::exchange(socket_, -1));
  if (pid_ > 0) {
    if (pid_fd_ >= 0) {
      syscall(SYS_pidfd_send_signal, pid_fd_, SIGKILL, nullptr, 0);
    } else {
      // pidfd creation failed: this unreaped direct child still owns its PID.
      // No other code in the server reaps children or ignores SIGCHLD.
      kill(pid_, SIGKILL);
    }
    rusage usage{};
    GrammarWorkerReport report;
    const auto deadline =
        std::chrono::steady_clock::now() +
        std::chrono::milliseconds(kGrammarCleanupMilliseconds);
    int result = 0;
    do {
      result = WaitWorker(pid_, &report.exit_status, &usage);
      if (result == pid_ || (result < 0 && errno != EINTR)) break;
      if (std::chrono::steady_clock::now() >= deadline) break;
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    } while (true);
    report.reaped = result == pid_;
    report.max_rss_bytes = static_cast<uint64_t>(usage.ru_maxrss) * 1024;
    report.reported_rss_bytes = reported_rss_bytes_;
    if (report.reaped) {
      CountReaped(control_.counters, report);
      ReleaseWorker(control_.domain);
    } else {
      auto& owner = Owner(control_.domain);
      const std::lock_guard lock(owner.mutex);
      owner.pending.emplace(PendingWorker{pid_, std::exchange(pid_fd_, -1),
                                          reported_rss_bytes_,
                                          control_.counters});
    }
    slot_owned_ = false;
    pid_ = -1;
    if (control_.worker_finished) control_.worker_finished(report);
  }
  if (pid_fd_ >= 0) close(std::exchange(pid_fd_, -1));
  if (slot_owned_) {
    ReleaseWorker(control_.domain);
    slot_owned_ = false;
  }
}

void GrammarProcess::ReportRss(uint64_t bytes) {
  reported_rss_bytes_ = std::max(reported_rss_bytes_, bytes);
}

Status GrammarProcess::Transfer(
    void* data, size_t bytes, bool write,
    std::chrono::steady_clock::time_point deadline,
    const std::function<bool()>& cancelled) {
  auto* cursor = static_cast<char*>(data);
  while (bytes) {
    if ((cancelled && cancelled()) ||
        (control_.cancelled && control_.cancelled())) {
      failure_ = GrammarFailure::kCancelled;
      Stop();
      return Status::Fail("grammar operation cancelled");
    }
    const auto remaining = deadline - std::chrono::steady_clock::now();
    if (remaining <= decltype(remaining)::zero()) {
      failure_ = GrammarFailure::kTimeout;
      Stop();
      return Status::Fail("grammar operation time limit exceeded");
    }
    if (socket_ < 0) break;
    pollfd descriptor{socket_, static_cast<short>(write ? POLLOUT : POLLIN), 0};
    const int timeout = static_cast<int>(std::clamp<int64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(remaining)
            .count(),
        1, 10));
    const int ready = poll(&descriptor, 1, timeout);
    if (ready < 0 && errno == EINTR) continue;
    if (ready < 0) break;
    if (!ready) continue;
    const size_t chunk = std::min<size_t>(bytes, 65536);
    const auto count =
        write ? send(socket_, cursor, chunk, MSG_DONTWAIT | MSG_NOSIGNAL)
              : recv(socket_, cursor, chunk, MSG_DONTWAIT);
    if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    if (count <= 0) break;
    cursor += count;
    bytes -= count;
  }
  if (!bytes) return Status();
  failure_ = GrammarFailure::kWorker;
  Stop();
  return Status::Fail("grammar worker closed or invalid transport");
}

Status GrammarProcess::AwaitExit(
    std::chrono::steady_clock::time_point deadline,
    const std::function<bool()>& cancelled) {
  while (pid_ > 0 && pid_fd_ >= 0) {
    if ((cancelled && cancelled()) ||
        (control_.cancelled && control_.cancelled())) {
      failure_ = GrammarFailure::kCancelled;
      Stop();
      return Status::Fail("worker completion cancelled");
    }
    const auto remaining = deadline - std::chrono::steady_clock::now();
    if (remaining <= decltype(remaining)::zero()) {
      failure_ = GrammarFailure::kTimeout;
      Stop();
      return Status::Fail("worker completion time limit exceeded");
    }
    rusage usage{};
    GrammarWorkerReport report;
    const int waited = WaitWorker(pid_, &report.exit_status, &usage);
    if (waited == pid_) {
      report.reaped = true;
      report.max_rss_bytes = static_cast<uint64_t>(usage.ru_maxrss) * 1024;
      report.reported_rss_bytes = reported_rss_bytes_;
      CountReaped(control_.counters, report);
      ReleaseWorker(control_.domain);
      slot_owned_ = false;
      pid_ = -1;
      close(std::exchange(pid_fd_, -1));
      if (socket_ >= 0) close(std::exchange(socket_, -1));
      if (control_.worker_finished) control_.worker_finished(report);
      if (WIFEXITED(report.exit_status) && !WEXITSTATUS(report.exit_status))
        return {};
      failure_ = GrammarFailure::kWorker;
      return Status::Fail("worker did not exit successfully");
    }
    if (waited < 0 && errno != EINTR) break;
    pollfd descriptor{pid_fd_, POLLIN, 0};
    const int timeout = static_cast<int>(std::clamp<int64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(remaining)
            .count(),
        1, 10));
    const int ready = poll(&descriptor, 1, timeout);
    if (ready < 0 && errno != EINTR) break;
    // A signalled pidfd alone is not confirmation that wait4 succeeded.
    if (ready > 0) std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  failure_ = GrammarFailure::kWorker;
  Stop();
  return Status::Fail("worker completion ownership unavailable");
}

Status GrammarProcess::CollectOutput(
    std::string* output, size_t max_bytes,
    std::chrono::steady_clock::time_point deadline,
    const std::function<bool()>& cancelled) {
  if (!output) {
    failure_ = GrammarFailure::kWorker;
    Stop();
    return Status::Fail("missing worker output destination");
  }
  output->clear();
  std::string next;
  std::array<char, 65536> buffer;
  try {
    while (socket_ >= 0 && pid_ > 0) {
      if ((cancelled && cancelled()) ||
          (control_.cancelled && control_.cancelled())) {
        failure_ = GrammarFailure::kCancelled;
        Stop();
        return Status::Fail("worker output cancelled");
      }
      const auto remaining = deadline - std::chrono::steady_clock::now();
      if (remaining <= decltype(remaining)::zero()) {
        failure_ = GrammarFailure::kTimeout;
        Stop();
        return Status::Fail("worker output time limit exceeded");
      }
      pollfd descriptor{socket_, POLLIN, 0};
      const int timeout = static_cast<int>(std::clamp<int64_t>(
          std::chrono::duration_cast<std::chrono::milliseconds>(remaining)
              .count(),
          1, 10));
      const int ready = poll(&descriptor, 1, timeout);
      if (ready < 0 && errno == EINTR) continue;
      if (ready < 0) break;
      if (!ready) continue;
      const auto count =
          recv(socket_, buffer.data(), buffer.size(), MSG_DONTWAIT);
      if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
      if (count < 0) break;
      if (!count) {
        const auto status = AwaitExit(deadline, cancelled);
        if (status) *output = std::move(next);
        return status;
      }
      if (static_cast<size_t>(count) > max_bytes - next.size()) {
        failure_ = GrammarFailure::kResource;
        Stop();
        return Status::Fail("worker output capacity exceeded");
      }
      next.append(buffer.data(), count);
    }
  } catch (const std::bad_alloc&) {
    failure_ = GrammarFailure::kResource;
    Stop();
    return Status::Fail("worker output allocation failed");
  }
  failure_ = GrammarFailure::kWorker;
  Stop();
  return Status::Fail("worker output transport unavailable");
}

bool EnterGrammarWorker(int argc, char** argv) {
  uint64_t parent, address_space;
  if (argc != 4 || !Number(argv[2], &parent) ||
      !Number(argv[3], &address_space) || parent <= 1 || address_space == 0 ||
      address_space > kGrammarAddressSpaceLimit)
    return false;
  if (prctl(PR_SET_PDEATHSIG, SIGKILL) ||
      static_cast<uint64_t>(getppid()) != parent)
    return false;
  ucred credentials{};
  socklen_t size = sizeof(credentials);
  if (getsockopt(kSocket, SOL_SOCKET, SO_PEERCRED, &credentials, &size) ||
      static_cast<uint64_t>(credentials.pid) != parent ||
      credentials.uid != getuid())
    return false;
  if (!Limit(RLIMIT_CORE, 0) || !Limit(RLIMIT_AS, address_space) ||
      !Limit(RLIMIT_STACK, 8 * 1024 * 1024) || !Limit(RLIMIT_NOFILE, 32))
    return false;
  // setrlimit alone does not shrink mappings inherited from the ELF loader.
  // Refuse an already-oversized baseline before any input/compiler allocation.
  FILE* memory = std::fopen("/proc/self/statm", "r");
  unsigned long pages = 0;
  const bool read = memory && std::fscanf(memory, "%lu", &pages) == 1;
  if (memory) std::fclose(memory);
  const auto page_size = sysconf(_SC_PAGESIZE);
  return read && page_size > 0 &&
         pages <= address_space / static_cast<uint64_t>(page_size);
}

bool WorkerTransfer(void* data, size_t bytes, bool write) {
  auto* cursor = static_cast<char*>(data);
  while (bytes) {
    const auto count = write ? send(kSocket, cursor, bytes, MSG_NOSIGNAL)
                             : recv(kSocket, cursor, bytes, 0);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    cursor += count;
    bytes -= count;
  }
  return true;
}
}  // namespace q4t::server::detail
