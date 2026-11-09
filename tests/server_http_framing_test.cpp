#include "q4t/server/http_request.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <thread>

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
const std::string kHead =
    "POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\n";
const std::string kChunk = kHead + "Transfer-Encoding: chunked\r\n\r\n";

struct Reader {
  std::string input{};
  size_t offset = 0;
  size_t split = 0;
  size_t step = 4096;
  std::string method{}, path{}, body{};
  HttpRequestHeaders headers{};
  HttpRequestState state{};
  int status = 0;
  int interim = 0;

  bool Read(HttpBodyLease* lease = nullptr,
            const std::function<bool()>& admit = {}) {
    return ReadHttpRequest(
        -1, &method, &path, &body, 1000, lease, &status, &headers, admit,
        [&](void* data, size_t size, auto) -> ssize_t {
          size = std::min({size, step, input.size() - offset});
          if (offset < split) size = std::min(size, split - offset);
          std::memcpy(data, input.data() + offset, size);
          offset += size;
          return size;
        },
        &state,
        [&](auto) {
          ++interim;
          return true;
        });
  }
};
struct Sockets {
  int fd[2]{-1, -1};
  Sockets() {
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fd) != 0)
      throw std::runtime_error("socketpair");
    timeval timeout{1, 0};
    ::setsockopt(fd[0], SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  }
  ~Sockets() {
    ::close(fd[0]);
    ::close(fd[1]);
  }
};
}  // namespace

Q4T_TEST(http_framing_chunked_partitions_and_binary) {
  const std::string binary("{\0\xff\r\n}", 6);
  const auto wire = kChunk + "6\r\n" + binary + "\r\n0\r\n\r\n";
  for (size_t split = 0; split <= wire.size(); ++split) {
    Reader reader{.input = wire, .split = split};
    Q4T_CHECK(reader.Read() && reader.body == binary);
    Q4T_CHECK(reader.headers.content_length == binary.size());
    Q4T_CHECK(reader.state.pending.empty());
  }
  Reader bytewise{.input = wire, .step = 1};
  Q4T_CHECK(bytewise.Read() && bytewise.body == binary);
  return true;
}

Q4T_TEST(http_framing_extensions_and_trailers_remain_separate) {
  Reader reader{
      .input = kHead +
               "Transfer-Encoding: cHuNkEd\r\nTrailer: Digest, X-Meta\r\n\r\n"
               "2 ; name = \"a\\\"b\" ; flag\r\n{}\r\n0;end=ok\r\n"
               "Digest: sha-256=fixture\r\nX-Meta: x\r\n\r\n",
      .step = 1};
  Q4T_CHECK(reader.Read() && reader.body == "{}");
  Q4T_CHECK(reader.headers.trailers.size() == 2);
  Q4T_CHECK(reader.headers.trailers[0].first == "digest");
  Q4T_CHECK(reader.headers.trailers[1].second == "x");
  Q4T_CHECK(reader.headers.authorization.empty());
  return true;
}

Q4T_TEST(http_framing_pipeline_keeps_exact_unconsumed_bytes) {
  const auto next =
      "GET /v1/models HTTP/1.1\r\nHost: local\r\n"
      "Authorization: Bearer second\r\n\r\n";
  for (bool chunked : {false, true}) {
    const auto first = chunked ? kChunk + "2\r\n{}\r\n0\r\n\r\n"
                               : kHead + "Content-Length: 2\r\n\r\n{}";
    Reader reader{.input = first + next};
    int admissions = 0;
    Q4T_CHECK(reader.Read(nullptr, [&] {
      ++admissions;
      return true;
    }));
    Q4T_CHECK(reader.body == "{}" && reader.state.pending == next);
    Q4T_CHECK(reader.Read(nullptr, [&] {
      ++admissions;
      return true;
    }));
    Q4T_CHECK(reader.body.empty() && reader.path == "/v1/models");
    Q4T_CHECK(reader.headers.authorization == "Bearer second");
    Q4T_CHECK(reader.state.pending.empty() && admissions == 2);
  }
  Reader prefixed{.input =
                      "POST /prefix/v1/completions HTTP/1.1\r\n"
                      "Host: local\r\nContent-Length: 5000\r\n\r\n" +
                      std::string(5000, 'x')};
  Q4T_CHECK(prefixed.Read(nullptr, [&] {
    prefixed.path = "/v1/completions";
    return true;
  }));
  Q4T_CHECK(prefixed.body.size() == 5000);
  return true;
}

