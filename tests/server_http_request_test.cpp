#include "q4t/server/http_request.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <barrier>
#include <string>
#include <thread>
#include <vector>

namespace {

bool Read(const std::string& wire, std::string* body) {
  int fd[2];
  if (socketpair(AF_UNIX, SOCK_STREAM, 0, fd) != 0) return false;
  // A separate writer also exercises partial reads and requests > socket buf.
  std::thread writer([&] {
    size_t offset = 0;
    while (offset < wire.size()) {
      const ssize_t n = send(fd[0], wire.data() + offset,
                             wire.size() - offset, MSG_NOSIGNAL);
      if (n <= 0) break;
      offset += static_cast<size_t>(n);
    }
    shutdown(fd[0], SHUT_WR);
  });
  std::string method, path;
  const bool ok = q4t::server::ReadHttpRequest(fd[1], &method, &path, body);
  shutdown(fd[1], SHUT_RDWR);
  writer.join();
  close(fd[0]);
  close(fd[1]);
  return ok;
}

}  // namespace

Q4T_TEST(http_connection_classification_exact_route_and_method) {
  using namespace q4t::server;
  for (const auto path : {"/metrics", "/v1/config", "/v1/capabilities",
                           "/v1/models"}) {
    Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
              HttpConnectionClass::kObservation);
    for (const auto method : {"POST", "HEAD", "get"})
      Q4T_CHECK(ClassifyHttpConnection(method, path) ==
                HttpConnectionClass::kBusiness);
    for (const auto suffix : {"?x=1", "/", "/anything"})
      Q4T_CHECK(ClassifyHttpConnection("GET", std::string(path) + suffix) ==
                HttpConnectionClass::kBusiness);
  }
  for (const auto path : {"/healthz", "/livez", "/readyz"})
    Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
              HttpConnectionClass::kControl);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/requests/cancel") ==
            HttpConnectionClass::kControl);
  Q4T_CHECK(ClassifyHttpConnection("GET", "/v1/requests/cancel") ==
            HttpConnectionClass::kBusiness);
  return true;
}

Q4T_TEST(http_connection_atomic_limit_and_reuse) {
  using q4t::server::AcquireHttpConnection;
  std::atomic<int> count{0}, successes{0};
  std::barrier start(16), acquired(16);
  std::vector<std::thread> callers;
  for (int i = 0; i < 16; ++i) {
    callers.emplace_back([&] {
      start.arrive_and_wait();
      const bool admitted = AcquireHttpConnection(count, 4);
      if (admitted) successes.fetch_add(1);
      acquired.arrive_and_wait();  // No release until every attempt finished.
      if (admitted) count.fetch_sub(1);
    });
  }
  for (auto& caller : callers) caller.join();
  Q4T_CHECK(successes.load() == 4 && count.load() == 0);
  for (int i = 0; i < 4; ++i) Q4T_CHECK(AcquireHttpConnection(count, 4));
  Q4T_CHECK(!AcquireHttpConnection(count, 4) && count.load() == 4);
  return true;
}

Q4T_TEST(http_valid_length_and_header_names) {
  std::string body;
  Q4T_CHECK(Read("POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\n"
                 "cOnTeNt-LeNgTh: \t2 \r\nX-Content-Length: 99\r\n\r\n{}",
                 &body));
  Q4T_CHECK(body == "{}");
  Q4T_CHECK(Read("GET /healthz HTTP/1.0\r\n\r\n", &body));
  Q4T_CHECK(body.empty());
  Q4T_CHECK(Read("POST / HTTP/1.1\r\nHost: local\r\n"
                 "X-Content-Length: 2\r\n\r\n{}", &body));
  Q4T_CHECK(body.empty());  // Unknown field must not become framing.
  return true;
}

Q4T_TEST(http_rejects_ambiguous_framing) {
  std::string body;
  for (const char* fields : {
           "Content-Length: 2junk", "Content-Length: +2",
           "Content-Length: -2", "Content-Length: 2,2",
           "Content-Length: 99999999999999999999999999999999",
           "Content-Length: 16777217", "Content-Length:",
           "Content-Length: 2\r\nContent-Length: 99",
           "Content-Length: 2\r\nContent-Length: 2",
           "Transfer-Encoding: chunked",
           "Content-Length: 2\r\nTransfer-Encoding: chunked",
           "Expect: 100-continue", " Content-Length: 2",
           "Content-Length : 2", "BrokenHeader", "Host: second"}) {
    Q4T_CHECK(!Read(std::string("POST / HTTP/1.1\r\nHost: local\r\n") +
                       fields + "\r\n\r\n{}", &body));
  }
  Q4T_CHECK(!Read("POST / HTTP/1.1\r\nHost: local\r\n"
                  "Content-Length: 3\r\n\r\n{}", &body));
  return true;
}

Q4T_TEST(http_request_line_and_header_budget) {
  std::string body;
  for (const char* line : {"POST / HTTP/9", "POST  / HTTP/1.1",
                           "POST http://local/ HTTP/1.1", "POST /#x HTTP/1.1"})
    Q4T_CHECK(!Read(std::string(line) + "\r\nHost: local\r\n\r\n", &body));
  Q4T_CHECK(!Read("GET / HTTP/1.1\r\n\r\n", &body));
  const std::string prefix = "GET / HTTP/1.1\r\nHost: local\r\nX: ";
  Q4T_CHECK(Read(prefix + std::string(65536 - prefix.size() - 4, 'x') +
                    "\r\n\r\n", &body));
  Q4T_CHECK(!Read(prefix + std::string(65537 - prefix.size() - 4, 'x') +
                     "\r\n\r\n", &body));
  return true;
}

