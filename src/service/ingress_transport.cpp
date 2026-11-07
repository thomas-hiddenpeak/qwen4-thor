#include "q4t/service/ingress_transport.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <optional>

#include "q4t/service/guardian_wire.h"

namespace q4t::service::ingress {
std::atomic<bool> stopped{false};
static_assert(std::atomic<bool>::is_always_lock_free);
namespace {
using namespace std::chrono_literals;

bool Wait(int fd, short events, Clock::time_point deadline,
          Connection* client = nullptr) {
  while (!stopped.load(std::memory_order_relaxed)) {
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                          deadline - Clock::now())
                          .count();
    if (left <= 0) return false;
    pollfd fds[2] = {{fd, static_cast<short>(events | POLLRDHUP), 0},
                     {client ? client->Fd() : -1,
                      static_cast<short>(
                          (client ? client->PollEvents() : POLLIN) | POLLRDHUP),
                      0}};
    const int result = ::poll(fds, client ? 2 : 1,
                              static_cast<int>(std::min<int64_t>(left, 100)));
    if (result < 0 && errno == EINTR) continue;
    if (result < 0) return false;
    // One request per connection. Extra request bytes or a disconnected
    // downstream cancel this relay; never drain/replay them on another model.
    if (client && fds[1].revents && client->Cancelled()) return false;
    if (fds[0].revents & (POLLERR | POLLNVAL)) return false;
    if (fds[0].revents & (events | POLLHUP | POLLRDHUP)) return true;
  }
  return false;
}

std::string Lower(std::string text) {
  for (char& c : text)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return text;
}

std::string_view Trim(std::string_view value) {
  while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
    value.remove_prefix(1);
  while (!value.empty() && (value.back() == ' ' || value.back() == '\t'))
    value.remove_suffix(1);
  return value;
}

bool ResponseHeaders(std::string_view raw, std::string_view extra,
                     std::string* output, std::optional<size_t>* length) {
  size_t pos = raw.find("\r\n");
  if (pos == raw.npos || pos < 12) return false;
  const auto first = raw.substr(0, pos);
  if (!first.starts_with("HTTP/1.1 ") && !first.starts_with("HTTP/1.0 "))
    return false;
  int status = 0;
  const auto parsed =
      std::from_chars(first.data() + 9, first.data() + 12, status);
  if (parsed.ec != std::errc{} || parsed.ptr != first.data() + 12 ||
      status < 200 || status > 599 || (first.size() > 12 && first[12] != ' '))
    return false;
  for (unsigned char c : first)
    if (c < 32 || c > 126) return false;
  output->assign(first);
  output->append("\r\n");
  pos += 2;
  bool seen_type = false;
  while (pos < raw.size()) {
    const size_t end = raw.find("\r\n", pos);
    if (end == raw.npos) return false;
    const auto field = raw.substr(pos, end - pos);
    const size_t colon = field.find(':');
    if (colon == field.npos || colon == 0) return false;
    const std::string name = Lower(std::string(field.substr(0, colon)));
    for (unsigned char c : name)
      if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
            std::string_view("!#$%&'*+-.^_`|~").find(c) !=
                std::string_view::npos))
        return false;
    const auto value = Trim(field.substr(colon + 1));
    for (unsigned char c : value)
      if ((c < 32 && c != '\t') || c == 127) return false;
    if (name == "transfer-encoding") return false;
    if (name == "content-length") {
      size_t size = 0;
      const auto result =
          std::from_chars(value.data(), value.data() + value.size(), size);
      if (length->has_value() || value.empty() || result.ec != std::errc{} ||
          result.ptr != value.data() + value.size())
        return false;
      *length = size;
    }
    if (name == "content-type") {
      if (seen_type) return false;
      seen_type = true;
    }
    // Preserve the runner's public response metadata, never private headers,
    // cookies, hop-by-hop options or a second response on this connection.
    if (name == "content-length" || name == "content-type" ||
        name == "retry-after" || name == "www-authenticate" ||
        name == "x-request-id" || name == "allow") {
      output->append(field);
      output->append("\r\n");
    }
    pos = end + 2;
  }
  if ((status == 204 || status == 304) && length->value_or(0) != 0)
    return false;
  if (status == 204 || status == 304) *length = 0;
  output->append("Connection: close\r\nCache-Control: no-store\r\n");
  output->append(extra);
  output->append("\r\n");
  return true;
}
}  // namespace

