#include "q4t/server/http_response.h"

#include <sys/socket.h>
#include <poll.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <pthread.h>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using q4t::server::detail::NormalizeUtf8;
using q4t::server::detail::Utf8Stream;

struct SocketPair {
  int fd[2]{-1, -1};
  SocketPair() { socketpair(AF_UNIX, SOCK_STREAM, 0, fd); }
  ~SocketPair() {
    for (int f : fd) if (f >= 0) close(f);
  }
};
}  // namespace

Q4T_TEST(server_http_context_identity_and_single_terminal) {
  using namespace q4t::server::detail;
  for (bool stream : {false, true}) {
    SocketPair sockets;
    HttpResponseContext context(sockets.fd[0]);
    Q4T_CHECK(context.TraceId().starts_with("req-"));
    Q4T_CHECK(context.TraceId() != NewRequestId());
    Q4T_CHECK(!context.SetTraceId("injected\r\nHeader: bad"));
    Q4T_CHECK(context.SetTraceId("client.trace-1"));
    if (stream)
      Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], context.State()));
    SendError(sockets.fd[0], 400, "bad input", {}, "invalid_request", "model");
    SendExceptionResponse(sockets.fd[0], *context.State());
    SendError(sockets.fd[0], 500, "duplicate");
    shutdown(sockets.fd[0], SHUT_WR);
    std::string wire;
    char buf[4096];
    ssize_t n;
    while ((n = recv(sockets.fd[1], buf, sizeof(buf), 0)) > 0)
      wire.append(buf, n);
    Q4T_CHECK(wire.find("X-Request-Id: client.trace-1\r\n") != wire.npos);
    Q4T_CHECK(wire.find("duplicate") == wire.npos);
    Q4T_CHECK(wire.find("\"param\":\"model\"") != wire.npos);
    Q4T_CHECK(wire.find("HTTP/1.1", 1) == wire.npos);
    if (stream) {
      Q4T_CHECK(wire.starts_with("HTTP/1.1 200 "));
      Q4T_CHECK(wire.ends_with("data: [DONE]\r\n\r\n"));
      Q4T_CHECK(wire.find("[DONE]", wire.find("[DONE]") + 1) == wire.npos);
    } else {
      Q4T_CHECK(wire.starts_with("HTTP/1.1 400 "));
    }
  }
  return true;
}

Q4T_TEST(server_sse_heartbeat_serialization_and_nullable_fields) {
  using namespace q4t::server::detail;
  using namespace std::chrono_literals;
  SocketPair sockets;
  HttpResponseContext context(sockets.fd[0]);
  Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], context.State()));
  auto control = std::make_shared<q4t::server::RequestControl>(
      "id", "", std::chrono::steady_clock::now() + 2s);
  context.StartHeartbeats(control, 10ms);
  std::this_thread::sleep_for(35ms);
  for (int i = 0; i < 8; ++i) {
    Q4T_CHECK(WriteAll(sockets.fd[0],
                       SseChunk("id", "model", "", "中文", "", 0, 123, true)));
    std::this_thread::sleep_for(2ms);
  }
  context.StopHeartbeats();
  SendStreamError(sockets.fd[0], context.State(), 409, "cancelled", {},
                  "explicit_cancel");
  shutdown(sockets.fd[0], SHUT_WR);
  std::string wire;
  char buf[8192];
  ssize_t n;
  while ((n = recv(sockets.fd[1], buf, sizeof(buf), 0)) > 0)
    wire.append(buf, n);
  Q4T_CHECK(wire.find(": keep-alive\r\n\r\n") != wire.npos);
  Q4T_CHECK(wire.ends_with("data: [DONE]\r\n\r\n"));
  size_t position = wire.find("\r\n\r\n") + 4;
  int choices = 0;
  while (position < wire.size()) {
    const auto end = wire.find("\r\n\r\n", position);
    Q4T_CHECK(end != wire.npos);
    const auto event = wire.substr(position, end - position);
    position = end + 4;
    if (event.starts_with(":")) continue;
    Q4T_CHECK(event.starts_with("data: "));
    if (event == "data: [DONE]") continue;
    q4t::io::Json value;
    Q4T_CHECK(q4t::io::ParseJson(event.substr(6), &value, true));
    if (const auto* data = value.Find("choices")) {
      Q4T_CHECK(value.Find("usage") && value.Find("usage")->IsNull());
      Q4T_CHECK(data->array[0].Find("logprobs")->IsNull());
      Q4T_CHECK(value.GetInt("created") == 123);
      ++choices;
    }
  }
  Q4T_CHECK(choices == 8);
  return true;
}

