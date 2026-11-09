#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
using namespace q4t::server;
using namespace q4t::server::detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;

struct SocketPair {
  int fd[2]{-1, -1};
  SocketPair() {
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fd))
      throw std::runtime_error("socketpair fixture");
  }
  ~SocketPair() {
    for (int value : fd)
      if (value >= 0) close(value);
  }
};

struct ReadResult {
  bool accepted;
  int status;
  HttpRequestHeaders headers;
};

ReadResult Read(const std::string& wire, bool eof = true,
                HttpBodyBudget* shared_budget = nullptr) {
  SocketPair sockets;
  if (!WriteAll(sockets.fd[0], wire, {Clock::now() + 1s}))
    throw std::runtime_error("fixture request write");
  if (eof) shutdown(sockets.fd[0], SHUT_WR);
  std::string method, path, body;
  HttpBodyBudget budget(kAggregateChatBodyBytes);
  HttpBodyLease lease(shared_budget ? *shared_budget : budget);
  ReadResult result{};
  result.accepted = ReadHttpRequest(sockets.fd[1], &method, &path, &body, 20,
                                    &lease, &result.status, &result.headers);
  return result;
}

uint64_t WriteCount(const HttpTransportMetrics& metrics) {
  uint64_t count = 0;
  for (const auto& origin : metrics.write)
    for (const auto& reason : origin) count += reason.load();
  return count;
}

}  // namespace

Q4T_TEST(http_observation_receive_shape_and_size_reasons) {
  for (const auto& fixture :
       {std::pair{"GET / HTTP/9\r\n\r\n", HttpReceiveFailure::kMalformed},
        {"POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
         "Content-Length: 16777217\r\n\r\n",
         HttpReceiveFailure::kBodyTooLarge},
        {"POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
         "Content-Length: 9999999999999999999999\r\n\r\n",
         HttpReceiveFailure::kBodyTooLarge},
        {"POST /other HTTP/1.1\r\nHost: t\r\nContent-Length: 4097\r\n\r\n",
         HttpReceiveFailure::kBodyTooLarge}}) {
    const auto result = Read(fixture.first);
    Q4T_CHECK(!result.accepted &&
              result.status == (fixture.second == HttpReceiveFailure::kBodyTooLarge
                                    ? 413 : 400));
    Q4T_CHECK(result.headers.failure == fixture.second);
  }
  const auto header =
      Read("GET / HTTP/1.1\r\nHost: t\r\nX: " + std::string(65536, 'x') +
           "\r\n\r\n");
  Q4T_CHECK(!header.accepted && header.status == 400);
  Q4T_CHECK(header.headers.failure == HttpReceiveFailure::kHeaderTooLarge);
  const auto valid = Read(
      "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
      "Content-Length: 2\r\n\r\n{}");
  Q4T_CHECK(valid.accepted);
  Q4T_CHECK(valid.headers.failure == HttpReceiveFailure::kNone);
  return true;
}

Q4T_TEST(http_observation_receive_eof_deadline_and_socket) {
  const std::string header =
      "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
      "Content-Length: 3\r\n\r\nx";
  for (bool body : {false, true}) {
    for (bool eof : {false, true}) {
      const auto result = Read(body ? header : "GET /", eof);
      const auto expected = eof ? (body ? HttpReceiveFailure::kBodyEof
                                        : HttpReceiveFailure::kHeaderEof)
                                : (body ? HttpReceiveFailure::kBodyDeadline
                                        : HttpReceiveFailure::kHeaderDeadline);
      Q4T_CHECK(!result.accepted && result.status == (eof ? 400 : 408));
      Q4T_CHECK(result.headers.failure == expected);
      Q4T_CHECK(result.headers.system_error == 0);
    }
  }
  std::string method, path, body;
  HttpRequestHeaders observed;
  Q4T_CHECK(!ReadHttpRequest(2147483647, &method, &path, &body, 20, nullptr,
                             nullptr, &observed));
  Q4T_CHECK(observed.failure == HttpReceiveFailure::kSocket);
  Q4T_CHECK(observed.system_error == EBADF);
  return true;
}

Q4T_TEST(http_observation_body_rejection_preserves_other_lease) {
  HttpBodyBudget budget(8);
  {
    HttpBodyLease held(budget);
    Q4T_CHECK(held.Acquire(8));
    for (const char* path : {"completions", "preflight"}) {
      const auto result =
          Read(std::string("POST /v1/chat/") + path +
                   " HTTP/1.1\r\nHost: t\r\nContent-Length: 1\r\n\r\n",
               false, &budget);
      Q4T_CHECK(!result.accepted && result.status == 503);
      Q4T_CHECK(result.headers.failure == HttpReceiveFailure::kBodyCapacity);
      Q4T_CHECK(result.headers.error_code == "body_capacity_exhausted");
      Q4T_CHECK(result.headers.error_param == "body" && budget.Used() == 8);
    }
  }
  Q4T_CHECK(budget.Used() == 0);
  return true;
}

