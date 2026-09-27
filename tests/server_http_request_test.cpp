#include "q4t/server/http_request.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <string>
#include <thread>

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
  Q4T_CHECK(Read("POST / HTTP/1.1\r\nHost: local\r\n"
                 "Content-Length: 16777216\r\n\r\n" + data, &body));
  Q4T_CHECK(body == data);
  Q4T_CHECK(Read("POST / HTTP/1.1\r\nHost: local\r\n"
                 "Content-Length: 2\r\n\r\n{}trailing", &body));
  Q4T_CHECK(body == "{}");  // Caller closes without dispatching another request.
  return true;
}
