#include "q4t/server/state_key_admin.h"

#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <filesystem>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
constexpr size_t kRequestBytes = 4096;
constexpr size_t kReplyBytes = 128 * 1024;
socklen_t Address(const std::string& directory, sockaddr_un* address) {
  const auto name = StateKeyAdminAddress(directory);
  if (name.empty() || name.size() + 1 > sizeof(address->sun_path)) return 0;
  *address = {};
  address->sun_family = AF_UNIX;
  std::memcpy(address->sun_path + 1, name.data(), name.size());
  return offsetof(sockaddr_un, sun_path) + 1 + name.size();
}
bool OwnedPeer(int fd) {
  ucred peer{};
  socklen_t size = sizeof(peer);
  return getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &size) == 0 &&
         size == sizeof(peer) && peer.pid > 1 && peer.uid == geteuid();
}
bool Wait(int fd, short events, Clock::time_point deadline) {
  while (Clock::now() < deadline) {
    pollfd descriptor{fd, events, 0};
    const auto remaining =
        std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now())
            .count();
    const int result =
        poll(&descriptor, 1, std::clamp<int64_t>(remaining, 0, 100));
    if (result < 0 && errno == EINTR) continue;
    if (result < 0 || (descriptor.revents & (POLLERR | POLLNVAL))) return false;
    if (result > 0) return (descriptor.revents & events) != 0;
  }
  return false;
}
ObjectReply InvalidCommand() {
  return Error(400, "state_key_command_invalid", "Invalid local key command");
}
ObjectReply Transport(bool submitted) {
  return Error(503, "state_key_admin_unavailable",
               submitted ? "Local key command receipt unavailable; inspect "
                           "keys before retrying"
                         : "Local key administration is unavailable",
               submitted);
}
bool Decode(std::string_view text, Json* value) {
  io::JsonParseLimits limits;
  limits.max_depth = 8;
  limits.max_values = 4096;
  limits.max_string_bytes = kReplyBytes;
  limits.reject_duplicate_keys = true;
  return io::ParseJson(std::string(text), value, true, limits).ok();
}
}  // namespace

std::string StateKeyAdminAddress(const std::string& directory) {
  if (directory.empty() || directory.front() != '/' ||
      directory.size() > 4096 || directory.find('\0') != directory.npos)
    return {};
  const std::filesystem::path path(directory);
  for (const auto& part : path)
    if (part == "." || part == "..") return {};
  auto canonical = path.lexically_normal().string();
  while (canonical.size() > 1 && canonical.back() == '/') canonical.pop_back();
  if (canonical == "/") return {};
  return "q4t.keys." + std::to_string(geteuid()) + '.' + Digest(canonical);
}

ObjectReply HandleStateKeyCommand(ObjectStore& objects, const Json& request,
                                  const StateAccess& access) {
  std::string bad;
  const auto action = request.GetString("action");
  if (action == "retention_inspect" || action == "retention_collect") {
    const auto* version = request.Find("version");
    const auto* cutoff = request.Find("item_created_before");
    const auto* limit = request.Find("limit");
    const auto* expected = request.Find("expected_plan");
    const bool collect = action == "retention_collect";
    if (!InputFields(request,
                     {"version", "action", "item_created_before", "limit",
                      "expected_plan"},
                     &bad, "command") ||
        !version || !IsInputInteger(*version) || version->AsInt() != 1 ||
        !cutoff || !IsInputInteger(*cutoff) || cutoff->number < 0 ||
        cutoff->number > (1LL << 53) - 1 || !limit || !IsInputInteger(*limit) ||
        limit->number < 1 || limit->number > 128 ||
        (collect ? (!expected || !expected->IsString() || expected->str.empty())
                 : expected != nullptr))
      return Error(400, "invalid_retention_command",
                   "Invalid local retention command");
    return objects.Retention(cutoff->AsInt(), limit->AsInt(),
                             collect ? expected->str : "", access.deadline,
                             access.cancelled);
  }
  if (!InputFields(request,
                   {"version", "action", "expected_revision", "retire"}, &bad,
                   "command") ||
      request.GetInt("version") != 1)
    return InvalidCommand();
  if (action == "inspect" || action == "initialize") {
    if (request.Has("expected_revision") || request.Has("retire"))
      return InvalidCommand();
    StateCipher cipher(objects);
    if (action == "initialize") {
      auto ready = cipher.Ready(access);
      if (!ready.ok()) return ready;
    }
    return cipher.Describe(access);
  }
  if (action != "rotate") return InvalidCommand();
  const auto* revision = request.Find("expected_revision");
  const auto* retired = request.GetArray("retire");
  if (!revision || !IsInputInteger(*revision) || revision->number <= 0 ||
      revision->number > (1LL << 53) - 1 || !retired ||
      retired->array.size() > StateCipher::kMaxKeys)
    return InvalidCommand();
  std::vector<std::string> ids;
  for (const auto& key : retired->array) {
    if (!key.IsString() || key.str.size() != 32 ||
        !std::all_of(key.str.begin(), key.str.end(), [](char c) {
          return (c >= 'a' && c <= 'f') || (c >= '0' && c <= '9');
        }))
      return InvalidCommand();
    ids.push_back(key.str);
  }
  return StateCipher(objects).Rotate(revision->AsInt(), ids, access);
}

