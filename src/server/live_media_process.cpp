#include "q4t/server/live_media_process.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <thread>
#include <vector>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using namespace object_detail;
// A child stuck in kernel teardown retains its slot until waitpid confirms
// exit. Only our own direct children can occupy these entries.
std::mutex slots_mutex;
std::array<pid_t, 4> slots{};
int Reserve() {
  std::lock_guard lock(slots_mutex);
  for (size_t i = 0; i < slots.size(); ++i) {
    if (slots[i] > 0 && (waitpid(slots[i], nullptr, WNOHANG) == slots[i]))
      slots[i] = 0;
    if (slots[i] == 0) {
      slots[i] = -1;
      return static_cast<int>(i);
    }
  }
  return -1;
}

bool Reaped(pid_t pid, Clock::time_point deadline) {
  do {
    const auto result = waitpid(pid, nullptr, WNOHANG);
    if (result == pid || (result < 0 && errno == ECHILD)) return true;
    if (result < 0 && errno != EINTR) return false;
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  } while (Clock::now() < deadline);
  return false;
}
ObjectReply Unavailable() {
  return Error(503, "live_media_unavailable",
               "WebRTC media worker unavailable");
}
}  // namespace

LiveMediaProcess::~LiveMediaProcess() {
  if (fd_ >= 0) close(fd_);
  if (pid_ > 0 &&
      !Reaped(pid_, Clock::now() + std::chrono::milliseconds(250))) {
    // No other code reaps our owned pid, which cannot have been recycled.
    kill(pid_, SIGKILL);
    if (Reaped(pid_, Clock::now() + std::chrono::milliseconds(250))) pid_ = -1;
  } else {
    pid_ = -1;
  }
  if (slot_ >= 0) {
    std::lock_guard lock(slots_mutex);
    slots[slot_] = std::max(0, pid_);
  }
}

bool LiveMediaProcess::Send(char type, std::string_view payload) {
  if (fd_ < 0 || payload.size() >= buffer_.size()) return false;
  iovec parts[] = {{&type, 1},
                   {const_cast<char*>(payload.data()), payload.size()}};
  msghdr msg{};
  msg.msg_iov = parts;
  msg.msg_iovlen = payload.empty() ? 1 : 2;
  return sendmsg(fd_, &msg, MSG_NOSIGNAL | MSG_DONTWAIT) ==
         static_cast<ssize_t>(payload.size() + 1);
}

LiveMediaProcess::Read LiveMediaProcess::Receive(char* type,
                                                 std::string_view* payload) {
  const auto n =
      recv(fd_, buffer_.data(), buffer_.size(), MSG_DONTWAIT | MSG_TRUNC);
  if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
    return Read::kPending;
  if (n <= 0 || static_cast<size_t>(n) > buffer_.size()) return Read::kClosed;
  *type = buffer_[0];
  *payload = {buffer_.data() + 1, static_cast<size_t>(n - 1)};
  return Read::kPacket;
}

ObjectReply LiveMediaProcess::Negotiate(std::string_view offer,
                                        const std::string& address,
                                        const std::function<bool()>& cancelled,
                                        std::string* answer) {
  if (offer.empty() || offer.size() >= buffer_.size() ||
      offer.find('\0') != offer.npos)
    return Error(400, "live_invalid_sdp", "Invalid WebRTC session description");
  if (!answer || !cancelled || slot_ >= 0) return Unavailable();
  struct sigaction disposition {};
  if (sigaction(SIGCHLD, nullptr, &disposition) ||
      disposition.sa_handler != SIG_DFL ||
      (disposition.sa_flags & SA_NOCLDWAIT))
    return Unavailable();
  slot_ = Reserve();
  if (slot_ < 0)
    return Error(429, "live_media_capacity", "Media sessions at capacity");
  std::error_code ec;
  const auto engine = std::filesystem::read_symlink("/proc/self/exe", ec);
  if (ec || !engine.is_absolute()) return Unavailable();
  const auto path = engine.parent_path() / "q4t-live-webrtc";
  int pair[2];
  if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair))
    return Unavailable();
  fd_ = pair[0];
  const int child = fcntl(pair[1], F_DUPFD_CLOEXEC, 4);
  close(pair[1]);
  if (child < 0) return Unavailable();
  posix_spawn_file_actions_t actions;
  if (posix_spawn_file_actions_init(&actions)) {
    close(child);
    return Unavailable();
  }
  int error = posix_spawn_file_actions_adddup2(&actions, child, 3);
  if (!error) error = posix_spawn_file_actions_addclosefrom_np(&actions, 4);
  char name[] = "q4t-live-webrtc", socket[] = "3";
  char* argv[] = {name, socket, const_cast<char*>(address.c_str()), nullptr};
  // Only media-specific deployment settings reach this child; service keys,
  // TLS credentials and model-library loader settings are never inherited.
  std::vector<std::string> options{"LC_ALL=C", "MALLOC_ARENA_MAX=2"};
  for (const auto* key : {"GST_PLUGIN_PATH_1_0", "GST_REGISTRY_1_0"})
    if (const char* value = std::getenv(key); value && *value)
      options.emplace_back(std::string(key) + "=" + value);
  std::vector<char*> env;
  for (auto& value : options) env.push_back(value.data());
  env.push_back(nullptr);
  pid_t pid = -1;
  if (!error)
    error =
        posix_spawn(&pid, path.c_str(), &actions, nullptr, argv, env.data());
  posix_spawn_file_actions_destroy(&actions);
  close(child);
  if (error) return Unavailable();
  pid_ = pid;
  if (!Send('O', offer)) return Unavailable();
  const auto until = Clock::now() + std::chrono::seconds(15);
  while (Clock::now() < until && !cancelled()) {
    char type;
    std::string_view payload;
    switch (Receive(&type, &payload)) {
      case Read::kClosed:
        return Unavailable();
      case Read::kPacket:
        if (type == 'I')
          return Error(400, "live_invalid_sdp", "Unusable WebRTC offer");
        if (type != 'A' || payload.empty()) return Unavailable();
        answer->assign(payload);
        return {};
      case Read::kPending:
        break;
    }
    pollfd socket_event{fd_, POLLIN, 0};
    if (poll(&socket_event, 1, 25) < 0 && errno != EINTR) return Unavailable();
  }
  return Unavailable();
}
}  // namespace q4t::server
