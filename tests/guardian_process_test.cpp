#include "q4t/service/guardian_process.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <thread>

#include "q4t/test.h"

namespace {
namespace s = q4t::service;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
std::string executable;
std::string output;

bool Wait(s::ChildProcess& child, int expected) {
  const auto end = Clock::now() + 3s;
  while (Clock::now() < end) {
    bool reaped = false;
    int code = 999;
    if (!s::ReapProcess(child.pidfd.Get(), &reaped, &code).ok()) return false;
    if (reaped) return code == expected;
    std::this_thread::sleep_for(2ms);
  }
  return false;
}

bool CleanAll() {
  const auto end = Clock::now() + 3s;
  while (Clock::now() < end) {
    std::vector<s::ProcessRecord> owned;
    if (!s::OwnDescendants(&owned).ok()) return false;
    for (const auto& record : owned) {
      s::OwnedFd fd;
      if (!s::OpenProcess(record.identity, &fd).ok()) continue;
      if (!s::SignalProcess(fd.Get(), SIGKILL).ok()) return false;
      bool reaped = false;
      int code = 0;
      // Descendants still parented by another dying process are not waitable
      // yet. Keep them in the next snapshot until adopted and actually reaped.
      s::ReapProcess(fd.Get(), &reaped, &code);
    }
    bool empty = false;
    if (!s::NoChildren(&empty).ok()) return false;
    if (empty) return true;
    std::this_thread::sleep_for(2ms);
  }
  return false;
}

struct Cleanup {
  ~Cleanup() { CleanAll(); }
};

class LeaseFile {
 public:
  LeaseFile() {
    std::string pattern = output + "/lease-XXXXXX";
    fd.Reset(mkstemp(pattern.data()));
    path = pattern;
  }
  ~LeaseFile() { unlink(path.c_str()); }
  s::OwnedFd fd;
  std::string path;
};

int Fixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const std::string mode(argv[1]);
  if (mode == "--sleep") {
    for (;;) pause();
  }
  if (mode == "--exit") return std::atoi(argv[2]);
  if (mode == "--fd-check" && argc == 4) {
    return fcntl(std::atoi(argv[2]), F_GETFD) >= 0 &&
                   fcntl(std::atoi(argv[3]), F_GETFD) < 0
               ? 0
               : 1;
  }
  if ((mode == "--close-lease" || mode == "--unlock-lease") && argc == 4) {
    const int fd = std::atoi(argv[2]);
    if (!s::CheckInheritedLease(fd, argv[3]).ok()) return 1;
    if (mode == "--unlock-lease" && flock(fd, LOCK_UN) != 0) return 2;
    close(fd);
    return 0;
  }
  if (mode == "--pdeath-parent" && argc == 3) {
    s::ChildProcess child;
    if (!s::SpawnProcess({executable, "--sleep"}, {}, {}, &child).ok())
      return 2;
    const pid_t pid = child.identity.pid;
    if (write(std::atoi(argv[2]), &pid, sizeof(pid)) !=
        static_cast<ssize_t>(sizeof(pid)))
      return 3;
    for (;;) pause();
  }
  if (mode == "--escape-tree" && argc == 3) {
    const pid_t middle = fork();
    if (middle < 0) return 2;
    if (middle == 0) {
      if (setsid() < 0) _exit(3);
      const pid_t leaf = fork();
      if (leaf < 0) _exit(4);
      if (leaf == 0) {
        const pid_t pid = getpid();
        if (write(std::atoi(argv[2]), &pid, sizeof(pid)) !=
            static_cast<ssize_t>(sizeof(pid)))
          _exit(5);
        for (;;) pause();
      }
      _exit(0);
    }
    return 0;
  }
  return -1;
}

bool ReadPid(int fd, pid_t* pid) {
  pollfd item{fd, POLLIN, 0};
  return poll(&item, 1, 3000) > 0 &&
         read(fd, pid, sizeof(*pid)) == static_cast<ssize_t>(sizeof(*pid));
}
}  // namespace