bool Write(int fd, std::string_view data, Clock::time_point deadline,
           Connection* client, bool* started) {
  while (!data.empty() && Wait(fd, POLLOUT, deadline, client)) {
    const ssize_t size =
        ::send(fd, data.data(), data.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
    if (size < 0 && (errno == EAGAIN || errno == EINTR)) continue;
    if (size <= 0) return false;
    if (started) *started = true;
    data.remove_prefix(static_cast<size_t>(size));
  }
  return data.empty();
}

bool Connection::Handshake(const std::shared_ptr<TlsContext>& context) {
  tls_ = context ? context->Stream(fd_) : nullptr;
  return tls_ && tls_->Accept(Clock::now() + 5s);
}

Connection::~Connection() {
  if (!tls_) return;
  // An early 401/413 may race the next encrypted request-body record. Closing
  // with unread TCP bytes produces RST and can discard the response just sent.
  // Send close_notify, half-close writes, then drain only a bounded transport
  // tail; never parse or dispatch another application request.
  tls_.reset();
  ::shutdown(fd_, SHUT_WR);
  const auto deadline = Clock::now() + 100ms;
  std::array<char, 4096> tail{};
  size_t discarded = 0;
  while (discarded < 65536 && Wait(fd_, POLLIN, deadline)) {
    const auto n = ::recv(fd_, tail.data(), tail.size(), MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    if (n <= 0) break;
    discarded += n;
  }
}

ssize_t Connection::Read(void* data, size_t size, Clock::time_point deadline) {
  if (tls_) return tls_->Read(data, size, deadline);
  while (Wait(fd_, POLLIN, deadline)) {
    const auto n = ::recv(fd_, data, size, MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    return n;
  }
  errno = Clock::now() >= deadline ? ETIMEDOUT : ECANCELED;
  return -1;
}

bool Connection::Send(std::string_view data, Clock::time_point deadline,
                      bool* started) {
  return tls_ ? tls_->Write(data, deadline, started)
              : Write(fd_, data, deadline, nullptr, started);
}

bool Connection::Cancelled() {
  if (tls_) return tls_->Cancelled();
  char byte;
  const auto n = ::recv(fd_, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
  return n >= 0 || (errno != EAGAIN && errno != EINTR);
}
short Connection::PollEvents() const {
  return tls_ ? tls_->PollEvents() : POLLIN;
}

std::string RequestId() {
  std::array<unsigned char, 16> bytes{};
  size_t used = 0;
  while (used < bytes.size()) {
    const ssize_t n = ::getrandom(bytes.data() + used, bytes.size() - used, 0);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return {};
    used += static_cast<size_t>(n);
  }
  constexpr char hex[] = "0123456789abcdef";
  std::string result = "req-";
  for (unsigned char c : bytes) {
    result += hex[c >> 4];
    result += hex[c & 15];
  }
  return result;
}

void Reply(Connection& client, int status, std::string_view body,
           std::string_view request_id, bool* started) {
  if (*started) return;
  const std::string extra =
      status == 401
          ? "WWW-Authenticate: Bearer realm=\"q4t\"\r\n"
          : (status == 429 || status == 503 ? "Retry-After: 1\r\n" : "");
  const std::string response =
      "HTTP/1.1 " + std::to_string(status) +
      " Response\r\nContent-Type: application/json\r\nContent-Length: " +
      std::to_string(body.size()) + "\r\nConnection: close\r\n" +
      "Cache-Control: no-store\r\nX-Request-Id: " + std::string(request_id) +
      "\r\n" + extra + client.extra_headers + "\r\n" + std::string(body);
  client.Send(response, Clock::now() + 1s, started);
}

void Error(Connection& client, int status, std::string_view code,
           std::string_view request_id, bool* started) {
  const auto value = wire::Object(
      {{"error",
        wire::Object(
            {{"message", wire::String("Service entrypoint rejected request")},
             {"type",
              wire::String(status == 503 ? "service_error" : "gateway_error")},
             {"param", io::Json{}},
             {"code", wire::String(std::string(code))}})}});
  std::string body;
  if (wire::Encode(value, &body))
    Reply(client, status, body, request_id, started);
}

Decision Authorize(const std::string& address, pid_t parent,
                   std::string_view client, bool control, int expected_port) {
  Decision denied;
  sockaddr_un target{};
  if (address.empty() || address.size() + 1 > sizeof(target.sun_path))
    return denied;
  target.sun_family = AF_UNIX;
  std::memcpy(target.sun_path + 1, address.data(), address.size());
  OwnedFd sock(
      ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
  if (sock.Get() < 0) return denied;
  const auto deadline = Clock::now() + 1500ms;
  if (::connect(sock.Get(), reinterpret_cast<sockaddr*>(&target),
                offsetof(sockaddr_un, sun_path) + address.size() + 1) != 0)
    return denied;
  ucred peer{};
  socklen_t peer_size = sizeof(peer);
  if (::getsockopt(sock.Get(), SOL_SOCKET, SO_PEERCRED, &peer, &peer_size) !=
          0 ||
      peer_size != sizeof(peer) || peer.pid != parent ||
      peer.uid != ::geteuid())
    return denied;
  std::string request;
  if (!wire::Encode(
          wire::Object({{"version", wire::Unsigned(1)},
                        {"client", wire::String(std::string(client))},
                        {"lane", wire::String(control ? "control" : "data")}}),
          &request) ||
      !Write(sock.Get(), request, deadline) ||
      !Wait(sock.Get(), POLLIN, deadline))
    return denied;
  char buffer[4096];
  iovec vector{buffer, sizeof(buffer)};
  msghdr message{};
  message.msg_iov = &vector;
  message.msg_iovlen = 1;
  const ssize_t size = ::recvmsg(sock.Get(), &message, MSG_DONTWAIT);
  io::Json result;
  if (size <= 0 || (message.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) ||
      !wire::Parse(std::string(buffer, static_cast<size_t>(size)), &result))
    return denied;
  if (wire::HasKeys(result, {"error"})) {
    const auto code = result.GetString("error");
    for (const auto* known :
         {"service_recovering", "service_quarantined", "service_stopping",
          "service_quota_wait", "service_binding_unavailable"})
      if (code == known) denied.error = code;
    return denied;
  }
  uint64_t port = 0;
  const auto token = result.GetString("token");
  if (!wire::HasKeys(result, {"port", "token"}) ||
      !wire::GetUnsigned(result, "port", &port) ||
      port != static_cast<uint64_t>(expected_port) || token.size() != 64)
    return denied;
  for (char c : token)
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return denied;
  return {expected_port, token, ""};
}

OwnedFd Connect(int port, Connection& client) {
  OwnedFd fd(::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
  if (fd.Get() < 0) return {};
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  address.sin_port = htons(static_cast<uint16_t>(port));
  const int connected = ::connect(
      fd.Get(), reinterpret_cast<sockaddr*>(&address), sizeof(address));
  if (connected != 0 && (errno != EINPROGRESS ||
                         !Wait(fd.Get(), POLLOUT, Clock::now() + 2s, &client)))
    return {};
  int error = 0;
  socklen_t size = sizeof(error);
  if (::getsockopt(fd.Get(), SOL_SOCKET, SO_ERROR, &error, &size) || error)
    return {};
  return fd;
}

void Relay(int upstream, Connection& client, int idle_seconds,
           std::string_view request_id, bool* started) {
  std::array<char, 16384> buffer{};
  std::string head;
  size_t end = std::string::npos;
  const auto header_deadline =
      Clock::now() + std::chrono::seconds(idle_seconds);
  while ((end = head.find("\r\n\r\n")) == std::string::npos) {
    if (head.size() >= 65536 ||
        !Wait(upstream, POLLIN, header_deadline, &client))
      break;
    const ssize_t n =
        ::recv(upstream, buffer.data(), buffer.size(), MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    if (n <= 0) break;
    head.append(buffer.data(), static_cast<size_t>(n));
  }
  std::string headers;
  std::optional<size_t> length;
  if (end == std::string::npos || end + 4 > 65536 ||
      !ResponseHeaders(std::string_view(head).substr(0, end + 2),
                       client.extra_headers, &headers, &length)) {
    Error(client, 503, "service_binding_unavailable", request_id, started);
    return;
  }
  if (!client.Send(headers, Clock::now() + 15s, started)) return;
  size_t delivered = 0;
  const auto emit = [&](std::string_view part) {
    if (length && part.size() > *length - delivered)
      part = part.substr(0, *length - delivered);
    if (!client.Send(part, Clock::now() + 15s)) return false;
    delivered += part.size();
    return true;
  };
  if (!emit(std::string_view(head).substr(end + 4))) return;
  while (!length || delivered < *length) {
    if (!Wait(upstream, POLLIN,
              Clock::now() + std::chrono::seconds(idle_seconds), &client))
      return;
    const ssize_t n =
        ::recv(upstream, buffer.data(), buffer.size(), MSG_DONTWAIT);
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    if (n <= 0) return;  // A truncated response remains truncated.
    if (!emit(std::string_view(buffer.data(), static_cast<size_t>(n)))) return;
  }
}
}  // namespace q4t::service::ingress
