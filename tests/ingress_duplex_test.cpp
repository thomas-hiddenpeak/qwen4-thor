#include "q4t/service/ingress_duplex.h"

#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <fstream>
#include <future>
#include <iterator>
#include <stdexcept>
#include <thread>

#include "q4t/test.h"

namespace {
using namespace q4t::service;
using namespace q4t::service::ingress;
using namespace std::chrono_literals;
struct Pair {
  OwnedFd a, b;
  Pair() {
    int fd[2];
    if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0,
                     fd) != 0)
      throw std::runtime_error("socketpair");
    a = OwnedFd(fd[0]); b = OwnedFd(fd[1]);
    const int capacity = 4096;
    for (int endpoint : fd)
      ::setsockopt(endpoint, SOL_SOCKET, SO_SNDBUF, &capacity, sizeof(capacity));
  }
};
bool Put(int fd, std::string_view bytes) {
  const auto deadline = Clock::now() + 2s;
  while (!bytes.empty() && Clock::now() < deadline) {
    const auto n = ::send(fd, bytes.data(), bytes.size(), MSG_NOSIGNAL);
    if (n > 0) bytes.remove_prefix(n);
    else if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
      pollfd event{fd, POLLOUT, 0};
      ::poll(&event, 1, 10);
    } else return false;
  }
  return bytes.empty();
}
std::string Take(int fd, size_t size) {
  std::string output(size, '\0');
  size_t offset = 0;
  const auto deadline = Clock::now() + 2s;
  while (offset < size && Clock::now() < deadline) {
    const auto n = ::recv(fd, output.data() + offset, size - offset, 0);
    if (n > 0) offset += n;
    else if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
      pollfd event{fd, POLLIN, 0};
      ::poll(&event, 1, 10);
    } else break;
  }
  output.resize(offset);
  return output;
}
DuplexLimits Short() { return {1000ms, 500ms, 2000ms, 8ULL << 30}; }
}  // namespace

Q4T_TEST(http_early_error_sent_before_half_close) {
  Pair pair;
  const auto reply = UnavailableResponse();
  ServeConnection(pair.b.Get(), false, nullptr, reply,
                  [](Connection&, bool&) { throw std::runtime_error("peer"); });
  Q4T_CHECK(Take(pair.a.Get(), reply.size() + 1) == reply);
  return true;
}

Q4T_TEST(http_started_stream_preserves_its_only_response) {
  Pair pair;
  constexpr std::string_view stream =
      "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: x\n\n";
  ServeConnection(pair.b.Get(), false, nullptr, UnavailableResponse(),
                  [&](Connection& client, bool& started) {
                    client.Send(stream, Clock::now() + 1s, &started);
                    throw std::runtime_error("peer");
                  });
  Q4T_CHECK(Take(pair.a.Get(), stream.size() + 1) == stream);
  return true;
}

Q4T_TEST(http_failed_tls_setup_sends_no_plaintext_response) {
  Pair pair;
  bool dispatched = false;
  ServeConnection(pair.b.Get(), true, nullptr, UnavailableResponse(),
                  [&](Connection&, bool&) { dispatched = true; });
  Q4T_CHECK(!dispatched && Take(pair.a.Get(), 1).empty());
  return true;
}

Q4T_TEST(duplex_preserves_upgrade_tails_and_bidirectional_bytes) {
  Pair client, backend;
  Connection connection(client.b.Get());
  auto relay = std::async(std::launch::async, [&] {
    return RelayDuplex(backend.b.Get(), connection, "client-tail", "server-tail",
                       Short());
  });
  Q4T_CHECK(Take(client.a.Get(), 11) == "server-tail");
  Q4T_CHECK(Take(backend.a.Get(), 11) == "client-tail");
  const auto send = [&](int fd, char outgoing, char incoming) {
    for (int i = 0; i < 128; ++i) {
      if (!Put(fd, std::string(4096, outgoing)) ||
          Take(fd, 4096) != std::string(4096, incoming)) return false;
    }
    return true;
  };
  auto upstream = std::async(std::launch::async,
                              [&] { return send(backend.a.Get(), 's', 'c'); });
  Q4T_CHECK(send(client.a.Get(), 'c', 's') && upstream.get());
  ::shutdown(backend.a.Get(), SHUT_WR);
  const auto result = relay.get();
  Q4T_CHECK(result.reason == DuplexEnd::kUpstreamEof);
  Q4T_CHECK(result.client_received == 524299 && result.client_sent == 524299 &&
            result.upstream_received == 524299 && result.upstream_sent == 524299);
  Q4T_CHECK(result.peak_to_client <= kDuplexBufferBytes &&
            result.peak_to_upstream <= kDuplexBufferBytes);
  return true;
}