Q4T_TEST(guardian_process_identity_rejects_pid_reuse_and_handles_comm) {
  s::ProcessRecord self;
  Q4T_CHECK(prctl(PR_SET_NAME, "q4t ) ( test", 0, 0, 0) == 0);
  Q4T_CHECK(s::ReadProcess(getpid(), &self).ok());
  Q4T_CHECK(self.identity.pid == getpid());
  s::OwnedFd fd;
  auto stale = self.identity;
  ++stale.start_ticks;
  Q4T_CHECK(!s::OpenProcess(stale, &fd).ok());
  Q4T_CHECK(s::OpenProcess(self.identity, &fd).ok());
  Q4T_CHECK(s::SignalProcess(fd.Get(), 0).ok());
  return true;
}

Q4T_TEST(guardian_spawn_exit_reaps_pinned_child) {
  Cleanup cleanup;
  s::ChildProcess child;
  Q4T_CHECK(s::SpawnProcess({executable, "--exit", "17"}, {}, {}, &child).ok());
  Q4T_CHECK(Wait(child, 17));
  bool empty = false;
  Q4T_CHECK(s::NoChildren(&empty).ok() && empty);
  return true;
}

Q4T_TEST(guardian_child_has_only_explicit_inherited_descriptors) {
  Cleanup cleanup;
  s::OwnedFd keep(open("/dev/null", O_RDWR | O_CLOEXEC));
  s::OwnedFd secret(open("/dev/null", O_RDWR));  // Deliberately no CLOEXEC.
  Q4T_CHECK(keep.Get() >= 3 && secret.Get() >= 3);
  s::ChildProcess child;
  Q4T_CHECK(
      s::SpawnProcess({executable, "--fd-check", std::to_string(keep.Get()),
                       std::to_string(secret.Get())},
                      {}, {keep.Get()}, &child)
          .ok());
  Q4T_CHECK(Wait(child, 0));
  Q4T_CHECK(fcntl(keep.Get(), F_GETFD) & FD_CLOEXEC);
  Q4T_CHECK(fcntl(secret.Get(), F_GETFD) >= 0);
  return true;
}

Q4T_TEST(guardian_spawn_rejects_unbounded_and_ambiguous_specifications) {
  s::ChildProcess child;
  Q4T_CHECK(!s::SpawnProcess({"true"}, {}, {}, &child).ok());
  Q4T_CHECK(!s::SpawnProcess({executable}, {"A=1", "A=2"}, {}, &child).ok());
  Q4T_CHECK(
      !s::SpawnProcess({executable, std::string(8193, 'x')}, {}, {}, &child)
           .ok());
  Q4T_CHECK(!s::SpawnProcess({executable}, {}, {0}, &child).ok());
  bool empty = false;
  Q4T_CHECK(s::NoChildren(&empty).ok() && empty);
  return true;
}

Q4T_TEST(guardian_exec_failure_is_reported_without_blocking_wait) {
  Cleanup cleanup;
  s::ChildProcess child;
  Q4T_CHECK(
      s::SpawnProcess({"/q4t-nonexistent-test-command"}, {}, {}, &child).ok());
  pollfd item{child.exec_status.Get(), POLLIN, 0};
  Q4T_CHECK(poll(&item, 1, 3000) > 0);
  int error = 0;
  Q4T_CHECK(read(item.fd, &error, sizeof(error)) ==
            static_cast<ssize_t>(sizeof(error)));
  Q4T_CHECK(error == ENOENT);
  Q4T_CHECK(Wait(child, 127));
  return true;
}

Q4T_TEST(guardian_live_child_is_not_reaped_or_reported_as_empty) {
  Cleanup cleanup;
  s::ChildProcess child;
  Q4T_CHECK(s::SpawnProcess({executable, "--sleep"}, {}, {}, &child).ok());
  const auto identity = child.identity;
  Q4T_CHECK(!s::SpawnProcess({executable, "--exit", "0"}, {}, {}, &child).ok());
  Q4T_CHECK(child.identity == identity);
  bool reaped = false, exited = false, empty = true;
  int code = 0;
  Q4T_CHECK(s::ReapProcess(child.pidfd.Get(), &reaped, &code).ok() && !reaped);
  Q4T_CHECK(s::ProcessExited(child.pidfd.Get(), &exited).ok() && !exited);
  Q4T_CHECK(s::NoChildren(&empty).ok() && !empty);
  Q4T_CHECK(s::SignalProcess(child.pidfd.Get(), SIGKILL).ok());
  Q4T_CHECK(Wait(child, -SIGKILL));
  return true;
}