Q4T_TEST(http_framing_version_and_connection_lifetime) {
  for (const auto version : {"1.0", "1.1"}) {
    for (const auto fields :
         {"", "Connection: keep-alive\r\n", "Connection: close\r\n",
          "Connection: keep-alive, Close\r\n",
          "Connection: keep-alive\r\nConnection: CLOSE\r\n"}) {
      Reader reader{.input = "GET / HTTP/" + std::string(version) +
                             "\r\nHost: local\r\n" + fields + "\r\n"};
      Q4T_CHECK(reader.Read());
      const std::string_view value(fields);
      const bool expected =
          value.find("lose") == value.npos &&
          value.find("CLOSE") == value.npos &&
          (std::string_view(version) == "1.1" || !value.empty());
      Q4T_CHECK(reader.headers.keep_alive == expected);
    }
  }
  Reader old_expect{.input =
                        "POST / HTTP/1.0\r\nExpect: extension\r\n"
                        "Content-Length: 2\r\n\r\n{}"};
  Q4T_CHECK(old_expect.Read() && old_expect.interim == 0);
  Reader unknown{.input = kHead + "Expect: extension\r\n\r\n"};
  Q4T_CHECK(!unknown.Read() && unknown.status == 417 && unknown.interim == 0);
  return true;
}

Q4T_TEST(http_framing_continue_follows_admission_and_reservation) {
  for (bool chunked : {false, true}) {
    Sockets sockets;
    const auto head = kHead + "Expect: 100-continue\r\n" +
                      (chunked ? "Transfer-Encoding: chunked\r\n\r\n"
                               : "Content-Length: 2\r\n\r\n");
    Q4T_CHECK(::send(sockets.fd[0], head.data(), head.size(), MSG_NOSIGNAL) >
              0);
    bool observed = false;
    std::thread sender([&] {
      std::string response;
      char byte;
      while (response.size() < 25 && ::recv(sockets.fd[0], &byte, 1, 0) == 1)
        response += byte;
      observed = response == "HTTP/1.1 100 Continue\r\n\r\n";
      const std::string payload = chunked ? "2\r\n{}\r\n0\r\n\r\n" : "{}";
      ::send(sockets.fd[0], payload.data(), payload.size(), MSG_NOSIGNAL);
    });
    HttpBodyBudget budget(2);
    HttpBodyLease lease(budget);
    HttpRequestHeaders headers;
    std::string method, path, body;
    int status = 0;
    bool admitted = false;
    const bool ok = ReadHttpRequest(sockets.fd[1], &method, &path, &body, 1500,
                                    &lease, &status, &headers, [&] {
                                      admitted = budget.Used() == 0 &&
                                                 headers.expect_continue;
                                      return admitted;
                                    });
    sender.join();
    Q4T_CHECK(ok && observed && admitted && body == "{}" && budget.Used() == 2);
  }
  return true;
}

Q4T_TEST(http_framing_rejection_before_continue) {
  for (int reject = 0; reject < 4; ++reject) {
    Reader reader{.input = kHead +
                           "Expect: 100-continue\r\n"
                           "Content-Length: 3\r\n\r\n"};
    HttpBodyBudget budget(reject == 2 ? 2 : 3);
    HttpBodyLease lease(budget);
    Q4T_CHECK(!reader.Read(&lease, [&] {
      if (reject == 0) return false;
      if (reject == 1) reader.headers.body_limit = 2;
      if (reject == 3) reader.path = "/unknown";
      return true;
    }));
    if (reject < 3)
      Q4T_CHECK(reader.interim == 0 && budget.Used() == 0);
    else
      Q4T_CHECK(reader.interim == 1 &&
                reader.headers.failure == HttpReceiveFailure::kBodyEof);
  }
  Reader empty{.input = kHead + "Expect: 100-continue\r\n\r\n"};
  Q4T_CHECK(empty.Read() && empty.interim == 0);
  return true;
}