Q4T_TEST(server_context_backpressure_has_absolute_deadline) {
  using namespace q4t::server::detail;
  using namespace std::chrono_literals;
  SocketPair sockets;
  int small = 1024;
  setsockopt(sockets.fd[0], SOL_SOCKET, SO_SNDBUF, &small, sizeof(small));
  HttpResponseContext context(sockets.fd[0]);
  Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], context.State()));
  auto control = std::make_shared<q4t::server::RequestControl>(
      "id", "", std::chrono::steady_clock::now() + 2s);
  context.StartHeartbeats(control, 10ms);
  const auto begin = std::chrono::steady_clock::now();
  Q4T_CHECK(!WriteAll(sockets.fd[0], std::string(1024 * 1024, 'x'),
                      {begin + 80ms, control.get()}));
  context.StopHeartbeats();
  Q4T_CHECK(std::chrono::steady_clock::now() - begin < 1s);
  Q4T_CHECK(!WriteAll(sockets.fd[0], "no second frame"));
  return true;
}

Q4T_TEST(server_utf8_stream_boundaries) {
  const std::vector<std::string> cases = {
      "", "plain\n\"text\\", "中文🚀🎉é", std::string("x\0y", 3),
      "\xc2\x80\xdf\xbf\xe0\xa0\x80\xed\x9f\xbf",
      "\xee\x80\x80\xef\xbf\xbf\xf0\x90\x80\x80\xf4\x8f\xbf\xbf"};
  for (const auto& input : cases) {
    Q4T_CHECK(NormalizeUtf8(input) == input);
    for (size_t split = 0; split <= input.size(); ++split) {
      Utf8Stream stream;
      std::string output = stream.Append(input.substr(0, split));
      output += stream.Append(input.substr(split));
      output += stream.Finish();
      Q4T_CHECK(output == input);
      Q4T_CHECK(stream.Finish().empty());
    }
    Utf8Stream stream;
    std::string output;
    for (char c : input) {
      const std::string piece = stream.Append(std::string(1, c));
      const auto event = q4t::server::detail::SseChunk(
          "test", "model", "", piece, "", 0);
      q4t::io::Json parsed;
      Q4T_CHECK(q4t::io::ParseJson(
          event.substr(6, event.size() - 10), &parsed, true).ok());
      // Every emitted piece is independently complete UTF-8.
      Q4T_CHECK(NormalizeUtf8(piece) == piece);
      output += piece;
    }
    output += stream.Finish();
    Q4T_CHECK(output == input);
  }
  return true;
}

Q4T_TEST(server_utf8_malformed_subparts) {
  const std::string r = "\xef\xbf\xbd";
  const std::vector<std::pair<std::string, std::string>> cases = {
      {"\x80", r}, {"\xc0\xaf", r + r}, {"\xff", r},
      {"\xe0\x80\x80", r + r + r},
      {"\xed\xa0\x80", r + r + r},
      {"\xf4\x90\x80\x80", r + r + r + r},
      {"\xf5\x80\x80\x80", r + r + r + r},
      {"\xc2", r}, {"\xe2\x82", r}, {"\xf0\x9f\x9a", r},
      {"\xe2\x82!", r + "!"}, {"\xf0\x9f!", r + "!"},
      {"\xe2\xc2\xa2", r + "\xc2\xa2"}};
  for (const auto& [input, expected] : cases) {
    Q4T_CHECK(NormalizeUtf8(input) == expected);
    for (size_t split = 0; split <= input.size(); ++split) {
      Utf8Stream stream;
      std::string output = stream.Append(input.substr(0, split));
      output += stream.Append(input.substr(split));
      output += stream.Finish();
      Q4T_CHECK(output == expected);
    }
  }
  return true;
}

