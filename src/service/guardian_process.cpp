#include "q4t/service/guardian_process.h"

#include <dirent.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <climits>
#include <cstring>
#include <sstream>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>

namespace q4t::service {
namespace {
constexpr size_t kMaxProcesses = 65536;
constexpr size_t kMaxArguments = 128;
constexpr size_t kMaxExecBytes = 64 * 1024;

Status Error(const char* operation) {
  return Status::Fail(std::string(operation) + ": " + std::strerror(errno));
}

template <class T>
bool Number(std::string_view text, T* result) {
  const auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), *result);
  return error == std::errc() && end == text.data() + text.size();
}

[[noreturn]] void ChildError(int fd) {
  const int error = errno ? errno : EINVAL;
  // A single int fits atomically in the empty nonblocking exec-error pipe.
  const ssize_t written = write(fd, &error, sizeof(error));
  (void)written;
  _exit(127);
}

bool CloseExcept(const std::vector<int>& fds) {
  unsigned first = 3;
  for (int fd : fds) {
    const unsigned next = static_cast<unsigned>(fd);
    if (next > first && syscall(SYS_close_range, first, next - 1, 0) != 0) {
      return false;
    }
    first = next + 1;
  }
  return syscall(SYS_close_range, first, UINT_MAX, 0) == 0;
}

bool ValidExec(const std::vector<std::string>& argv,
               const std::vector<std::string>& env) {
  if (argv.empty() || argv[0].empty() || argv[0][0] != '/' ||
      argv.size() > kMaxArguments || env.size() > kMaxArguments) {
    return false;
  }
  size_t bytes = 0;
  for (const auto* values : {&argv, &env}) {
    for (const auto& value : *values) {
      if (value.find('\0') != std::string::npos || value.size() > 8192) {
        return false;
      }
      bytes += value.size() + 1;
    }
  }
  std::unordered_set<std::string> names;
  for (const auto& value : env) {
    const size_t equal = value.find('=');
    if (equal == std::string::npos || equal == 0 ||
        !names.insert(value.substr(0, equal)).second)
      return false;
  }
  return bytes <= kMaxExecBytes;
}
}  // namespace

OwnedFd::~OwnedFd() { Reset(); }
OwnedFd::OwnedFd(OwnedFd&& other) noexcept : fd_(other.Release()) {}
OwnedFd& OwnedFd::operator=(OwnedFd&& other) noexcept {
  if (this != &other) Reset(other.Release());
  return *this;
}
int OwnedFd::Release() { return std::exchange(fd_, -1); }
void OwnedFd::Reset(int fd) {
  if (fd_ >= 0) close(fd_);  // Linux close must not be retried after EINTR.
  fd_ = fd;
}

