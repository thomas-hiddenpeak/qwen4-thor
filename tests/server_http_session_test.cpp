#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <charconv>
#include <stdexcept>
#include <thread>

namespace {
using namespace q4t::server;
using namespace q4t::server::detail;
using namespace std::chrono_literals;

struct Pair {
  int fd[2];
  Pair() {
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fd))
      throw std::runtime_error("socketpair");
  }
  ~Pair() {
    ::close(fd[0]);
    ::close(fd[1]);
  }
  std::string Take() {
    std::string result;
    char data[4096];
    for (;;) {
      const auto n = ::recv(fd[1], data, sizeof(data), MSG_DONTWAIT);
      if (n <= 0) return result;
      result.append(data, n);
    }
  }
};

// Independent wire consumer: reads exactly one HTTP chunk sequence, without
// relying on the production framing helper or application SSE delimiters.
bool Decode(std::string_view wire, std::string* decoded) {
  const auto head = wire.find("\r\n\r\n");
  if (head == wire.npos) return false;
  wire.remove_prefix(head + 4);
  while (!wire.empty()) {
    const auto end = wire.find("\r\n");
    if (end == wire.npos) return false;
    size_t size = 0;
    const auto parsed =
        std::from_chars(wire.data(), wire.data() + end, size, 16);
    if (parsed.ec != std::errc{} || parsed.ptr != wire.data() + end)
      return false;
    wire.remove_prefix(end + 2);
    if (!size) return wire == "\r\n";
    if (size > wire.size() || wire.substr(size, 2) != "\r\n") return false;
    decoded->append(wire.substr(0, size));
    wire.remove_prefix(size + 2);
  }
  return false;
}
}  // namespace

Q4T_TEST(http_session_json_error_reuse_has_new_context) {
  Pair pair;
  std::string previous;
  for (bool error : {false, true, false}) {
    HttpResponseContext context(pair.fd[0]);
    context.SetKeepAlive(true);
    Q4T_CHECK(context.TraceId() != previous);
    previous = context.TraceId();
    if (error)
      SendError(pair.fd[0], 429, "quota", {}, "client_rpm_exhausted");
    else
      Q4T_CHECK(SendSimple(pair.fd[0], 200, "OK", "{}", "application/json"));
    Q4T_CHECK(context.FinishResponse());
    Q4T_CHECK(!context.FinishResponse());
    const auto wire = pair.Take();
    Q4T_CHECK(wire.find("Connection: keep-alive\r\n") != wire.npos);
    Q4T_CHECK(wire.find(previous) != wire.npos && wire.ends_with('}'));
  }
  return true;
}

Q4T_TEST(http_session_sse_chunks_serialize_heartbeat_and_terminal) {
  Pair pair;
  HttpResponseContext context(pair.fd[0]);
  context.SetKeepAlive(true);
  Q4T_CHECK(BeginStreamingResponse(pair.fd[0], context.State()));
  auto control = std::make_shared<RequestControl>(
      "stream", "", std::chrono::steady_clock::now() + 2s);
  context.StartHeartbeats(control, 5ms);
  Q4T_CHECK(WriteAll(pair.fd[0], "data: one\r\n\r\n"));
  std::this_thread::sleep_for(20ms);
  context.StopHeartbeats();
  *context.State() = ResponseState::kTerminal;
  Q4T_CHECK(WriteAll(pair.fd[0], "data: [DONE]\r\n\r\n"));
  Q4T_CHECK(context.FinishResponse());
  const auto wire = pair.Take();
  std::string decoded;
  Q4T_CHECK(wire.find("Transfer-Encoding: chunked\r\n") != wire.npos);
  Q4T_CHECK(Decode(wire, &decoded));
  Q4T_CHECK(decoded.starts_with("data: one\r\n\r\n"));
  Q4T_CHECK(decoded.find(": keep-alive\r\n\r\n") != decoded.npos);
  Q4T_CHECK(decoded.ends_with("data: [DONE]\r\n\r\n"));
  return true;
}

Q4T_TEST(http_session_partial_or_failed_stream_cannot_finish) {
  for (bool fail_write : {false, true}) {
    Pair pair;
    HttpResponseContext context(pair.fd[0]);
    context.SetKeepAlive(true);
    Q4T_CHECK(BeginStreamingResponse(pair.fd[0], context.State()));
    Q4T_CHECK(WriteAll(pair.fd[0], "data: partial\r\n\r\n"));
    if (fail_write) {
      *context.State() = ResponseState::kTerminal;
      Q4T_CHECK(!WriteAll(pair.fd[0], "data: [DONE]\r\n\r\n",
                          {std::chrono::steady_clock::now() - 1ms}));
    }
    Q4T_CHECK(!context.FinishResponse());
    const auto wire = pair.Take();
    std::string decoded;
    Q4T_CHECK(!Decode(wire, &decoded));
    Q4T_CHECK(wire.find("data: [DONE]") == wire.npos);
    Q4T_CHECK(context.WriteFailure().reason ==
              (fail_write ? HttpWriteFailure::kWriteDeadline
                          : HttpWriteFailure::kNone));
  }
  return true;
}

