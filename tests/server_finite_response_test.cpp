#include "q4t/server/finite_response.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <stdexcept>

#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Pair {
  int fds[2] = {-1, -1};
  Pair() {
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, fds))
      throw std::runtime_error("socketpair");
  }
  ~Pair() {
    for (const auto fd : fds)
      if (fd >= 0) close(fd);
  }
};
bool NoWire(const Pair& pair) {
  char byte;
  const auto read = recv(pair.fds[1], &byte, 1, MSG_DONTWAIT);
  return read == -1 && (errno == EAGAIN || errno == EWOULDBLOCK);
}
void Terminal(void* value) noexcept { ++*static_cast<int*>(value); }
q4t::io::Json Parse(const std::string& wire) {
  q4t::io::Json value;
  if (!q4t::io::ParseJson(wire, &value, true, kApplicationJsonLimits))
    throw std::runtime_error("test response JSON");
  return value;
}
}  // namespace

Q4T_TEST(
    finite_response_preserves_complete_endpoint_json_exact_numbers_and_usage) {
  FiniteResponse capture;
  const std::string body =
      R"({"id":"resp_fixture","object":"response","output":[{"type":"function_call","call_id":"call_1","name":"lookup","arguments":"{\"id\":42}"}],"usage":{"input_tokens":19,"output_tokens":7,"total_tokens":26},"v":1e-999})";
  Q4T_CHECK(capture.Record(200, "application/json", body) &&
            capture.Complete() && capture.Status() == 200 &&
            capture.Body() == body && !capture.Failed());
  Q4T_CHECK(!capture.Record(200, "application/json", "{}") &&
            capture.Failed() && !capture.Complete());
  return true;
}

Q4T_TEST(
    finite_response_rejects_invalid_json_type_status_and_duplicate_fields) {
  for (const auto* body : {"[]", "{\"a\":1,\"a\":2}", "{\"x\":NaN}", "{} {}",
                           "{\"text\":\"\xff\"}"}) {
    FiniteResponse capture;
    Q4T_CHECK(!capture.Record(200, "application/json", body) &&
              capture.Failed() && capture.Body().empty());
    Q4T_CHECK(!capture.Record(200, "application/json", "{}"));
  }
  for (const int status : {0, 199, 300, 302, 399, 600}) {
    FiniteResponse capture;
    Q4T_CHECK(!capture.Record(status, "application/json", "{}") &&
              !capture.Complete());
  }
  for (const auto* type :
       {"text/event-stream", "application/octet-stream", "text/plain"}) {
    FiniteResponse capture;
    Q4T_CHECK(!capture.Record(200, type, "{}"));
  }
  return true;
}

Q4T_TEST(finite_response_exact_capacity_and_invalid_internal_limits) {
  const std::string body =
      "{\"content\":\"" + std::string(1024 * 1024, 'x') + "\"}";
  FiniteResponse exact(body.size()), short_limit(body.size() - 1);
  Q4T_CHECK(exact.Record(200, "application/json", body) &&
            exact.Body() == body);
  Q4T_CHECK(!short_limit.Record(200, "application/json", body) &&
            short_limit.Body().empty());
  for (const size_t limit : {size_t{0}, kChatBodyBytes + 1}) {
    bool invalid = false;
    try {
      FiniteResponse bad(limit);
    } catch (const std::invalid_argument&) {
      invalid = true;
    }
    Q4T_CHECK(invalid);
  }
  return true;
}

Q4T_TEST(
    finite_response_typed_send_uses_no_socket_and_preserves_terminal_hook) {
  Pair pair;
  FiniteResponse capture;
  HttpResponseContext context(
      HttpResponseContext::Detached{pair.fds[0], &capture});
  int calls = 0;
  context.SetBeforeTerminal(Terminal, &calls);
  const std::string body =
      "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"42\"}}]"
      "}";
  Q4T_CHECK(SendSimple(pair.fds[0], 200, "OK", body, "application/json"));
  Q4T_CHECK(capture.Complete() && capture.Body() == body && calls == 1 &&
            *context.State() == ResponseState::kComplete && NoWire(pair));
  context.BeforeTerminal();
  Q4T_CHECK(calls == 1);
  return true;
}

