#include "q4t/service/ingress_admin.h"

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>

#include "q4t/service/guardian_wire.h"

namespace q4t::service::ingress {

bool IngressAdmin::Open(const std::string& address) {
  sockaddr_un target{};
  if (address.empty() || address.size() >= sizeof(target.sun_path))
    return false;
  target.sun_family = AF_UNIX;
  std::memcpy(target.sun_path + 1, address.data(), address.size());
  listener_ = OwnedFd(
      ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
  return listener_.Get() >= 0 &&
         ::bind(listener_.Get(), reinterpret_cast<sockaddr*>(&target),
                offsetof(sockaddr_un, sun_path) + address.size() + 1) == 0 &&
         ::listen(listener_.Get(), 8) == 0;
}

void IngressAdmin::Step(Credentials* credentials, ClientAccess* access) {
  const auto now = std::chrono::steady_clock::now();
  // One accepted peer and at most eight tiny requests per event-loop turn.
  OwnedFd accepted(::accept4(listener_.Get(), nullptr, nullptr,
                             SOCK_NONBLOCK | SOCK_CLOEXEC));
  if (accepted.Get() >= 0) {
    ucred identity{};
    socklen_t size = sizeof(identity);
    auto free =
        std::find_if(peers_.begin(), peers_.end(),
                     [](const Peer& peer) { return peer.fd.Get() < 0; });
    if (free != peers_.end() &&
        ::getsockopt(accepted.Get(), SOL_SOCKET, SO_PEERCRED, &identity,
                     &size) == 0 &&
        size == sizeof(identity) && identity.uid == ::geteuid()) {
      free->fd = std::move(accepted);
      free->deadline = now + std::chrono::seconds(1);
    }
  }
  for (auto& peer : peers_) {
    if (peer.fd.Get() < 0) continue;
    if (now >= peer.deadline) {
      peer.fd = {};
      continue;
    }
    char buffer[512];
    const auto n =
        ::recv(peer.fd.Get(), buffer, sizeof(buffer), MSG_DONTWAIT | MSG_TRUNC);
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    io::Json command;
    if (n > 0 && n <= ssize_t(sizeof(buffer)) &&
        wire::Parse(std::string(buffer, n), &command) &&
        wire::HasKeys(command, {"op"})) {
      const auto op = command.GetString("op");
      if (op == "access-status") {
        const auto status =
            access ? access->Describe()
                   : wire::Object({{"configured", wire::Boolean(false)}});
        std::string reply;
        if (wire::Encode(status, &reply))
          ::send(peer.fd.Get(), reply.data(), reply.size(),
                 MSG_NOSIGNAL | MSG_DONTWAIT);
      }
      if (op == "status" || op == "reload") {
        const auto before =
            credentials ? credentials->Status().GetInt("load_attempt") : 0;
        if (credentials && op == "reload") credentials->Step(true);
        auto status = credentials
                          ? credentials->Status()
                          : wire::Object({{"enabled", wire::Boolean(false)}});
        if (op == "reload")
          status.object.emplace_back(
              "reload_accepted",
              wire::Boolean(status.GetInt("load_attempt") > before));
        std::string reply;
        if (wire::Encode(status, &reply))
          ::send(peer.fd.Get(), reply.data(), reply.size(),
                 MSG_NOSIGNAL | MSG_DONTWAIT);
      }
    }
    peer.fd = {};
  }
}
}  // namespace q4t::service::ingress
