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
#include <cerrno>
#include <charconv>
#include <cstdio>
#include <cstring>
#include <utility>

namespace q4t::server::detail {
namespace {
constexpr int kSocket = 3;
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

GrammarProcess::GrammarProcess(GrammarControl control)
    : control_(std::move(control)) {}
GrammarProcess::~GrammarProcess() { Stop(); }

Status GrammarProcess::Start(const char* entry, const GrammarControl& control,
                             std::unique_ptr<GrammarProcess>* out) {
  out->reset();
  if (!control.address_space_bytes ||
      control.address_space_bytes > kGrammarAddressSpaceLimit)
    return Status::Fail("invalid grammar worker memory ceiling");
  struct sigaction disposition {};
  if (sigaction(SIGCHLD, nullptr, &disposition) ||
      disposition.sa_handler != SIG_DFL ||
      (disposition.sa_flags & SA_NOCLDWAIT))
    return Status::Fail("grammar worker requires owned child reaping");
  auto process = std::unique_ptr<GrammarProcess>(new GrammarProcess(control));
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
    char name[] = "q4t-grammar-worker";
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
  if (control.worker_started) control.worker_started();
  process->pid_fd_ = syscall(SYS_pidfd_open, process->pid_, 0);
  if (process->pid_fd_ < 0)
    return Status::Fail("grammar worker pidfd unavailable");
  *out = std::move(process);
  return Status();
}

void GrammarProcess::Stop() {
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
    int result;
    do {
      result = wait4(pid_, &report.exit_status, 0, &usage);
    } while (result < 0 && errno == EINTR);
    report.reaped = result == pid_;
    report.max_rss_bytes = static_cast<uint64_t>(usage.ru_maxrss) * 1024;
    pid_ = -1;
    if (control_.worker_finished) control_.worker_finished(report);
  }
  if (pid_fd_ >= 0) close(std::exchange(pid_fd_, -1));
  if (socket_ >= 0) close(std::exchange(socket_, -1));
}

Status GrammarProcess::Transfer(
    void* data, size_t bytes, bool write,
    std::chrono::steady_clock::time_point deadline) {
  auto* cursor = static_cast<char*>(data);
  while (bytes) {
    if (control_.cancelled && control_.cancelled()) {
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