Q4T_TEST(server_http_parse_error_terminal) {
  using namespace q4t::server::detail;
  for (bool stream : {false, true}) {
    SocketPair sockets;
    ResponseState state = stream ? ResponseState::kStreaming :
                                   ResponseState::kUnsent;
    SendResponseParseError(sockets.fd[0], stream, &state, "invalid \"参数\"");
    Q4T_CHECK(state == ResponseState::kTerminal);
    SendResponseParseError(sockets.fd[0], stream, &state, "duplicate");
    SendExceptionResponse(sockets.fd[0], state);  // Must not append another end.
    shutdown(sockets.fd[0], SHUT_WR);
    std::string output;
    std::array<char, 2048> bytes{};
    for (;;) {
      const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), 0);
      Q4T_CHECK(n >= 0);
      if (!n) break;
      output.append(bytes.data(), static_cast<size_t>(n));
    }
    Q4T_CHECK(output.find("finish_reason") == output.npos);
    Q4T_CHECK(output.find("\"usage\"") == output.npos);
    std::string body;
    if (stream) {
      Q4T_CHECK(output.starts_with("data: "));
      Q4T_CHECK(output.ends_with("data: [DONE]\r\n\r\n"));
      const auto done = output.find("[DONE]");
      Q4T_CHECK(output.find("[DONE]", done + 1) == output.npos);
      body = output.substr(6, output.find("\r\n\r\n") - 6);
    } else {
      Q4T_CHECK(output.starts_with("HTTP/1.1 502 Bad Gateway\r\n"));
      body = output.substr(output.find("\r\n\r\n") + 4);
    }
    q4t::io::Json value;
    Q4T_CHECK(q4t::io::ParseJson(body, &value).ok());
    Q4T_CHECK(value.Find("error")->GetString("code") == "response_parse_failed");
    Q4T_CHECK(value.Find("error")->GetString("message") == "invalid \"参数\"");
  }
  return true;
}

Q4T_TEST(server_http_response_send_status) {
  SocketPair sockets;
  Q4T_CHECK(sockets.fd[0] >= 0);
  Q4T_CHECK(q4t::server::detail::SendSimple(
      sockets.fd[0], 200, "OK", "hello", "text/plain"));
  std::array<char, 1024> data{};
  const auto n = recv(sockets.fd[1], data.data(), data.size(), 0);
  Q4T_CHECK(n > 0);
  const std::string response(data.data(), static_cast<size_t>(n));
  Q4T_CHECK(response.starts_with("HTTP/1.1 200 OK\r\n"));
  Q4T_CHECK(response.find("Content-Length: 5\r\n") != std::string::npos);
  Q4T_CHECK(response.ends_with("\r\n\r\nhello"));
  shutdown(sockets.fd[1], SHUT_RDWR);
  Q4T_CHECK(!q4t::server::detail::SendSimple(
      sockets.fd[0], 200, "OK", "unsent", "text/plain"));
  return true;
}

Q4T_TEST(server_http_error_classification) {
  using namespace q4t::server::detail;
  struct Case { int status; const char* reason; const char* type; };
  for (const auto& item : std::vector<Case>{
           {400, "Bad Request", "invalid_request_error"},
           {404, "Not Found", "invalid_request_error"},
           {408, "Request Timeout", "timeout_error"},
           {409, "Conflict", "conflict_error"},
           {500, "Internal Server Error", "server_error"},
           {502, "Bad Gateway", "server_error"},
           {503, "Service Unavailable", "service_unavailable_error"}}) {
    SocketPair sockets;
    SendError(sockets.fd[0], item.status, "bad \"参数\"", {}, "test_code", "n");
    shutdown(sockets.fd[0], SHUT_WR);
    std::array<char, 2048> bytes{};
    const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), MSG_WAITALL);
    Q4T_CHECK(n > 0);
    const std::string output(bytes.data(), static_cast<size_t>(n));
    Q4T_CHECK(output.starts_with("HTTP/1.1 " + std::to_string(item.status) +
                                 " " + item.reason + "\r\n"));
    q4t::io::Json value;
    Q4T_CHECK(q4t::io::ParseJson(
        output.substr(output.find("\r\n\r\n") + 4), &value).ok());
    const auto* error = value.Find("error");
    Q4T_CHECK(error && error->GetString("type") == item.type);
    Q4T_CHECK(error->GetString("message") == "bad \"参数\"");
    Q4T_CHECK(error->GetString("code") == "test_code");
    Q4T_CHECK(error->GetString("param") == "n");
  }
  return true;
}