Q4T_TEST(http_observation_first_write_deadline_and_receive_once) {
  SocketPair sockets;
  int buffer = 1024;
  setsockopt(sockets.fd[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
  HttpTransportMetrics metrics;
  {
    HttpResponseContext context(sockets.fd[0], &metrics);
    context.RecordReceiveFailure(HttpReceiveFailure::kMalformed, 400);
    context.RecordReceiveFailure(HttpReceiveFailure::kMedia, 415);
    Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], context.State()));
    const std::string payload(1024 * 1024, 'x');
    Q4T_CHECK(!WriteAll(sockets.fd[0], payload, {Clock::now() + 40ms}));
    const auto first = context.WriteFailure();
    Q4T_CHECK(first.reason == HttpWriteFailure::kWriteDeadline);
    Q4T_CHECK(first.origin == HttpWriteOrigin::kHandler);
    Q4T_CHECK(first.transferred > 0 && first.transferred < payload.size());
    Q4T_CHECK(!WriteAll(sockets.fd[0], "must not buy another write window"));
    const auto after = context.WriteFailure();
    Q4T_CHECK(first.reason == after.reason &&
              first.transferred == after.transferred);
    Q4T_CHECK(WriteCount(metrics) == 0);
  }
  Q4T_CHECK(WriteCount(metrics) == 1);
  Q4T_CHECK(
      metrics.receive[static_cast<size_t>(HttpReceiveFailure::kMalformed)] ==
      1);
  Q4T_CHECK(metrics.receive[static_cast<size_t>(HttpReceiveFailure::kMedia)] ==
            0);
  return true;
}

Q4T_TEST(http_observation_write_cancellation_and_deadline_priority) {
  for (const auto& fixture :
       {std::pair{RequestControl::State::kExplicit,
                  HttpWriteFailure::kExplicitCancel},
        {RequestControl::State::kDisconnect, HttpWriteFailure::kPeerClosed},
        {RequestControl::State::kShutdown, HttpWriteFailure::kShutdown},
        {RequestControl::State::kFailure, HttpWriteFailure::kRequestFailure},
        {RequestControl::State::kDeadline,
         HttpWriteFailure::kRequestDeadline}}) {
    SocketPair sockets;
    HttpTransportMetrics metrics;
    {
      HttpResponseContext context(sockets.fd[0], &metrics);
      RequestControl control("id", "key", Clock::now() - 1ms);
      Q4T_CHECK(control.Cancel(fixture.first));
      Q4T_CHECK(
          !WriteAll(sockets.fd[0], "unsent", {Clock::now() - 1ms, &control}));
      Q4T_CHECK(context.WriteFailure().reason == fixture.second);
      Q4T_CHECK(context.WriteFailure().transferred == 0);
    }
    Q4T_CHECK(WriteCount(metrics) == 1);
  }
  SocketPair sockets;
  HttpResponseContext context(sockets.fd[0]);
  RequestControl natural("id", "key", Clock::now() - 1ms);
  Q4T_CHECK(!WriteAll(sockets.fd[0], "unsent", {.control = &natural}));
  Q4T_CHECK(context.WriteFailure().reason ==
            HttpWriteFailure::kRequestDeadline);
  return true;
}

Q4T_TEST(http_observation_peer_and_invalid_socket_distinct) {
  SocketPair sockets;
  close(sockets.fd[1]);
  sockets.fd[1] = -1;
  HttpResponseContext peer(sockets.fd[0]);
  Q4T_CHECK(!WriteAll(sockets.fd[0], "x"));
  Q4T_CHECK(peer.WriteFailure().reason == HttpWriteFailure::kPeerClosed);
  Q4T_CHECK(peer.WriteFailure().system_error == EPIPE ||
            peer.WriteFailure().system_error == ECONNRESET);
  HttpResponseContext invalid(2147483647);
  Q4T_CHECK(!WriteAll(2147483647, "x"));
  Q4T_CHECK(invalid.WriteFailure().reason == HttpWriteFailure::kSocket);
  Q4T_CHECK(invalid.WriteFailure().system_error == EBADF);
  return true;
}

Q4T_TEST(http_observation_heartbeat_first_failure_after_join) {
  SocketPair sockets;
  HttpTransportMetrics metrics;
  {
    HttpResponseContext context(sockets.fd[0], &metrics);
    Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], context.State()));
    auto control =
        std::make_shared<RequestControl>("id", "key", Clock::now() + 2s);
    close(sockets.fd[1]);
    sockets.fd[1] = -1;
    context.StartHeartbeats(control, 5ms);
    const auto end = Clock::now() + 1s;
    while (context.WriteFailure().reason == HttpWriteFailure::kNone &&
           Clock::now() < end)
      std::this_thread::sleep_for(1ms);
    context.StopHeartbeats();
    const auto failure = context.WriteFailure();
    Q4T_CHECK(failure.reason == HttpWriteFailure::kPeerClosed);
    Q4T_CHECK(failure.origin == HttpWriteOrigin::kHeartbeat);
    Q4T_CHECK(control->Cancelled());
    Q4T_CHECK(!WriteAll(sockets.fd[0], "duplicate"));
    Q4T_CHECK(WriteCount(metrics) == 0);
  }
  Q4T_CHECK(WriteCount(metrics) == 1);
  Q4T_CHECK(metrics.write[static_cast<size_t>(HttpWriteOrigin::kHeartbeat)]
                         [static_cast<size_t>(HttpWriteFailure::kPeerClosed)] ==
            1);
  return true;
}
