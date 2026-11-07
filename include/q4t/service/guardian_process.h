// Linux-only CPU process primitives for the standalone Guardian.
// No CUDA, NVML, systemd, Python embedding or serving library dependencies.
#pragma once

#include <sys/types.h>

#include <cstdint>
#include <string>
#include <vector>

#include "q4t/status.h"

namespace q4t::service {

class OwnedFd {
 public:
  OwnedFd() = default;
  explicit OwnedFd(int fd) : fd_(fd) {}
  ~OwnedFd();
  OwnedFd(const OwnedFd&) = delete;
  OwnedFd& operator=(const OwnedFd&) = delete;
  OwnedFd(OwnedFd&& other) noexcept;
  OwnedFd& operator=(OwnedFd&& other) noexcept;
  int Get() const { return fd_; }
  int Release();
  void Reset(int fd = -1);

 private:
  int fd_ = -1;
};

struct ProcessIdentity {
  pid_t pid = -1;
  uint64_t start_ticks = 0;
  bool operator==(const ProcessIdentity&) const = default;
};

struct ProcessRecord {
  ProcessIdentity identity;
  pid_t parent = -1;
  pid_t group = -1;
  char state = '?';
};

// /proc disappearance is reported as failure, never an invented identity.
Status ReadProcess(pid_t pid, ProcessRecord* record, bool* gone = nullptr);
Status OpenProcess(const ProcessIdentity& expected, OwnedFd* pidfd);
Status SignalProcess(int pidfd, int signal);
Status ProcessExited(int pidfd, bool* exited);
Status EnableSubreaper();

struct ChildProcess {
  ProcessIdentity identity;
  OwnedFd pidfd;
  OwnedFd exec_status;
};

// argv[0] is absolute. env is the complete, explicit environment. Only the
// named FDs (>= 3) and stdio survive exec; original parent flags are untouched.
// This function must run in the single-threaded Guardian, never in the model.
// Exec completion is asynchronous. A child losing its parent gets SIGTERM,
// including across exec; credentials cannot be elevated by the executable.
Status SpawnProcess(const std::vector<std::string>& argv,
                    const std::vector<std::string>& env,
                    const std::vector<int>& pass_fds, ChildProcess* child);

// Reap only an actual direct/adopted child pinned by this pidfd. Never wait
// without WNOHANG. A kill request is not proof that the process has exited.
Status ReapProcess(int pidfd, bool* reaped, int* exit_code);

// Return all current descendants of the calling Guardian, including adopted
// orphans that escaped their original process group/session. The caller must
// be a subreaper and must freeze launches before interpreting an empty tree.
// Unknown/unreadable ancestry or limits are failure, never empty success.
Status OwnDescendants(std::vector<ProcessRecord>* result);
Status NoChildren(bool* empty);

// Validate an inherited shared flock description without issuing LOCK_UN.
// This is a supplementary ownership check, not proof of no inter-poll GPU use.
Status CheckInheritedLease(int fd, const std::string& path);

// Memory-side stop priority. A late disk reply cannot reopen launch permission.
// A restarted Guardian never reuses an old pending request; it reconciles the
// persistent grant before opening a new one. Freeze is sticky in this instance.
class LaunchGate {
 public:
  bool Begin(uint64_t request, uint64_t controller_generation);
  bool Consume(uint64_t request, uint64_t controller_generation);
  void Stop();
  void Freeze();
  bool Stopping() const { return stopping_; }
  bool Frozen() const { return frozen_; }

 private:
  bool stopping_ = false;
  bool frozen_ = false;
  uint64_t request_ = 0;
  uint64_t controller_generation_ = 0;
  uint64_t last_request_ = 0;
};

}  // namespace q4t::service