Q4T_TEST(server_http_stream_error_is_single_terminal) {
  using namespace q4t::server::detail;
  SocketPair sockets;
  auto state = ResponseState::kStreaming;
  SendStreamError(sockets.fd[0], &state, 409, "cancelled", {},
                  "deadline_exceeded");
  SendStreamError(sockets.fd[0], &state, 500, "duplicate", {}, "internal_error");
  shutdown(sockets.fd[0], SHUT_WR);
  std::array<char, 2048> bytes{};
  const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), MSG_WAITALL);
  Q4T_CHECK(n > 0);
  const std::string output(bytes.data(), static_cast<size_t>(n));
  Q4T_CHECK(output == "data: " + ErrorJson(409, "cancelled",
              "deadline_exceeded") + "\r\n\r\ndata: [DONE]\r\n\r\n");
  Q4T_CHECK(state == ResponseState::kTerminal);
  return true;
}

Q4T_TEST(server_http_stream_creation_identity) {
  using namespace q4t::server::detail;
  for (const auto& event : {
      SseChunk("id", "model", "assistant", "", "", 0, 1700000000),
      SseDeltaChunk("id", "model", "{}", "stop", 0, 1700000000)}) {
    q4t::io::Json value;
    Q4T_CHECK(q4t::io::ParseJson(event.substr(6, event.size() - 10), &value).ok());
    Q4T_CHECK(value.GetInt("created") == 1700000000);
  }
  return true;
}

Q4T_TEST(server_http_exception_terminal_states) {
  using q4t::server::detail::ResponseState;
  for (const auto state : {ResponseState::kUnsent, ResponseState::kHeadersStarted,
                           ResponseState::kStreaming,
                           ResponseState::kTerminal,
                           ResponseState::kComplete}) {
    SocketPair sockets;
    q4t::server::detail::SendExceptionResponse(sockets.fd[0], state);
    shutdown(sockets.fd[0], SHUT_WR);
    std::array<char, 2048> bytes{};
    const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), 0);
    Q4T_CHECK(n >= 0);
    const std::string output(bytes.data(), static_cast<size_t>(n));
    if (state == ResponseState::kHeadersStarted ||
        state == ResponseState::kTerminal || state == ResponseState::kComplete) {
      Q4T_CHECK(output.empty());
      continue;
    }
    std::string json;
    if (state == ResponseState::kUnsent) {
      Q4T_CHECK(output.starts_with("HTTP/1.1 500 "));
      json = output.substr(output.find("\r\n\r\n") + 4);
    } else {
      Q4T_CHECK(output.starts_with("data: "));
      Q4T_CHECK(output.ends_with("data: [DONE]\r\n\r\n"));
      json = output.substr(6, output.find("\r\n\r\n") - 6);
    }
    q4t::io::Json parsed;
    Q4T_CHECK(q4t::io::ParseJson(json, &parsed, true).ok());
    Q4T_CHECK(parsed.Has("error"));
  }
  return true;
}