Status ReadProcess(pid_t pid, ProcessRecord* record, bool* gone) {
  if (gone != nullptr) *gone = false;
  if (pid <= 0 || record == nullptr) return Status::Fail("invalid PID output");
  const std::string path = "/proc/" + std::to_string(pid) + "/stat";
  const OwnedFd fd(open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
  if (fd.Get() < 0) {
    if (gone != nullptr) *gone = errno == ENOENT || errno == ESRCH;
    return Error("open process identity");
  }
  std::array<char, 8192> bytes{};
  ssize_t count;
  do {
    count = read(fd.Get(), bytes.data(), bytes.size());
  } while (count < 0 && errno == EINTR);
  if (count < 0) {
    if (gone != nullptr) *gone = errno == ENOENT || errno == ESRCH;
    return Error("read process identity");
  }
  if (count == 0 || count == static_cast<ssize_t>(bytes.size())) {
    return Status::Fail("invalid process record size");
  }
  std::string_view text(bytes.data(), static_cast<size_t>(count));
  const size_t close = text.rfind(')');
  const size_t open = text.find('(');
  pid_t observed_pid = -1;
  if (open == std::string_view::npos || open == 0 ||
      close == std::string_view::npos || close <= open ||
      !Number(text.substr(0, open - 1), &observed_pid) || observed_pid != pid) {
    return Status::Fail("malformed process identity");
  }
  text.remove_prefix(close + 1);
  std::array<std::string_view, 20> fields{};
  for (auto& field : fields) {
    const size_t begin = text.find_first_not_of(' ');
    if (begin == std::string_view::npos) return Status::Fail("short stat");
    text.remove_prefix(begin);
    const size_t end = text.find(' ');
    if (end == std::string_view::npos) return Status::Fail("short stat field");
    field = text.substr(0, end);
    text.remove_prefix(end);
  }
  ProcessRecord parsed;
  parsed.identity.pid = pid;
  if (fields[0].size() != 1 || !Number(fields[1], &parsed.parent) ||
      !Number(fields[2], &parsed.group) ||
      !Number(fields[19], &parsed.identity.start_ticks) ||
      parsed.identity.start_ticks == 0) {
    return Status::Fail("invalid stat fields");
  }
  parsed.state = fields[0][0];
  *record = parsed;
  return Status{};
}

Status OpenProcess(const ProcessIdentity& expected, OwnedFd* pidfd) {
  if (expected.pid <= 0 || !expected.start_ticks || pidfd == nullptr) {
    return Status::Fail("invalid expected process identity");
  }
  OwnedFd fd(static_cast<int>(syscall(SYS_pidfd_open, expected.pid, 0)));
  if (fd.Get() < 0) return Error("pidfd_open");
  ProcessRecord current;
  const auto status = ReadProcess(expected.pid, &current);
  if (!status.ok()) return status;
  if (current.identity != expected) return Status::Fail("PID was reused");
  *pidfd = std::move(fd);
  return Status{};
}

Status SignalProcess(int pidfd, int signal) {
  if (pidfd < 0 || signal < 0 || signal >= NSIG) {
    return Status::Fail("invalid pidfd signal");
  }
  if (syscall(SYS_pidfd_send_signal, pidfd, signal, nullptr, 0) != 0 &&
      errno != ESRCH)
    return Error("pidfd_send_signal");
  return Status{};
}

Status ProcessExited(int pidfd, bool* exited) {
  if (pidfd < 0 || exited == nullptr) return Status::Fail("invalid pidfd poll");
  pollfd item{pidfd, POLLIN, 0};
  int result;
  do {
    result = poll(&item, 1, 0);
  } while (result < 0 && errno == EINTR);
  if (result < 0) return Error("poll pidfd");
  if (item.revents & POLLNVAL) return Status::Fail("closed pidfd");
  *exited = (item.revents & (POLLIN | POLLHUP)) != 0;
  return Status{};
}

Status EnableSubreaper() {
  if (prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0) {
    return Error("enable subreaper");
  }
  int enabled = 0;
  if (prctl(PR_GET_CHILD_SUBREAPER, &enabled, 0, 0, 0) != 0 || !enabled) {
    return Status::Fail("subreaper unavailable");
  }
  return Status{};
}

Status SpawnProcess(const std::vector<std::string>& argv,
                    const std::vector<std::string>& env,
                    const std::vector<int>& pass_fds, ChildProcess* child) {
  if (!ValidExec(argv, env) || child == nullptr || pass_fds.size() > 16) {
    return Status::Fail("invalid bounded spawn specification");
  }
  if (child->identity.pid > 0 || child->pidfd.Get() >= 0) {
    return Status::Fail("spawn output already owns a process");
  }
  struct sigaction disposition {};
  if (sigaction(SIGCHLD, nullptr, &disposition) != 0 ||
      disposition.sa_handler != SIG_DFL ||
      (disposition.sa_flags & SA_NOCLDWAIT)) {
    return Status::Fail("Guardian requires explicit child reaping");
  }
  ProcessRecord self;
  auto status = ReadProcess(getpid(), &self);
  if (!status.ok()) return status;
  OwnedFd capability;
  status = OpenProcess(self.identity, &capability);
  if (!status.ok()) return status;
  std::vector<int> keep = pass_fds;
  std::sort(keep.begin(), keep.end());
  if (std::adjacent_find(keep.begin(), keep.end()) != keep.end()) {
    return Status::Fail("duplicate inherited FD");
  }
  for (int fd : keep) {
    if (fd < 3 || fcntl(fd, F_GETFD) < 0)
      return Status::Fail("invalid pass FD");
  }
  int errors[2];
  if (pipe2(errors, O_CLOEXEC | O_NONBLOCK) != 0) return Error("exec pipe");
  OwnedFd read_error(errors[0]), write_error(errors[1]);
  keep.push_back(write_error.Get());
  std::sort(keep.begin(), keep.end());
  std::vector<char*> args, environment;
  for (const auto& value : argv)
    args.push_back(const_cast<char*>(value.c_str()));
  for (const auto& value : env) {
    environment.push_back(const_cast<char*>(value.c_str()));
  }
  args.push_back(nullptr);
  environment.push_back(nullptr);
  const pid_t parent = getpid();
  const pid_t pid = fork();
  if (pid < 0) return Error("fork");
  if (pid == 0) {
    const int error_fd = write_error.Get();
    if (prctl(PR_SET_PDEATHSIG, SIGTERM, 0, 0, 0) != 0 || getppid() != parent ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 || setsid() < 0) {
      ChildError(error_fd);
    }
    for (int sig : {SIGTERM, SIGINT, SIGHUP, SIGPIPE, SIGCHLD}) {
      struct sigaction action {};
      action.sa_handler = SIG_DFL;
      sigemptyset(&action.sa_mask);
      if (sigaction(sig, &action, nullptr) != 0) ChildError(error_fd);
    }
    sigset_t empty;
    sigemptyset(&empty);
    if (sigprocmask(SIG_SETMASK, &empty, nullptr) != 0 || getppid() != parent)
      ChildError(error_fd);
    for (int fd : pass_fds) {
      if (fcntl(fd, F_SETFD, 0) != 0) ChildError(error_fd);
    }
    if (!CloseExcept(keep)) ChildError(error_fd);
    execve(args[0], args.data(), environment.data());
    ChildError(error_fd);
  }
  write_error.Reset();
  child->identity.pid = pid;
  // No other thread/reaper is allowed in Guardian. The direct child cannot
  // have its PID recycled until we reap it, even if exec already failed.
  ProcessRecord record;
  status = ReadProcess(pid, &record);
  if (status.ok()) child->identity = record.identity;
  OwnedFd pidfd;
  if (status.ok()) status = OpenProcess(record.identity, &pidfd);
  if (!status.ok()) {
    // This is only the just-created unreaped child, never a historical PID.
    kill(pid, SIGKILL);
    return Status::Fail("cannot pin newly spawned child; cleanup required");
  }
  child->identity = record.identity;
  child->pidfd = std::move(pidfd);
  child->exec_status = std::move(read_error);
  return Status{};
}

Status ReapProcess(int pidfd, bool* reaped, int* exit_code) {
  if (pidfd < 0 || reaped == nullptr || exit_code == nullptr) {
    return Status::Fail("invalid reap output");
  }
  siginfo_t info{};
  int result;
  do {
    result =
        waitid(P_PIDFD, static_cast<id_t>(pidfd), &info, WEXITED | WNOHANG);
  } while (result < 0 && errno == EINTR);
  if (result < 0) return Error("waitid pidfd");
  *reaped = info.si_pid != 0;
  if (*reaped) {
    *exit_code = info.si_code == CLD_EXITED ? info.si_status : -info.si_status;
  }
  return Status{};
}

Status OwnDescendants(std::vector<ProcessRecord>* result) {
  if (result == nullptr) return Status::Fail("missing descendant output");
  result->clear();
  ProcessRecord self;
  const auto self_status = ReadProcess(getpid(), &self);
  if (!self_status.ok()) return self_status;
  DIR* directory = opendir("/proc");
  if (directory == nullptr) return Error("open proc inventory");
  std::unordered_map<pid_t, ProcessRecord> records;
  bool failed = false;
  int read_error = 0;
  while (true) {
    errno = 0;
    const dirent* entry = readdir(directory);
    if (entry == nullptr) {
      read_error = errno;
      break;
    }
    pid_t pid;
    if (!Number(std::string_view(entry->d_name), &pid) || pid <= 0) continue;
    ProcessRecord record;
    bool gone = false;
    const auto status = ReadProcess(pid, &record, &gone);
    if (!status.ok()) {
      if (gone) continue;
      failed = true;
      break;
    }
    records.emplace(pid, record);
    if (records.size() > kMaxProcesses) {
      failed = true;
      break;
    }
  }
  closedir(directory);
  if (failed || read_error) return Status::Fail("incomplete proc inventory");
  const auto found = records.find(self.identity.pid);
  if (found == records.end() || found->second.identity != self.identity) {
    return Status::Fail("Guardian identity changed during inventory");
  }
  std::unordered_multimap<pid_t, pid_t> children;
  for (const auto& [pid, record] : records)
    children.emplace(record.parent, pid);
  std::unordered_set<pid_t> owned{self.identity.pid};
  std::vector<pid_t> queue{self.identity.pid};
  for (size_t index = 0; index < queue.size(); ++index) {
    const pid_t parent = queue[index];
    const auto [begin, end] = children.equal_range(parent);
    for (auto it = begin; it != end; ++it) {
      const auto& record = records.at(it->second);
      if (owned.contains(record.identity.pid) ||
          record.identity.start_ticks < records.at(parent).identity.start_ticks)
        continue;
      owned.insert(record.identity.pid);
      queue.push_back(record.identity.pid);
      result->push_back(record);
      if (result->size() > 4096) {
        result->clear();
        return Status::Fail("owned descendant limit exceeded");
      }
    }
  }
  return Status{};
}

Status NoChildren(bool* empty) {
  if (empty == nullptr) return Status::Fail("missing empty-tree output");
  siginfo_t info{};
  int result;
  do {
    result = waitid(P_ALL, 0, &info, WEXITED | WNOHANG | WNOWAIT);
  } while (result < 0 && errno == EINTR);
  *empty = result < 0 && errno == ECHILD;
  if (result < 0 && errno != ECHILD) return Error("waitid empty tree");
  return Status{};
}

Status CheckInheritedLease(int fd, const std::string& path) {
  if (fd < 3 || path.empty() || path[0] != '/') {
    return Status::Fail("invalid inherited lease");
  }
  struct stat held {
  }, named{};
  if (fstat(fd, &held) != 0) return Error("stat inherited lease");
  OwnedFd probe(open(path.c_str(), O_RDWR | O_CLOEXEC | O_NOFOLLOW));
  if (probe.Get() < 0 || fstat(probe.Get(), &named) != 0) {
    return Error("open named lease");
  }
  if (!S_ISREG(held.st_mode) || held.st_nlink != 1 ||
      held.st_dev != named.st_dev || held.st_ino != named.st_ino ||
      (fcntl(fd, F_GETFL) & O_ACCMODE) != O_RDWR) {
    return Status::Fail("inherited lease inode or mode mismatch");
  }
  // /proc fdinfo reports locks belonging to THIS open file description. An
  // independently opened FD for the same inode has no such entry. Inspecting
  // it avoids reacquiring an accidentally unlocked description as a check.
  const std::string info_path = "/proc/self/fdinfo/" + std::to_string(fd);
  const OwnedFd info(open(info_path.c_str(), O_RDONLY | O_CLOEXEC));
  if (info.Get() < 0) return Error("open lease fdinfo");
  std::array<char, 8192> bytes{};
  ssize_t count;
  do {
    count = read(info.Get(), bytes.data(), bytes.size());
  } while (count < 0 && errno == EINTR);
  if (count <= 0 || count == static_cast<ssize_t>(bytes.size())) {
    return Status::Fail("lease fdinfo unavailable or oversized");
  }
  std::istringstream lines(
      std::string(bytes.data(), static_cast<size_t>(count)));
  std::string line;
  bool found_lock = false;
  while (std::getline(lines, line)) {
    if (!line.starts_with("lock:")) continue;
    std::istringstream fields(line);
    std::array<std::string, 9> values;
    for (auto& value : values) fields >> value;
    std::string extra;
    if (fields >> extra || values[2] != "FLOCK" || values[3] != "ADVISORY" ||
        values[4] != "WRITE" || values[7] != "0" || values[8] != "EOF" ||
        found_lock) {
      return Status::Fail("unexpected lease fdinfo lock");
    }
    found_lock = true;
  }
  if (!found_lock) return Status::Fail("FD does not own inherited flock");
  if (flock(probe.Get(), LOCK_EX | LOCK_NB) == 0) {
    // Closing this independent probe releases only its description. Never
    // issue LOCK_UN on fd: children share the parent's open description.
    return Status::Fail("inherited lease is not continuously held");
  }
  if (errno != EWOULDBLOCK && errno != EAGAIN) return Error("probe lease");
  return Status{};
}

bool LaunchGate::Begin(uint64_t request, uint64_t controller_generation) {
  if (stopping_ || frozen_ || request_ || request <= last_request_ ||
      !controller_generation || controller_generation < controller_generation_)
    return false;
  request_ = request;
  last_request_ = request;
  controller_generation_ = controller_generation;
  return true;
}
bool LaunchGate::Consume(uint64_t request, uint64_t controller_generation) {
  if (stopping_ || frozen_ || !request_ || request != request_ ||
      controller_generation != controller_generation_)
    return false;
  request_ = 0;
  return true;
}
void LaunchGate::Stop() {
  stopping_ = true;
  request_ = 0;
}
void LaunchGate::Freeze() {
  frozen_ = true;
  request_ = 0;
}

}  // namespace q4t::service