std::unique_ptr<StateKeyAdmin> StateKeyAdmin::Start(
    ObjectStore& objects, const std::string& directory) noexcept {
  try {
    sockaddr_un address{};
    const auto size = Address(directory, &address);
    if (!size) return {};
    auto admin = std::unique_ptr<StateKeyAdmin>(new StateKeyAdmin(objects));
    admin->listener_.Reset(
        socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
    if (admin->listener_.Get() < 0 ||
        bind(admin->listener_.Get(), reinterpret_cast<sockaddr*>(&address),
             size) != 0 ||
        listen(admin->listener_.Get(), 2) != 0)
      return {};
    admin->alive_.store(true);
    admin->worker_ = std::jthread(
        [owner = admin.get()](std::stop_token stop) { owner->Run(stop); });
    return admin;
  } catch (...) {
    return {};
  }
}
StateKeyAdmin::~StateKeyAdmin() {
  if (worker_.joinable()) {
    worker_.request_stop();
    worker_.join();
  }
}
void StateKeyAdmin::Run(std::stop_token stop) noexcept {
  while (!stop.stop_requested()) {
    try {
      Step(stop);
    } catch (...) {
      // Lose only this command's receipt, never replay its mutation. The one
      // existing operator worker remains available for a later inspect. No
      // auxiliary exception triggers model health/restart or new threads.
      for (auto& peer : peers_) {
        peer.fd.Reset();
        peer.reply.clear();
      }
    }
    // A full accept queue must not turn the loop into a busy spin.
    if (std::any_of(peers_.begin(), peers_.end(),
                    [](const Peer& peer) { return peer.fd.Get() >= 0; })) {
      poll(nullptr, 0, 10);
    } else {
      pollfd listener{listener_.Get(), POLLIN, 0};
      poll(&listener, 1, 100);
    }
  }
  alive_.store(false);
  listener_.Reset();
  for (auto& peer : peers_) peer.fd.Reset();
}
void StateKeyAdmin::Step(std::stop_token stop) {
  for (auto& peer : peers_) {
    if (peer.fd.Get() < 0) {
      service::OwnedFd accepted(accept4(listener_.Get(), nullptr, nullptr,
                                        SOCK_NONBLOCK | SOCK_CLOEXEC));
      if (accepted.Get() < 0) continue;
      if (!OwnedPeer(accepted.Get())) continue;
      peer.fd = std::move(accepted);
      peer.deadline = Clock::now() + 2s;
      peer.reply.clear();
    }
    if (Clock::now() >= peer.deadline) {
      peer.fd.Reset();
      continue;
    }
    if (peer.reply.empty()) {
      std::array<char, kRequestBytes> buffer;
      const auto size = recv(peer.fd.Get(), buffer.data(), buffer.size(),
                             MSG_DONTWAIT | MSG_TRUNC);
      if (size < 0 &&
          (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
        continue;
      if (size <= 0) {
        peer.fd.Reset();
        continue;
      }
      Json request;
      ObjectReply reply;
      if (size > static_cast<ssize_t>(buffer.size()) ||
          !Decode(std::string_view(buffer.data(), size), &request))
        reply = InvalidCommand();
      else
        reply = HandleStateKeyCommand(
            objects_, request, {"local:key-admin", peer.deadline, [stop] {
                                  return stop.stop_requested();
                                }});
      peer.reply = OutputJson(WireReply(reply));
      if (peer.reply.size() > kReplyBytes) {
        peer.fd.Reset();
        continue;
      }
      // One command/receipt per connection. Never replay a lost mutation.
      peer.deadline = Clock::now() + 1s;
    }
    const auto written = send(peer.fd.Get(), peer.reply.data(),
                              peer.reply.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
    if (written < 0 &&
        (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
      continue;
    peer.fd.Reset();
  }
}

ObjectReply ExchangeStateKeyCommand(const std::string& directory,
                                    const Json& request,
                                    Clock::time_point deadline) {
  sockaddr_un address{};
  const auto size = Address(directory, &address);
  const auto wire = OutputJson(request);
  if (!size || wire.size() > kRequestBytes) return InvalidCommand();
  service::OwnedFd fd(
      socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
  if (fd.Get() < 0) return Transport(false);
  if (connect(fd.Get(), reinterpret_cast<sockaddr*>(&address), size) != 0) {
    if (errno != EINPROGRESS || !Wait(fd.Get(), POLLOUT, deadline))
      return Transport(false);
    int error = 0;
    socklen_t length = sizeof(error);
    if (getsockopt(fd.Get(), SOL_SOCKET, SO_ERROR, &error, &length) != 0 ||
        error)
      return Transport(false);
  }
  if (!OwnedPeer(fd.Get())) return Transport(false);
  if (!Wait(fd.Get(), POLLOUT, deadline)) return Transport(false);
  const auto sent =
      send(fd.Get(), wire.data(), wire.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
  if (sent != static_cast<ssize_t>(wire.size())) return Transport(sent >= 0);
  if (!Wait(fd.Get(), POLLIN, deadline)) return Transport(true);
  std::array<char, kReplyBytes> buffer;
  const auto received =
      recv(fd.Get(), buffer.data(), buffer.size(), MSG_DONTWAIT | MSG_TRUNC);
  Json reply;
  if (received <= 0 || received > static_cast<ssize_t>(buffer.size()) ||
      !Decode(std::string_view(buffer.data(), received), &reply))
    return Transport(true);
  return DecodeReply(reply);
}
}  // namespace q4t::server