Q4T_TEST(server_http_host_memory_identity_and_terminal) {
  using namespace q4t::server::detail;
  for (bool stream : {false, true}) {
    SocketPair sockets;
    HttpResponseContext context(sockets.fd[0]);
    context.SetTraceId("memory-contract");
    if (stream)
      Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], context.State()));
    SendExceptionResponse(sockets.fd[0], *context.State(),
                          ExceptionKind::kHostMemory);
    SendExceptionResponse(sockets.fd[0], *context.State(),
                          ExceptionKind::kHostMemory);
    shutdown(sockets.fd[0], SHUT_WR);
    std::array<char, 2048> bytes{};
    const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), 0);
    Q4T_CHECK(n > 0);
    const std::string wire(bytes.data(), static_cast<size_t>(n));
    Q4T_CHECK(wire.starts_with(stream ? "HTTP/1.1 200 " : "HTTP/1.1 503 "));
    Q4T_CHECK(wire.find("X-Request-Id: memory-contract\r\n") != wire.npos);
    const auto pos = wire.find("host_memory_exhausted");
    Q4T_CHECK(pos != wire.npos &&
              wire.find("host_memory_exhausted", pos + 1) == wire.npos);
    Q4T_CHECK(wire.find("service_unavailable_error") != wire.npos);
    Q4T_CHECK(!stream || wire.ends_with("data: [DONE]\r\n\r\n"));
    Q4T_CHECK(wire.find("finish_reason") == wire.npos);
  }
  return true;
}

Q4T_TEST(server_http_host_memory_does_not_append_after_partial_or_terminal) {
  using namespace q4t::server::detail;
  for (auto state : {ResponseState::kHeadersStarted, ResponseState::kTerminal,
                     ResponseState::kComplete}) {
    SocketPair sockets;
    HttpResponseContext context(sockets.fd[0]);
    *context.State() = state;
    SendExceptionResponse(sockets.fd[0], state, ExceptionKind::kHostMemory);
    shutdown(sockets.fd[0], SHUT_WR);
    char byte;
    Q4T_CHECK(recv(sockets.fd[1], &byte, 1, 0) == 0);
  }
  return true;
}

Q4T_TEST(server_http_header_completion) {
  using namespace q4t::server::detail;
  SocketPair sockets;
  ResponseState state = ResponseState::kUnsent;
  Q4T_CHECK(!BeginStreamingResponse(sockets.fd[0], &state,
      {std::chrono::steady_clock::now() - std::chrono::seconds(1)}));
  Q4T_CHECK(state == ResponseState::kUnsent);
  Q4T_CHECK(BeginStreamingResponse(sockets.fd[0], &state));
  Q4T_CHECK(state == ResponseState::kStreaming);
  SendExceptionResponse(sockets.fd[0], state);
  shutdown(sockets.fd[0], SHUT_WR);
  std::array<char, 1024> data{};
  const auto n = recv(sockets.fd[1], data.data(), data.size(), 0);
  Q4T_CHECK(n > 0);
  const std::string output(data.data(), static_cast<size_t>(n));
  Q4T_CHECK(output.starts_with("HTTP/1.1 200 OK\r\n"));
  Q4T_CHECK(output.find("\r\n\r\ndata: {\"error\":") != std::string::npos);
  Q4T_CHECK(output.ends_with("data: [DONE]\r\n\r\n"));
  return true;
}

Q4T_TEST(server_http_exception_cannot_append_after_terminal) {
  using q4t::server::detail::ResponseState;
  const auto finish = q4t::server::detail::SseChunk(
      "id", "model", "", "", "stop", 0);
  for (const auto state : {ResponseState::kStreaming, ResponseState::kTerminal}) {
    SocketPair sockets;
    Q4T_CHECK(q4t::server::detail::WriteAll(sockets.fd[0], finish));
    q4t::server::detail::SendExceptionResponse(sockets.fd[0], state);
    shutdown(sockets.fd[0], SHUT_WR);
    std::string received;
    std::array<char, 1024> buffer;
    for (;;) {
      const auto n = recv(sockets.fd[1], buffer.data(), buffer.size(), 0);
      if (n <= 0) break;
      received.append(buffer.data(), static_cast<size_t>(n));
    }
    if (state == ResponseState::kStreaming) {
      // The previous state assignment produces a contradictory error after
      // an already published normal terminal: preserve this counterexample.
      Q4T_CHECK(received.find("\"error\"") != std::string::npos);
    } else {
      Q4T_CHECK(received == finish);
      Q4T_CHECK(received.find("[DONE]") == std::string::npos);
    }
  }
  return true;
}