Q4T_TEST(http_framing_chunked_capacity_and_unwind) {
  for (bool client_limit : {false, true}) {
    HttpBodyBudget budget(client_limit ? 4 : 3);
    {
      HttpBodyLease lease(budget);
      Reader reader{.input = kChunk + "2\r\n{}\r\n2\r\n"};
      Q4T_CHECK(!reader.Read(&lease, [&] {
        reader.headers.body_limit = client_limit ? 3 : 4;
        return true;
      }));
      Q4T_CHECK(reader.status == (client_limit ? 413 : 503));
      Q4T_CHECK(reader.body == "{}" && budget.Used() == 2);
    }
    Q4T_CHECK(budget.Used() == 0);
  }
  Reader huge{.input = kChunk + "1000001\r\n"};
  Q4T_CHECK(!huge.Read() && huge.status == 413 && huge.body.empty());
  return true;
}

Q4T_TEST(http_framing_bad_chunks_truncation_and_smuggling) {
  for (const auto payload :
       {"", "2\r\n{", "2\r\n{}", "2\r\n{}\n0\r\n\r\n", "+2\r\n{}\r\n0\r\n\r\n",
        "0x2\r\n{}\r\n0\r\n\r\n", " 0\r\n\r\n", "ffffffffffffffffffffffff\r\n",
        "0;=x\r\n\r\n", "0;x=\"bad\r\n\r\n", "0;x=\r\n\r\n",
        "0\r\nAuthorization: Bearer forged\r\n\r\n",
        "0\r\nX-Q4T-Client-Id: victim\r\n\r\n",
        "0\r\nContent-Length: 9\r\n\r\n", "0\r\nBad : field\r\n\r\n",
        "0\r\nContinuation\r\n\r\n"}) {
    Reader reader{.input = kChunk + payload, .step = 1};
    Q4T_CHECK(!reader.Read());
  }
  for (const auto field :
       {"Content-Length: 0", "Transfer-Encoding: chunked",
        "Trailer: Authorization", "Connection: Content-Length",
        "Connection: host", "Trailer: X-Q4T-Gateway-Token",
        "Connection: X-Private\r\nTrailer: X-Private"}) {
    Reader reader{.input = kHead + "Transfer-Encoding: chunked\r\n" + field +
                           "\r\n\r\n0\r\n\r\n"};
    Q4T_CHECK(!reader.Read() && reader.interim == 0);
  }
  Reader line{.input = kChunk + "0;x=" + std::string(8192, 'x') + "\r\n\r\n"};
  Q4T_CHECK(!line.Read());
  std::string trailers;
  for (int i = 0; i < 129; ++i) trailers += "Digest: x\r\n";
  Reader count{.input = kChunk + "0\r\n" + trailers + "\r\n"};
  Q4T_CHECK(!count.Read());
  Reader total{.input = kChunk};
  for (int i = 0; i < 9; ++i)
    total.input += "1;x=" + std::string(8000, 'x') + "\r\nx\r\n";
  total.input += "0\r\n\r\n";
  Q4T_CHECK(!total.Read());
  return true;
}

Q4T_TEST(http_framing_chunked_absolute_deadline) {
  Sockets sockets;
  Q4T_CHECK(::send(sockets.fd[0], kChunk.data(), kChunk.size(), MSG_NOSIGNAL) >
            0);
  std::thread sender([&] {
    for (int i = 0; i < 5; ++i) {
      std::this_thread::sleep_for(30ms);
      if (::send(sockets.fd[0], "1\r\nx\r\n", 6, MSG_NOSIGNAL) < 0) break;
    }
  });
  HttpBodyBudget budget(8);
  HttpBodyLease lease(budget);
  HttpRequestHeaders headers;
  std::string method, path, body;
  int status = 0;
  const bool ok = ReadHttpRequest(sockets.fd[1], &method, &path, &body, 80,
                                  &lease, &status, &headers);
  ::shutdown(sockets.fd[1], SHUT_RDWR);
  sender.join();
  Q4T_CHECK(!ok && status == 408);
  Q4T_CHECK(headers.failure == HttpReceiveFailure::kBodyDeadline);
  Q4T_CHECK(budget.Used() == body.size() && !body.empty());
  return true;
}
