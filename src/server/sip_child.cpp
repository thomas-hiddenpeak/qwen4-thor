#include "sip_child.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <thread>

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
bool Reaped(pid_t pid, Clock::time_point deadline) {
  do {
    int result = waitpid(pid, nullptr, WNOHANG);
    if (result == pid || (result < 0 && errno == ECHILD)) return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  } while (Clock::now() < deadline);
  return false;
}
}  // namespace

SipChild::~SipChild() {
  CloseChannel();
  if (pid_ <= 0 || Reaped(pid_, Clock::now() + std::chrono::milliseconds(150)))
    return;
  kill(pid_, SIGKILL);
  // A direct child that is still tearing down retains its wait owner, without
  // extending model shutdown indefinitely.
  if (!Reaped(pid_, Clock::now() + std::chrono::milliseconds(350))) {
    std::thread([pid = pid_] {
      while (waitpid(pid, nullptr, 0) < 0 && errno == EINTR) {
      }
    }).detach();
  }
}

void SipChild::CloseChannel() {
  if (fd_ >= 0) close(fd_);
  fd_ = -1;
}

bool SipChild::Send(const std::string& message) const {
  ssize_t n;
  do {
    n = send(fd_, message.data(), message.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
  } while (n < 0 && errno == EINTR);
  return n == static_cast<ssize_t>(message.size());
}

bool SipChild::Ready(const std::string& expected) {
  pollfd event{fd_, POLLIN, 0};
  const auto until = Clock::now() + std::chrono::seconds(3);
  while (Clock::now() < until) {
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        until - Clock::now())
                        .count();
    const int result = poll(&event, 1, static_cast<int>(ms));
    if (result < 0 && errno == EINTR) continue;
    if (result <= 0) return false;
    char bytes[256];
    auto n = recv(fd_, bytes, sizeof(bytes), MSG_DONTWAIT | MSG_TRUNC);
    if (n <= 0 || size_t(n) > sizeof(bytes)) return false;
    const auto value = std::string_view(bytes, n);
    return value == expected;
  }
  return false;
}

bool SipChild::Start(const std::string& program, std::vector<std::string> args,
                     int webhook_fd) {
  if (fd_ >= 0 || pid_ >= 0) return false;
  int pair[2];
  if (socketpair(AF_UNIX, SOCK_CLOEXEC | SOCK_SEQPACKET, 0, pair)) return false;
  fd_ = pair[0];
  const int child = fcntl(pair[1], F_DUPFD_CLOEXEC, 5);
  close(pair[1]);
  if (child < 0) return false;
  const int hook = webhook_fd < 0 ? -1 : fcntl(webhook_fd, F_DUPFD_CLOEXEC, 5);
  posix_spawn_file_actions_t actions;
  int error = posix_spawn_file_actions_init(&actions);
  if (!error) {
    error = posix_spawn_file_actions_adddup2(&actions, child, 3);
    if (!error && webhook_fd >= 0)
      error = hook < 0 ? EBADF
                       : posix_spawn_file_actions_adddup2(&actions, hook, 4);
    if (!error)
      error =
          posix_spawn_file_actions_addclosefrom_np(&actions, hook >= 0 ? 5 : 4);
    if (!error) {
      std::vector<char*> argv{const_cast<char*>(program.c_str())};
      for (auto& value : args) argv.push_back(value.data());
      argv.push_back(nullptr);
      char locale[] = "LC_ALL=C", allocator[] = "MALLOC_ARENA_MAX=2";
      char* env[] = {locale, allocator, nullptr};
      pid_t pid;
      error = posix_spawn(&pid, program.c_str(), &actions, nullptr, argv.data(),
                          env);
      if (!error) pid_ = pid;
    }
    posix_spawn_file_actions_destroy(&actions);
  }
  if (hook >= 0) close(hook);
  close(child);
  return error == 0;
}
}  // namespace q4t::server