Q4T_TEST(http_body_capacity_and_trailing_bytes) {
  std::string body;
  const std::string data(16 * 1024 * 1024, 'x');
  Q4T_CHECK(Read("POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\n"
                 "Content-Length: 16777216\r\n\r\n" + data, &body));
  Q4T_CHECK(body == data);
  Q4T_CHECK(Read("POST / HTTP/1.1\r\nHost: local\r\n"
                 "Content-Length: 2\r\n\r\n{}trailing", &body));
  Q4T_CHECK(body == "{}");  // Caller closes without dispatching another request.
  return true;
}

Q4T_TEST(http_preflight_body_uses_shared_chat_reservation) {
  using namespace q4t::server;
  std::string body;
  Q4T_CHECK(Read("POST /v1/chat/preflight HTTP/1.1\r\nHost: local\r\n"
                 "Content-Length: 5000\r\n\r\n" + std::string(5000, 'x'), &body));
  Q4T_CHECK(body.size() == 5000);
  HttpBodyBudget budget(8);
  HttpBodyLease chat(budget);
  Q4T_CHECK(chat.Acquire(8));
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  const std::string wire = "POST /v1/chat/preflight HTTP/1.1\r\nHost: local\r\n"
                           "Content-Length: 1\r\n\r\n";
  Q4T_CHECK(send(fd[0], wire.data(), wire.size(), MSG_NOSIGNAL) > 0);
  std::string method, path;
  HttpBodyLease preflight(budget);
  int status = 400;
  const bool ok = ReadHttpRequest(fd[1], &method, &path, &body, 100,
                                   &preflight, &status);
  close(fd[0]);
  close(fd[1]);
  Q4T_CHECK(!ok && status == 503 && budget.Used() == 8);
  return true;
}

Q4T_TEST(http_absolute_deadline_and_body_budget) {
  using namespace q4t::server;
  HttpBodyBudget budget(8);
  {
    HttpBodyLease a(budget), b(budget);
    Q4T_CHECK(a.Acquire(8));
    Q4T_CHECK(!b.Acquire(1));
    Q4T_CHECK(budget.Used() == 8);
  }
  Q4T_CHECK(budget.Used() == 0);
  for (bool body_phase : {false, true}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    std::thread writer([&] {
      const std::string prefix = body_phase ?
          "POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\n"
          "Content-Length: 8\r\n\r\n" : "GET / HTTP/1.1\r\nX: ";
      send(fd[0], prefix.data(), prefix.size(), MSG_NOSIGNAL);
      for (int i = 0; i < 5; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        if (send(fd[0], "x", 1, MSG_NOSIGNAL) < 0) break;
      }
    });
    std::string method, path, body;
    int status = 0;
    bool ok;
    {
      HttpBodyLease lease(budget);
      ok = ReadHttpRequest(fd[1], &method, &path, &body, 90, &lease, &status);
    }
    shutdown(fd[1], SHUT_RDWR);
    writer.join();
    close(fd[0]);
    close(fd[1]);
    Q4T_CHECK(!ok && status == 408 && budget.Used() == 0);
  }
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  const std::string wire = "POST /v1/chat/completions HTTP/1.1\r\n"
                           "Host: local\r\nContent-Length: 9\r\n\r\n";
  send(fd[0], wire.data(), wire.size(), MSG_NOSIGNAL);
  std::string method, path, body;
  HttpBodyLease lease(budget);
  int status = 0;
  const bool ok = ReadHttpRequest(fd[1], &method, &path, &body, 1000,
                                   &lease, &status);
  close(fd[0]);
  close(fd[1]);
  Q4T_CHECK(!ok && status == 503 && budget.Used() == 0);
  return true;
}

Q4T_TEST(http_header_identity_and_pre_body_admission) {
  using namespace q4t::server;
  for (const auto& extra :
       {std::string("X-Request-Id: abc.1:trace\r\n"),
        std::string("X-Request-Id: abc\r\nx-request-id: abc\r\n"),
        std::string("X-Request-Id: bad id\r\n"),
        std::string("X-Request-Id: ") + std::string(129, 'x') + "\r\n",
        std::string("Content-Type: application/json\r\n")}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    const std::string wire =
        "POST /v1/chat/completions HTTP/1.1\r\nHost: t\r\n"
        "Content-Length: 8\r\nContent-Type: application/json\r\n" +
        extra + "\r\n";
    Q4T_CHECK(send(fd[0], wire.data(), wire.size(), MSG_NOSIGNAL) > 0);
    std::string method, path, body;
    HttpRequestHeaders headers;
    HttpBodyBudget budget(8);
    HttpBodyLease lease(budget);
    bool called = false;
    int status = 400;
    const bool ok = ReadHttpRequest(fd[1], &method, &path, &body, 200, &lease,
                                    &status, &headers, [&] {
                                      called = true;
                                      status = 503;
                                      return false;
                                    });
    close(fd[0]);
    close(fd[1]);
    Q4T_CHECK(!ok && body.empty() && budget.Used() == 0);
    if (extra.starts_with("X-Request-Id: abc.1")) {
      Q4T_CHECK(called && status == 503);
      Q4T_CHECK(headers.request_id == "abc.1:trace");
      Q4T_CHECK(headers.content_length == 8);
    } else {
      Q4T_CHECK(!called && status == 400 && !headers.error_param.empty());
    }
  }
  return true;
}