Q4T_TEST(finite_response_actual_error_retains_status_code_and_param) {
  FiniteResponse capture;
  HttpResponseContext context(HttpResponseContext::Detached{-1, &capture});
  SendError(-1, 429, "CPU fixture quota exhausted", {}, "rate_limit_exceeded",
            "max_tokens");
  Q4T_CHECK(capture.Complete() && capture.Status() == 429);
  const auto json = Parse(capture.Body());
  Q4T_CHECK(json.Find("error")->GetString("code") == "rate_limit_exceeded" &&
            json.Find("error")->GetString("param") == "max_tokens" &&
            json.Find("error")->GetString("message") ==
                "CPU fixture quota exhausted");
  SendExceptionResponse(-1, *context.State());
  Q4T_CHECK(capture.Complete() && capture.Status() == 429);
  return true;
}

Q4T_TEST(
    finite_response_exception_and_host_memory_use_original_error_contract) {
  for (const auto kind :
       {ExceptionKind::kInternal, ExceptionKind::kHostMemory}) {
    FiniteResponse capture;
    HttpResponseContext context(HttpResponseContext::Detached{-1, &capture});
    int calls = 0;
    context.SetBeforeTerminal(Terminal, &calls);
    SendExceptionResponse(-1, ResponseState::kUnsent, kind);
    const auto memory = kind == ExceptionKind::kHostMemory;
    Q4T_CHECK(capture.Complete() && capture.Status() == (memory ? 503 : 500) &&
              calls == 1);
    const auto json = Parse(capture.Body());
    Q4T_CHECK(json.Find("error")->GetString("code") ==
              (memory ? "host_memory_exhausted" : "internal_error"));
    const auto original = capture.Body();
    SendExceptionResponse(-1, *context.State());
    Q4T_CHECK(capture.Complete() && capture.Body() == original);
  }
  return true;
}

Q4T_TEST(finite_response_stream_binary_and_raw_cannot_masquerade_as_json) {
  for (const int mode : {0, 1, 2}) {
    Pair pair;
    FiniteResponse capture;
    HttpResponseContext context(
        HttpResponseContext::Detached{pair.fds[0], &capture});
    bool written = true;
    int producer_calls = 0;
    if (mode == 0)
      written = BeginStreamingResponse(pair.fds[0], context.State());
    if (mode == 1)
      written = SendBinaryResponse(pair.fds[0], 2, [&](std::string* chunk) {
        ++producer_calls;
        *chunk = "{}";
        return true;
      });
    if (mode == 2) written = WriteAll(pair.fds[0], "HTTP/1.1 200 OK\r\n\r\n{}");
    Q4T_CHECK(!written && capture.Failed() && !capture.Complete() &&
              producer_calls == 0 && NoWire(pair));
  }
  return true;
}

Q4T_TEST(
    finite_response_nested_detached_preparation_cannot_capture_parent_result) {
  Pair pair;
  FiniteResponse capture;
  HttpResponseContext parent(
      HttpResponseContext::Detached{pair.fds[0], &capture});
  parent.SetTraceId("parent-cpu-fixture");
  {
    HttpResponseContext child(HttpResponseContext::Detached{pair.fds[0]});
    Q4T_CHECK(!child.CapturesJson() &&
              HttpResponseContext::Current(pair.fds[0]) == &child);
    SendError(pair.fds[0], 400, "private preparation fixture", {},
              "invalid_request");
  }
  Q4T_CHECK(HttpResponseContext::Current(pair.fds[0]) == &parent &&
            *parent.State() == ResponseState::kUnsent && !capture.Complete() &&
            !capture.Failed());
  Q4T_CHECK(SendSimple(pair.fds[0], 200, "OK", "{\"answer\":42}",
                       "application/json") &&
            capture.Complete() &&
            Parse(capture.Body()).GetInt("answer") == 42 && NoWire(pair));
  return true;
}

Q4T_TEST(finite_response_cancel_and_write_deadline_prevent_success_capture) {
  for (const bool cancelled : {true, false}) {
    FiniteResponse capture;
    HttpResponseContext context(HttpResponseContext::Detached{-1, &capture});
    RequestControl control("cpu-request", "", Clock::now() + 10s);
    WriteOptions options;
    if (cancelled) {
      control.Cancel();
      options.control = &control;
    } else
      options.deadline = Clock::now() - 1ms;
    Q4T_CHECK(!SendSimple(-1, 200, "OK", "{}", "application/json", options) &&
              capture.Failed() && !capture.Complete() &&
              capture.Body().empty());
  }
  return true;
}
