#include "q4t/service/ingress_duplex.h"

#include <poll.h>
#include <sys/socket.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>

namespace q4t::service::ingress {
namespace {
using namespace std::chrono_literals;
struct Buffer {
  std::array<char, kDuplexBufferBytes> data{};
  size_t offset = 0, end = 0;
  short read_wait = POLLIN, write_wait = POLLOUT;
  Clock::time_point write_deadline;
  bool Empty() const { return offset == end; }
  std::string_view Bytes() const {
    return {data.data() + offset, end - offset};
  }
  void Fill(size_t count, Clock::time_point deadline) {
    offset = 0;
    end = count;
    write_deadline = deadline;
  }
};
bool Again() {
  return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
}
}  // namespace

DuplexResult RelayDuplex(int upstream, Connection& client,
                         std::string_view client_prefix,
                         std::string_view upstream_prefix,
                         const DuplexLimits& limits,
                         const std::function<bool()>& authorized) {
  DuplexResult result;
  if (upstream < 0 || client.Fd() < 0 || upstream == client.Fd() ||
      client_prefix.size() > kDuplexBufferBytes ||
      upstream_prefix.size() > kDuplexBufferBytes ||
      limits.idle <= 0ms || limits.idle > 1h ||
      limits.stalled_write <= 0ms || limits.stalled_write > 1min ||
      limits.session <= 0ms || limits.session > 1h ||
      !limits.bytes_per_direction || limits.bytes_per_direction > (8ULL << 30) ||
      client_prefix.size() > limits.bytes_per_direction ||
      upstream_prefix.size() > limits.bytes_per_direction)
    return result;
  Buffer to_client, to_upstream;
  const auto start = Clock::now();
  const auto session_deadline = start + limits.session;
  auto idle_deadline = start + limits.idle;
  const auto initialize = [&](Buffer& buffer, std::string_view prefix) {
    if (!prefix.empty())
      std::memcpy(buffer.data.data(), prefix.data(), prefix.size());
    buffer.Fill(prefix.size(), start + limits.stalled_write);
  };
  initialize(to_upstream, client_prefix);
  initialize(to_client, upstream_prefix);
  result.client_received = result.peak_to_upstream = client_prefix.size();
  result.upstream_received = result.peak_to_client = upstream_prefix.size();
  bool upstream_eof = false;
  auto next_authorization = start;
  const auto finish = [&](DuplexEnd reason, int error = 0) {
    result.reason = reason;
    result.system_error = error;
    return result;
  };
  while (!stopped.load(std::memory_order_relaxed)) {
    auto now = Clock::now();
    if (authorized && now >= next_authorization) {
      if (!authorized()) return finish(DuplexEnd::kRevoked);
      now = Clock::now();
      next_authorization = now + 1s;
    }
    if (now >= session_deadline) return finish(DuplexEnd::kSessionDeadline);
    if (now >= idle_deadline) return finish(DuplexEnd::kIdleDeadline);
    if ((!to_client.Empty() && now >= to_client.write_deadline) ||
        (!to_upstream.Empty() && now >= to_upstream.write_deadline))
      return finish(DuplexEnd::kWriteDeadline);
    bool progress = false;
    // Write first, keeping the exact buffer and length on every WANT retry.
    if (!to_client.Empty()) {
      const auto n = client.TryWrite(to_client.Bytes(), &to_client.write_wait);
      if (n > 0) {
        to_client.offset += static_cast<size_t>(n);
        result.client_sent += n;
        to_client.write_deadline = Clock::now() + limits.stalled_write;
        progress = true;
      } else if (!n || !Again()) {
        return finish(DuplexEnd::kIoError, n ? errno : EPIPE);
      }
    }
    if (upstream_eof && to_client.Empty())
      return finish(DuplexEnd::kUpstreamEof);
    if (!upstream_eof && !to_upstream.Empty()) {
      const auto bytes = to_upstream.Bytes();
      const auto n = ::send(upstream, bytes.data(), bytes.size(),
                             MSG_DONTWAIT | MSG_NOSIGNAL);
      if (n > 0) {
        to_upstream.offset += static_cast<size_t>(n);
        result.upstream_sent += n;
        to_upstream.write_deadline = Clock::now() + limits.stalled_write;
        progress = true;
      } else if (!n || !Again()) {
        return finish(DuplexEnd::kIoError, n ? errno : EPIPE);
      }
    }
    if (!upstream_eof && to_upstream.Empty()) {
      const auto maximum = std::min<uint64_t>(
          to_upstream.data.size(),
          limits.bytes_per_direction - result.client_received + 1);
      const auto n = client.TryRead(to_upstream.data.data(), maximum,
                                     &to_upstream.read_wait);
      if (n > 0) {
        result.client_received += n;
        if (result.client_received > limits.bytes_per_direction)
          return finish(DuplexEnd::kByteLimit);
        to_upstream.Fill(n, Clock::now() + limits.stalled_write);
        result.peak_to_upstream =
            std::max(result.peak_to_upstream, static_cast<size_t>(n));
        progress = true;
      } else if (!n) {
        return finish(DuplexEnd::kClientEof);
      } else if (!Again()) {
        return finish(DuplexEnd::kIoError, errno);
      }
    }
    if (!upstream_eof && to_client.Empty()) {
      const auto maximum = std::min<uint64_t>(
          to_client.data.size(),
          limits.bytes_per_direction - result.upstream_received + 1);
      const auto n = ::recv(upstream, to_client.data.data(), maximum,
                             MSG_DONTWAIT);
      if (n > 0) {
        result.upstream_received += n;
        if (result.upstream_received > limits.bytes_per_direction)
          return finish(DuplexEnd::kByteLimit);
        to_client.Fill(n, Clock::now() + limits.stalled_write);
        result.peak_to_client =
            std::max(result.peak_to_client, static_cast<size_t>(n));
        progress = true;
      } else if (!n) {
        upstream_eof = true;
        progress = true;
      } else if (!Again()) {
        return finish(DuplexEnd::kIoError, errno);
      }
    }
    if (progress) {
      idle_deadline = Clock::now() + limits.idle;
      continue;
    }
    short client_events = 0, upstream_events = 0;
    if (!to_client.Empty()) client_events |= to_client.write_wait;
    if (!upstream_eof) {
      if (to_upstream.Empty()) client_events |= to_upstream.read_wait;
      if (!to_upstream.Empty()) upstream_events |= POLLOUT;
      if (to_client.Empty()) upstream_events |= POLLIN;
    }
    const auto deadline = std::min(
        {session_deadline, idle_deadline,
         to_client.Empty() ? session_deadline : to_client.write_deadline,
         to_upstream.Empty() ? session_deadline : to_upstream.write_deadline});
    const auto left = std::chrono::ceil<std::chrono::milliseconds>(
        deadline - Clock::now()).count();
    pollfd fds[] = {{client_events ? client.Fd() : -1, client_events, 0},
                    {upstream_events ? upstream : -1, upstream_events, 0}};
    const int ready = ::poll(fds, 2, std::clamp<int64_t>(left, 0, 100));
    if (ready < 0 && errno != EINTR)
      return finish(DuplexEnd::kIoError, errno);
    for (const auto& fd : fds)
      if (fd.revents & POLLNVAL) return finish(DuplexEnd::kIoError, EBADF);
    // Even on HUP/ERR, try nonblocking I/O first to preserve queued final data.
  }
  return finish(DuplexEnd::kStopped);
}
}  // namespace q4t::service::ingress