Q4T_TEST(http_session_http10_stream_closes_but_length_can_reuse) {
  for (bool stream : {false, true}) {
    Pair pair;
    HttpResponseContext context(pair.fd[0]);
    context.SetKeepAlive(true, false);
    if (stream) {
      Q4T_CHECK(BeginStreamingResponse(pair.fd[0], context.State()));
      *context.State() = ResponseState::kTerminal;
      Q4T_CHECK(WriteAll(pair.fd[0], "data: [DONE]\r\n\r\n"));
    } else {
      Q4T_CHECK(SendSimple(pair.fd[0], 200, "OK", "{}", "application/json"));
    }
    Q4T_CHECK(context.FinishResponse() == !stream);
    const auto wire = pair.Take();
    Q4T_CHECK(wire.find("Transfer-Encoding:") == wire.npos);
    Q4T_CHECK(wire.find(stream ? "Connection: close"
                               : "Connection: keep-alive") != wire.npos);
  }
  return true;
}

Q4T_TEST(http_session_stream_error_finishes_once) {
  Pair pair;
  HttpResponseContext context(pair.fd[0]);
  context.SetKeepAlive(true);
  Q4T_CHECK(BeginStreamingResponse(pair.fd[0], context.State()));
  SendStreamError(pair.fd[0], context.State(), 502, "bad output", {},
                  "response_parse_failed");
  Q4T_CHECK(context.FinishResponse());
  SendStreamError(pair.fd[0], context.State(), 500, "again", {}, "again");
  Q4T_CHECK(!context.FinishResponse());
  std::string decoded;
  Q4T_CHECK(Decode(pair.Take(), &decoded));
  Q4T_CHECK(decoded.find("response_parse_failed") != decoded.npos);
  Q4T_CHECK(decoded.find("again") == decoded.npos);
  Q4T_CHECK(decoded.ends_with("data: [DONE]\r\n\r\n"));
  return true;
}

Q4T_TEST(http_session_idle_deadline_and_pending_input) {
  Pair pair;
  HttpRequestState state;
  Q4T_CHECK(!AwaitHttpRequest(pair.fd[0], &state, 20));
  Q4T_CHECK(::send(pair.fd[1], "GET /", 5, MSG_NOSIGNAL) == 5);
  Q4T_CHECK(AwaitHttpRequest(pair.fd[0], &state, 20));
  Q4T_CHECK(state.pending == "G");
  Q4T_CHECK(AwaitHttpRequest(pair.fd[0], &state, 0));
  char tail[8];
  Q4T_CHECK(::recv(pair.fd[0], tail, sizeof(tail), MSG_DONTWAIT) == 4);
  Q4T_CHECK(std::string_view(tail, 4) == "ET /");
  state.pending.clear();
  ::shutdown(pair.fd[1], SHUT_WR);
  Q4T_CHECK(!AwaitHttpRequest(pair.fd[0], &state, 20));
  bool used = false;
  Q4T_CHECK(AwaitHttpRequest(-1, &state, 20, [&](void* out, size_t size, auto) {
    used = size == 1;
    *static_cast<char*>(out) = 'G';
    return 1;
  }));
  Q4T_CHECK(used && state.pending == "G");
  return true;
}

Q4T_TEST(http_close_sends_final_response_before_draining_late_body) {
  Pair pair;
  HttpResponseContext context(pair.fd[0]);
  SendError(pair.fd[0], 409, "Upload is cancelled", {}, "upload_cancelled");
  std::thread ending([&] { DrainHttpClose(pair.fd[0]); });
  char wire[4096];
  const auto received = ::recv(pair.fd[1], wire, sizeof(wire), 0);
  const bool response = received > 0 &&
      std::string_view(wire, received).find("upload_cancelled") !=
          std::string_view::npos;
  // A final response is already available even if the upload body is late.
  const std::string late = "POST /never-dispatch HTTP/1.1\r\n\r\n";
  const auto sent = ::send(pair.fd[1], late.data(), late.size(), MSG_NOSIGNAL);
  ::shutdown(pair.fd[1], SHUT_WR);
  ending.join();
  Q4T_CHECK(response && sent == static_cast<ssize_t>(late.size()));
  Q4T_CHECK(::recv(pair.fd[0], wire, sizeof(wire), MSG_DONTWAIT) == 0);
  Q4T_CHECK(::recv(pair.fd[1], wire, sizeof(wire), 0) == 0);
  return true;
}

Q4T_TEST(http_close_drain_bounds_idle_time_and_unparsed_tail_bytes) {
  Pair idle;
  auto start = std::chrono::steady_clock::now();
  DrainHttpClose(idle.fd[0]);
  Q4T_CHECK(std::chrono::steady_clock::now() - start < 1s);
  Pair large;
  const std::string tail(65536 + 4096, 'x');
  Q4T_CHECK(::send(large.fd[1], tail.data(), tail.size(), MSG_NOSIGNAL) ==
            static_cast<ssize_t>(tail.size()));
  start = std::chrono::steady_clock::now();
  DrainHttpClose(large.fd[0]);
  Q4T_CHECK(std::chrono::steady_clock::now() - start < 1s);
  char remaining[8192];
  Q4T_CHECK(::recv(large.fd[0], remaining, sizeof(remaining), MSG_DONTWAIT) ==
            4096);
  return true;
}
