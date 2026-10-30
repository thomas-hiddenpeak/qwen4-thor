// Link the actual response implementation with wrapped send/operator new.
#include "q4t/server/http_response.h"
#include "q4t/server/serve_fault.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <new>
#include <string>

namespace {
thread_local bool deny_allocation = false;
int short_write = 0;
const char* exception_point = nullptr;

struct Fixture {
  int fd[2]{-1, -1};
  Fixture() {
    socketpair(AF_UNIX, SOCK_STREAM, 0, fd);
    q4t::server::detail::serve_test_hook = +[](const char* point) {
      if (exception_point && std::strcmp(point, exception_point) == 0)
        throw std::bad_alloc();
    };
  }
  ~Fixture() {
    deny_allocation = false;
    short_write = 0;
    exception_point = nullptr;
    q4t::server::detail::serve_test_hook = nullptr;
    for (int value : fd) if (value >= 0) close(value);
  }
  std::string Read() {
    shutdown(fd[0], SHUT_WR);
    std::string result;
    std::array<char, 1024> bytes{};
    for (;;) {
      const auto n = recv(fd[1], bytes.data(), bytes.size(), 0);
      if (n <= 0) return result;
      result.append(bytes.data(), static_cast<size_t>(n));
    }
  }
};
}  // namespace

extern "C" ssize_t __real_send(int, const void*, size_t, int);
extern "C" ssize_t __wrap_send(int fd, const void* data, size_t len, int flags) {
  if (short_write == 1) {
    short_write = 2;
    return __real_send(fd, data, std::min(len, size_t{8}), flags);
  }
  if (short_write == 2) {
    short_write = 0;
    errno = EPIPE;
    return -1;
  }
  return __real_send(fd, data, len, flags);
}
extern "C" void* __real__Znwm(size_t);
extern "C" void* __wrap__Znwm(size_t bytes) {
  if (deny_allocation) throw std::bad_alloc();
  return __real__Znwm(bytes);
}

Q4T_TEST(header_production_exception_transitions) {
  using namespace q4t::server::detail;
  for (const char* point : {"handler.before_header", "handler.partial_header",
                            "handler.after_header"}) {
    Fixture f;
    exception_point = point;
    if (std::strcmp(point, "handler.partial_header") == 0) short_write = 1;
    ResponseState state = ResponseState::kUnsent;
    bool caught = false;
    try {
      BeginStreamingResponse(f.fd[0], &state);
    } catch (const std::bad_alloc&) {
      caught = true;
    }
    short_write = 0;
    exception_point = nullptr;
    Q4T_CHECK(caught);
    SendExceptionResponse(f.fd[0], state);
    const std::string wire = f.Read();
    if (std::strcmp(point, "handler.before_header") == 0) {
      Q4T_CHECK(state == ResponseState::kUnsent);
      Q4T_CHECK(wire.starts_with("HTTP/1.1 500 "));
      Q4T_CHECK(wire.find("data:") == std::string::npos);
    } else if (std::strcmp(point, "handler.partial_header") == 0) {
      Q4T_CHECK(state == ResponseState::kHeadersStarted);
      Q4T_CHECK(wire == "HTTP/1.1");
    } else {
      Q4T_CHECK(state == ResponseState::kStreaming);
      Q4T_CHECK(wire.starts_with("HTTP/1.1 200 OK\r\n"));
      Q4T_CHECK(wire.find("\r\n\r\ndata: {\"error\":") != std::string::npos);
      Q4T_CHECK(wire.ends_with("data: [DONE]\r\n\r\n"));
      Q4T_CHECK(wire.find("HTTP/", 1) == std::string::npos);
    }
  }
  return true;
}

Q4T_TEST(header_production_partial_failure) {
  using namespace q4t::server::detail;
  Fixture f;
  short_write = 1;
  ResponseState state = ResponseState::kUnsent;
  Q4T_CHECK(!BeginStreamingResponse(f.fd[0], &state));
  Q4T_CHECK(state == ResponseState::kHeadersStarted);
  SendExceptionResponse(f.fd[0], state);
  Q4T_CHECK(f.Read() == "HTTP/1.1");
  return true;
}

Q4T_TEST(header_production_no_allocation_and_terminal) {
  using namespace q4t::server::detail;
  Fixture f;
  ResponseState state = ResponseState::kUnsent;
  // Prove the interceptor is active before checking the production helper.
  deny_allocation = true;
  bool intercepted = false;
  try { (void)::operator new(1024); }
  catch (const std::bad_alloc&) { intercepted = true; }
  Q4T_CHECK(intercepted);
  const bool sent = BeginStreamingResponse(f.fd[0], &state);
  deny_allocation = false;
  Q4T_CHECK(sent && state == ResponseState::kStreaming);
  const auto finish = SseChunk("id", "model", "", "", "stop", 0);
  state = ResponseState::kTerminal;
  Q4T_CHECK(WriteAll(f.fd[0], finish));
  SendExceptionResponse(f.fd[0], state);
  const auto wire = f.Read();
  Q4T_CHECK(wire.ends_with(finish));
  Q4T_CHECK(wire.find("error") == std::string::npos);
  Q4T_CHECK(wire.find("[DONE]") == std::string::npos);
  return true;
}