Q4T_TEST(duplex_final_upstream_bytes_drain_before_eof) {
  Pair client, backend;
  Connection connection(client.b.Get());
  auto relay = std::async(std::launch::async, [&] {
    return RelayDuplex(backend.b.Get(), connection, "", "", Short());
  });
  auto writer = std::async(std::launch::async, [&] {
    const bool ok = Put(backend.a.Get(), std::string(100000, 'x'));
    ::shutdown(backend.a.Get(), SHUT_WR);
    return ok;
  });
  Q4T_CHECK(Take(client.a.Get(), 100000) == std::string(100000, 'x'));
  Q4T_CHECK(writer.get());
  const auto result = relay.get();
  Q4T_CHECK(result.reason == DuplexEnd::kUpstreamEof &&
            result.client_sent == 100000);
  return true;
}

Q4T_TEST(duplex_idle_and_absolute_session_deadlines_are_distinct) {
  for (const bool session : {false, true}) {
    Pair client, backend;
    Connection connection(client.b.Get());
    auto limits = Short();
    if (session) limits.session = 30ms;
    else limits.idle = 30ms;
    const auto begin = Clock::now();
    const auto result = RelayDuplex(backend.b.Get(), connection, "", "", limits);
    Q4T_CHECK(result.reason == (session ? DuplexEnd::kSessionDeadline
                                        : DuplexEnd::kIdleDeadline));
    Q4T_CHECK(Clock::now() - begin >= 30ms && Clock::now() - begin < 300ms);
  }
  return true;
}

Q4T_TEST(duplex_stuck_consumer_does_not_block_other_direction) {
  Pair client, backend;
  Connection connection(client.b.Get());
  auto limits = Short();
  limits.stalled_write = 100ms;
  auto relay = std::async(std::launch::async, [&] {
    return RelayDuplex(backend.b.Get(), connection, "",
                       std::string(kDuplexBufferBytes, 'x'), limits);
  });
  // Client never reads. Its inbound audio can still reach the backend, but
  // that activity must not keep the blocked outbound buffer alive forever.
  Q4T_CHECK(Put(client.a.Get(), "audio") && Take(backend.a.Get(), 5) == "audio");
  const auto result = relay.get();
  Q4T_CHECK(result.reason == DuplexEnd::kWriteDeadline);
  Q4T_CHECK(result.upstream_sent == 5 && result.client_sent < kDuplexBufferBytes);
  return true;
}

Q4T_TEST(duplex_stuck_backend_does_not_block_downstream_delivery) {
  Pair client, backend;
  Connection connection(client.b.Get());
  auto limits = Short();
  limits.stalled_write = 100ms;
  auto relay = std::async(std::launch::async, [&] {
    return RelayDuplex(backend.b.Get(), connection,
                       std::string(kDuplexBufferBytes, 'x'), "", limits);
  });
  Q4T_CHECK(Put(backend.a.Get(), "event") && Take(client.a.Get(), 5) == "event");
  const auto result = relay.get();
  Q4T_CHECK(result.reason == DuplexEnd::kWriteDeadline);
  Q4T_CHECK(result.client_sent == 5 && result.upstream_sent < kDuplexBufferBytes);
  return true;
}

