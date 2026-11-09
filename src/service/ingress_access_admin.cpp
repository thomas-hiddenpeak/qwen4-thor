#include "q4t/service/ingress_access_admin.h"

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>

#include "q4t/service/guardian_wire.h"

namespace q4t::service::ingress {

bool AccessAdmin::Open(const std::string& address, pid_t owner) {
  sockaddr_un target{};
  if (owner < 2 || address.empty() || address.size() >= sizeof(target.sun_path))
    return false;
  target.sun_family = AF_UNIX;
  std::memcpy(target.sun_path + 1, address.data(), address.size());
  listener_ = OwnedFd(
      ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
  owner_ = owner;
  return listener_.Get() >= 0 &&
         ::bind(listener_.Get(), reinterpret_cast<sockaddr*>(&target),
                offsetof(sockaddr_un, sun_path) + address.size() + 1) == 0 &&
         ::listen(listener_.Get(), 2) == 0;
}

void AccessAdmin::Reply(Peer& peer, const ClientAccess& access, bool ok,
                        const char* error) {
  std::string reply;
  if (wire::Encode(wire::Object({{"ok", wire::Boolean(ok)},
                                 {"error", wire::String(error)},
                                 {"status", access.Describe()}}),
                   &reply))
    ::send(peer.fd.Get(), reply.data(), reply.size(),
           MSG_DONTWAIT | MSG_NOSIGNAL);
  peer = {};
}

void AccessAdmin::Receive(Peer& peer, ClientAccess& access) {
  // Two peers, at most eight 16 KiB packets each per event-loop turn.
  for (int frame = 0; frame < 8 && peer.fd.Get() >= 0; ++frame) {
    std::array<char, 16384> buffer{};
    const auto n = ::recv(peer.fd.Get(), buffer.data(), buffer.size(),
                          MSG_DONTWAIT | MSG_TRUNC);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) return;
    if (n <= 0 || n > ssize_t(buffer.size())) {
      peer = {};
      return;
    }
    if (peer.bytes == 0) {
      io::Json command;
      if (n > 1024 ||
          !wire::Parse(std::string(buffer.data(), n), &command).ok()) {
        Reply(peer, access, false, "access_request_invalid");
        return;
      }
      if (wire::HasKeys(command, {"op"}) &&
          command.GetString("op") == "status") {
        Reply(peer, access, true);
        return;
      }
      uint64_t bytes = 0, revision = 0;
      const auto digest = command.GetString("sha256");
      const auto* initialize = command.Find("initialize");
      if (!peer.writer ||
          !wire::HasKeys(command, {"op", "bytes", "sha256", "expected_revision",
                                   "initialize"}) ||
          command.GetString("op") != "publish" ||
          !wire::GetUnsigned(command, "bytes", &bytes).ok() || !bytes ||
          bytes > kAccessPolicyBytes ||
          !wire::GetUnsigned(command, "expected_revision", &revision).ok() ||
          revision > kAccessRevisionLimit || !initialize ||
          !initialize->IsBool() || digest.size() != 64 ||
          !std::all_of(digest.begin(), digest.end(), [](char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
          })) {
        Reply(peer, access, false, "access_publication_refused");
        return;
      }
      peer.bytes = bytes;
      peer.expected_revision = revision;
      peer.initialize = initialize->boolean;
      peer.digest = digest;
      peer.payload.reserve(bytes);
      continue;
    }
    if (peer.payload.size() + n > peer.bytes) {
      Reply(peer, access, false, "access_payload_invalid");
      return;
    }
    peer.payload.append(buffer.data(), n);
    if (peer.payload.size() != peer.bytes) continue;
    AccessPolicy candidate;
    if (!ParseAccessPolicy(peer.payload, &candidate).ok() ||
        AccessPolicyDigest(candidate) != peer.digest) {
      Reply(peer, access, false, "access_payload_invalid");
      return;
    }
    const auto result =
        peer.initialize
            ? (peer.expected_revision == 0
                   ? access.Configure(std::move(candidate))
                   : Status::Fail("initialization conflict"))
            : access.Synchronize(std::move(candidate), peer.expected_revision);
    Reply(peer, access, result.ok(),
          result.ok() ? "" : "access_revision_conflict");
  }
}

void AccessAdmin::Step(ClientAccess& access) {
  const auto now = std::chrono::steady_clock::now();
  OwnedFd accepted(::accept4(listener_.Get(), nullptr, nullptr,
                             SOCK_NONBLOCK | SOCK_CLOEXEC));
  if (accepted.Get() >= 0) {
    ucred identity{};
    socklen_t size = sizeof(identity);
    auto free = std::find_if(peers_.begin(), peers_.end(),
                             [](const Peer& p) { return p.fd.Get() < 0; });
    if (free != peers_.end() &&
        ::getsockopt(accepted.Get(), SOL_SOCKET, SO_PEERCRED, &identity,
                     &size) == 0 &&
        size == sizeof(identity) && identity.uid == ::geteuid()) {
      free->fd = std::move(accepted);
      free->writer = identity.pid == owner_;
      free->deadline = now + std::chrono::seconds(3);
    }
  }
  for (auto& peer : peers_) {
    if (peer.fd.Get() < 0) continue;
    if (now >= peer.deadline)
      peer = {};
    else
      Receive(peer, access);
  }
}

}  // namespace q4t::service::ingress