Q4T_TEST(guardian_parent_death_terminates_and_reaps_exec_descendant) {
  Cleanup cleanup;
  int pipe[2];
  Q4T_CHECK(pipe2(pipe, O_CLOEXEC) == 0);
  s::OwnedFd input(pipe[0]), report(pipe[1]);
  s::ChildProcess parent;
  Q4T_CHECK(s::SpawnProcess(
                {executable, "--pdeath-parent", std::to_string(report.Get())},
                {}, {report.Get()}, &parent)
                .ok());
  report.Reset();
  pid_t pid = -1;
  Q4T_CHECK(ReadPid(input.Get(), &pid));
  s::ProcessRecord record;
  Q4T_CHECK(s::ReadProcess(pid, &record).ok());
  s::ChildProcess adopted;
  adopted.identity = record.identity;
  Q4T_CHECK(s::OpenProcess(record.identity, &adopted.pidfd).ok());
  Q4T_CHECK(s::SignalProcess(parent.pidfd.Get(), SIGKILL).ok());
  Q4T_CHECK(Wait(parent, -SIGKILL));
  Q4T_CHECK(Wait(adopted, -SIGTERM));
  bool empty = false;
  Q4T_CHECK(s::NoChildren(&empty).ok() && empty);
  return true;
}

Q4T_TEST(guardian_orphan_reaping_finds_double_fork_outside_original_group) {
  Cleanup cleanup;
  int pipe[2];
  Q4T_CHECK(pipe2(pipe, O_CLOEXEC) == 0);
  s::OwnedFd input(pipe[0]), report(pipe[1]);
  s::ChildProcess parent;
  Q4T_CHECK(s::SpawnProcess(
                {executable, "--escape-tree", std::to_string(report.Get())}, {},
                {report.Get()}, &parent)
                .ok());
  report.Reset();
  pid_t leaf = -1;
  Q4T_CHECK(ReadPid(input.Get(), &leaf));
  Q4T_CHECK(Wait(parent, 0));
  std::vector<s::ProcessRecord> owned;
  Q4T_CHECK(s::OwnDescendants(&owned).ok());
  bool found = false;
  for (const auto& record : owned) {
    if (record.identity.pid == leaf) {
      found = true;
      Q4T_CHECK(record.group != parent.identity.pid);
    }
  }
  Q4T_CHECK(found);
  Q4T_CHECK(CleanAll());
  bool empty = false;
  Q4T_CHECK(s::NoChildren(&empty).ok() && empty);
  return true;
}

Q4T_TEST(guardian_inherited_lease_survives_child_close) {
  Cleanup cleanup;
  LeaseFile lease;
  Q4T_CHECK(lease.fd.Get() >= 3);
  Q4T_CHECK(flock(lease.fd.Get(), LOCK_EX | LOCK_NB) == 0);
  Q4T_CHECK(s::CheckInheritedLease(lease.fd.Get(), lease.path).ok());
  s::ChildProcess child;
  Q4T_CHECK(s::SpawnProcess({executable, "--close-lease",
                             std::to_string(lease.fd.Get()), lease.path},
                            {}, {lease.fd.Get()}, &child)
                .ok());
  Q4T_CHECK(Wait(child, 0));
  Q4T_CHECK(s::CheckInheritedLease(lease.fd.Get(), lease.path).ok());
  return true;
}