Q4T_TEST(duplex_byte_limit_never_forwards_over_budget_tail) {
  for (const bool input : {false, true}) {
    Pair client, backend;
    Connection connection(client.b.Get());
    auto limits = Short();
    limits.bytes_per_direction = 4;
    Q4T_CHECK(Put(input ? client.a.Get() : backend.a.Get(), "abcde"));
    const auto result = RelayDuplex(backend.b.Get(), connection, "", "", limits);
    Q4T_CHECK(result.reason == DuplexEnd::kByteLimit);
    Q4T_CHECK(result.client_sent == 0 && result.upstream_sent == 0);
  }
  return true;
}

Q4T_TEST(duplex_client_eof_and_shutdown_do_not_replay_or_own_fds) {
  Pair client, backend;
  Connection connection(client.b.Get());
  ::shutdown(client.a.Get(), SHUT_WR);
  Q4T_CHECK(RelayDuplex(backend.b.Get(), connection, "", "", Short()).reason ==
            DuplexEnd::kClientEof);
  stopped = true;
  const auto result = RelayDuplex(backend.b.Get(), connection, "", "", Short());
  stopped = false;
  Q4T_CHECK(result.reason == DuplexEnd::kStopped);
  Q4T_CHECK(Put(backend.b.Get(), "still-owned"));
  Q4T_CHECK(Take(backend.a.Get(), 11) == "still-owned");
  return true;
}

Q4T_TEST(duplex_invalid_limits_do_not_consume_input) {
  Pair client, backend;
  Connection connection(client.b.Get());
  Q4T_CHECK(Put(client.a.Get(), "retained"));
  auto limits = Short();
  limits.session = 3600001ms;
  Q4T_CHECK(RelayDuplex(backend.b.Get(), connection, "", "", limits).reason ==
            DuplexEnd::kInvalidLimits);
  limits = Short();
  Q4T_CHECK(RelayDuplex(backend.b.Get(), connection,
                        std::string(kDuplexBufferBytes + 1, 'x'), "",
                        limits).reason == DuplexEnd::kInvalidLimits);
  Q4T_CHECK(Take(client.b.Get(), 8) == "retained");
  return true;
}

int main(int argc, char** argv) {
  ::signal(SIGPIPE, SIG_IGN);
  if (argc == 1) return q4t::test::RunAllTests();
  // CPU-only TLS peer controlled by the Python test through inherited sockets.
  // No listener, model or HTTP route; fds and certificate paths are test inputs.
  if (argc != 7) return 2;
  const std::string_view mode = argv[1];
  if (mode != "--tls-peer" && mode != "--http-error-peer") return 2;
  const auto file = [](const char* path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) throw std::runtime_error("fixture file");
    return std::string(std::istreambuf_iterator<char>(stream), {});
  };
  try {
    std::string error;
    auto tls = TlsContext::Create({file(argv[2]), file(argv[3]), "", ""},
                                   "127.0.0.1", false, &error);
    if (!tls) return 3;
    OwnedFd client(std::stoi(argv[4])), upstream(std::stoi(argv[5]));
    if (mode == "--http-error-peer") {
      bool dispatched = false;
      ServeConnection(client.Get(), true, tls, UnavailableResponse(),
                      [&](Connection&, bool&) {
                        dispatched = true;
                        throw std::runtime_error("test handler");
                      });
      if (!dispatched) return 4;
      std::puts("{}");
      return 0;
    }
    Connection connection(client.Get());
    if (!connection.Handshake(tls)) return 4;
    auto limits = Short();
    limits.idle = 2000ms; limits.session = 6000ms;
    limits.stalled_write = std::chrono::milliseconds(std::stoi(argv[6]));
    const auto result = RelayDuplex(upstream.Get(), connection, "", "", limits);
    std::printf("{\"reason\":%d,\"client_sent\":%llu,\"upstream_sent\":%llu,"
                "\"peak_to_client\":%zu,\"peak_to_upstream\":%zu}\n",
                static_cast<int>(result.reason),
                static_cast<unsigned long long>(result.client_sent),
                static_cast<unsigned long long>(result.upstream_sent),
                result.peak_to_client, result.peak_to_upstream);
    return 0;
  } catch (...) { return 5; }
}