Q4T_TEST(server_http_response_partial_writes) {
  SocketPair sockets;
  Q4T_CHECK(sockets.fd[0] >= 0);
  int buffer = 1024;
  setsockopt(sockets.fd[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
  const std::string input(128 * 1024, 'x');
  std::string received;
  std::jthread reader([&] {
    std::array<char, 137> bytes;
    while (received.size() < input.size()) {
      const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), 0);
      if (n <= 0) break;
      received.append(bytes.data(), static_cast<size_t>(n));
    }
  });
  const bool sent = q4t::server::detail::WriteAll(sockets.fd[0], input);
  shutdown(sockets.fd[0], SHUT_WR);
  reader.join();
  Q4T_CHECK(sent && received == input);
  return true;
}

Q4T_TEST(server_http_write_deadline_despite_progress_and_interrupts) {
  using namespace std::chrono_literals;
  using Clock = std::chrono::steady_clock;
  SocketPair sockets;
  Q4T_CHECK(sockets.fd[0] >= 0);
  int buffer = 1024;
  setsockopt(sockets.fd[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
  struct sigaction previous{}, action{};
  action.sa_handler = +[](int) {};
  sigemptyset(&action.sa_mask);
  Q4T_CHECK(sigaction(SIGUSR2, &action, &previous) == 0);
  const auto writer = pthread_self();
  std::atomic<size_t> received{0};
  std::jthread reader([&](std::stop_token stop) {
    std::array<char, 1024> bytes;
    while (!stop.stop_requested()) {
      const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(),
                           MSG_DONTWAIT);
      if (n > 0) received.fetch_add(static_cast<size_t>(n));
      pthread_kill(writer, SIGUSR2);  // Interrupt blocked poll; no SA_RESTART.
      std::this_thread::sleep_for(5ms);
    }
  });
  const std::string input(1024 * 1024, 'x');
  const auto start = Clock::now();
  const q4t::server::detail::WriteOptions options{start + 100ms};
  const bool sent = q4t::server::detail::WriteAll(
      sockets.fd[0], input, options);
  const auto elapsed = Clock::now() - start;
  reader.request_stop();
  reader.join();
  sigaction(SIGUSR2, &previous, nullptr);
  Q4T_CHECK(!sent);
  Q4T_CHECK(received.load() > 0 && received.load() < input.size());
  Q4T_CHECK(elapsed >= 100ms && elapsed < 2s);
  // Reusing the same terminal budget cannot buy a second wait.
  Q4T_CHECK(!q4t::server::detail::WriteAll(sockets.fd[0], "end", options));
  return true;
}

Q4T_TEST(server_http_write_cancel_and_request_deadline) {
  using namespace std::chrono_literals;
  using Clock = std::chrono::steady_clock;
  SocketPair sockets;
  Q4T_CHECK(sockets.fd[0] >= 0);
  int buffer = 1024;
  setsockopt(sockets.fd[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
  q4t::server::RequestControl control("writer", "key", Clock::now() + 10s);
  std::jthread cancel([&] {
    std::this_thread::sleep_for(30ms);
    control.Cancel();
  });
  const auto start = Clock::now();
  Q4T_CHECK(!q4t::server::detail::WriteAll(
      sockets.fd[0], std::string(1024 * 1024, 'x'), {.control = &control}));
  Q4T_CHECK(Clock::now() - start < 2s);
  Q4T_CHECK(!control.Finish());
  cancel.join();
  q4t::server::RequestControl expired("deadline", "key", Clock::now() - 1ms);
  Q4T_CHECK(!q4t::server::detail::WriteAll(
      sockets.fd[0], "unsent", {.control = &expired}));
  Q4T_CHECK(std::string(expired.Reason()) == "deadline_exceeded");
  return true;
}