Q4T_TEST(guardian_negative_child_unlock_is_detected_and_never_reacquired) {
  Cleanup cleanup;
  LeaseFile lease;
  Q4T_CHECK(lease.fd.Get() >= 3);
  Q4T_CHECK(flock(lease.fd.Get(), LOCK_EX | LOCK_NB) == 0);
  s::ChildProcess child;
  Q4T_CHECK(s::SpawnProcess({executable, "--unlock-lease",
                             std::to_string(lease.fd.Get()), lease.path},
                            {}, {lease.fd.Get()}, &child)
                .ok());
  Q4T_CHECK(Wait(child, 0));
  Q4T_CHECK(!s::CheckInheritedLease(lease.fd.Get(), lease.path).ok());
  s::OwnedFd contender(open(lease.path.c_str(), O_RDWR));
  Q4T_CHECK(flock(contender.Get(), LOCK_EX | LOCK_NB) == 0);
  return true;
}

Q4T_TEST(guardian_new_description_cannot_borrow_someone_elses_lease) {
  LeaseFile lease;
  Q4T_CHECK(lease.fd.Get() >= 3);
  Q4T_CHECK(flock(lease.fd.Get(), LOCK_EX | LOCK_NB) == 0);
  s::OwnedFd impostor(open(lease.path.c_str(), O_RDWR));
  Q4T_CHECK(!s::CheckInheritedLease(impostor.Get(), lease.path).ok());
  Q4T_CHECK(s::CheckInheritedLease(lease.fd.Get(), lease.path).ok());
  return true;
}

Q4T_TEST(guardian_lease_path_replacement_does_not_transfer_ownership) {
  LeaseFile lease, other;
  Q4T_CHECK(lease.fd.Get() >= 3 && other.fd.Get() >= 3);
  Q4T_CHECK(flock(lease.fd.Get(), LOCK_EX | LOCK_NB) == 0);
  Q4T_CHECK(rename(other.path.c_str(), lease.path.c_str()) == 0);
  Q4T_CHECK(!s::CheckInheritedLease(lease.fd.Get(), lease.path).ok());
  return true;
}

Q4T_TEST(guardian_stop_wins_over_late_persisted_launch_permission) {
  s::LaunchGate gate;
  Q4T_CHECK(gate.Begin(1, 1));
  gate.Stop();
  Q4T_CHECK(!gate.Consume(1, 1));
  Q4T_CHECK(!gate.Begin(2, 2));
  Q4T_CHECK(gate.Stopping());
  return true;
}

Q4T_TEST(guardian_frozen_journal_never_grants_new_launch) {
  s::LaunchGate gate;
  Q4T_CHECK(gate.Begin(1, 1));
  gate.Freeze();
  Q4T_CHECK(!gate.Consume(1, 1));
  Q4T_CHECK(!gate.Begin(2, 1));
  Q4T_CHECK(gate.Frozen() && !gate.Stopping());
  return true;
}

Q4T_TEST(guardian_stale_or_duplicate_receipt_cannot_launch_twice) {
  s::LaunchGate gate;
  Q4T_CHECK(gate.Begin(1, 2));
  Q4T_CHECK(!gate.Begin(2, 2));
  Q4T_CHECK(!gate.Consume(1, 1));
  Q4T_CHECK(gate.Consume(1, 2));
  Q4T_CHECK(!gate.Consume(1, 2));
  Q4T_CHECK(!gate.Begin(1, 2));
  Q4T_CHECK(!gate.Begin(2, 1));
  Q4T_CHECK(gate.Begin(2, 3));
  Q4T_CHECK(gate.Consume(2, 3));
  return true;
}

int main(int argc, char** argv) {
  executable = std::filesystem::canonical("/proc/self/exe").string();
  const int fixture = Fixture(argc, argv);
  if (fixture >= 0) return fixture;
  if (argc != 1 || !s::EnableSubreaper().ok()) return 2;
  const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (directory == nullptr) return 2;
  output = directory;
  std::filesystem::create_directories(output);
  std::vector<std::string> names;
  for (const auto& test : q4t::test::Registry()) names.push_back(test.name);
  const int result = q4t::test::RunTests(names, false);
  return CleanAll() ? result : 1;
}
