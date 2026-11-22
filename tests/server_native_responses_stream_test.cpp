#include "q4t/server/native_responses_stream.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <stdexcept>

#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_output.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
using namespace q4t::server::detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json value;
  const auto status =
      q4t::io::ParseJson(text, &value, true, kApplicationJsonLimits);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
struct Fixture {
  ResponsesOutput output;
  std::string begin, delta, end;
  explicit Fixture(bool failed = false, bool incomplete = false) {
    const auto options = Parse(R"({"model":"qwen3.8-flash-next",
      "instructions":"Private backend instructions", "tools":[
        {"type":"function","name":"project_lookup","parameters":
          {"type":"object","properties":{"key":{"type":"string"}}}}],
      "store":false,"parallel_tool_calls":true,"tool_choice":"auto"})");
    if (!output.Init(options, "resp_native_fixture", 42, false) ||
        !output.Start(&begin))
      throw std::runtime_error("Responses fixture initialization");
    ResponseDelta text;
    text.kind = ResponseDelta::Kind::kContent;
    text.text = "真实后端输出42";
    if (!output.Append(text, "null", &delta))
      throw std::runtime_error("Responses fixture delta");
    ResponseDelta tool;
    tool.kind = ResponseDelta::Kind::kTool;
    tool.tool_start = true;
    tool.call = {0, "call_lookup", "project_lookup", "{\"key\":\"version\"}"};
    std::string tool_wire;
    if (!output.Append(tool, "null", &tool_wire))
      throw std::runtime_error("Responses fixture tool");
    delta += tool_wire;
    const auto status =
        failed ? output.Fail("model_failed", "actual failure", &end)
               : output.Finish(incomplete, 1, {12, 0, 7, 0, 0}, 43, &end);
    if (!status) throw std::runtime_error(status.message());
  }
};
struct Pair {
  int fd[2] = {-1, -1};
  Pair() {
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, fd))
      throw std::runtime_error("socketpair");
  }
  ~Pair() {
    for (const auto value : fd)
      if (value >= 0) close(value);
  }
  bool Empty() const {
    char byte;
    return recv(fd[1], &byte, 1, MSG_DONTWAIT) == -1 &&
           (errno == EAGAIN || errno == EWOULDBLOCK);
  }
};
void Terminal(void* pointer) noexcept { ++*static_cast<int*>(pointer); }
ObjectReply Ok(const Json&) { return {}; }
}  // namespace

Q4T_TEST(native_responses_actual_output_uses_no_socket_and_waits_for_seal) {
  Fixture fixture;
  std::vector<Json> events;
  NativeResponsesStream capture([&](const Json& event) {
    events.push_back(event);
    return ObjectReply{};
  });
  Pair pair;
  HttpResponseContext http(
      HttpResponseContext::Detached{pair.fd[0], nullptr, &capture});
  int terminals = 0;
  http.SetBeforeTerminal(Terminal, &terminals);
  Q4T_CHECK(BeginStreamingResponse(pair.fd[0], http.State()));
  Q4T_CHECK(WriteAll(pair.fd[0], fixture.begin));
  Q4T_CHECK(WriteAll(pair.fd[0], fixture.delta));
  Q4T_CHECK(!capture.Complete() && !capture.Failed());
  http.BeforeTerminal();
  *http.State() = ResponseState::kTerminal;
  Q4T_CHECK(WriteAll(pair.fd[0], fixture.end));
  Q4T_CHECK(!capture.Complete() && !capture.Failed());
  capture.Seal();
  Q4T_CHECK(capture.Complete() && capture.Error().ok() &&
            capture.Id() == "resp_native_fixture" && terminals == 1 &&
            pair.Empty() && capture.Events() == int64_t(events.size()));
  Q4T_CHECK(OutputJson(capture.Response()) == fixture.output.Json());
  Q4T_CHECK(capture.Response().Find("usage")->GetInt("total_tokens") == 19);
  const auto* items = capture.Response().GetArray("output");
  Q4T_CHECK(items && items->array.size() == 2 &&
            items->array[1].GetString("type") == "function_call" &&
            items->array[1].GetString("call_id") == "call_lookup" &&
            items->array[1].GetString("arguments") == "{\"key\":\"version\"}");
  Q4T_CHECK(events.front().GetString("type") == "response.created" &&
            events.back().GetString("type") == "response.completed");
  http.BeforeTerminal();
  Q4T_CHECK(terminals == 1);
  return true;
}

Q4T_TEST(native_responses_failed_incomplete_and_exception_keep_real_outcomes) {
  for (const bool failed : {false, true}) {
    Fixture fixture(failed, !failed);
    NativeResponsesStream capture(Ok);
    Q4T_CHECK(capture.Begin() && capture.Append(fixture.begin) &&
              capture.Append(fixture.delta) && capture.Append(fixture.end));
    capture.Seal();
    Q4T_CHECK(capture.Complete() &&
              capture.Response().GetString("status") ==
                  (failed ? "failed" : "incomplete") &&
              capture.Error().ok() == !failed);
    if (failed) Q4T_CHECK(capture.Error().code == "model_failed");
  }
  Fixture fixture;
  std::vector<Json> events;
  NativeResponsesStream capture([&](const auto& event) {
    events.push_back(event);
    return ObjectReply{};
  });
  HttpResponseContext http(
      HttpResponseContext::Detached{-1, nullptr, &capture});
  int64_t sequence = 0;
  http.SetResponsesStream(&ResponsesOutput::RenderStreamError, &fixture.output,
                          &sequence);
  Q4T_CHECK(BeginStreamingResponse(-1, http.State()) &&
            WriteAll(-1, fixture.begin) && WriteAll(-1, fixture.delta));
  sequence = capture.Events();
  SendExceptionResponse(-1, *http.State(), ExceptionKind::kHostMemory);
  capture.Seal();
  Q4T_CHECK(capture.Complete() && capture.Response().IsNull() &&
            capture.Error().code == "host_memory_exhausted" &&
            events.back().GetString("type") == "error" &&
            events.back().GetInt("sequence_number") == capture.Events() - 1);
  SendExceptionResponse(-1, *http.State());
  Q4T_CHECK(capture.Complete());
  return true;
}

Q4T_TEST(native_responses_finite_errors_keep_status_without_fake_events) {
  for (const bool exception : {false, true}) {
    int events = 0;
    NativeResponsesStream capture([&](const auto&) {
      ++events;
      return ObjectReply{};
    });
    HttpResponseContext http(
        HttpResponseContext::Detached{-1, nullptr, &capture});
    if (exception)
      SendExceptionResponse(-1, *http.State());
    else
      SendError(-1, 429, "actual quota", {}, "rate_limit_exceeded", "tokens");
    capture.Seal();
    Q4T_CHECK(capture.Complete() && !capture.Error().ok() && events == 0 &&
              capture.Id().empty() &&
              capture.Error().http_status == (exception ? 500 : 429));
    if (!exception)
      Q4T_CHECK(capture.Error().value.Find("error")->GetString("param") ==
                "tokens");
  }
  for (const auto* body : {"{}", "[]", "{\"error\":{\"code\":\"x\"}}",
                           "{\"error\":{},\"error\":{}}"}) {
    NativeResponsesStream capture(Ok);
    Q4T_CHECK(!capture.RecordError(500, "application/json", body));
    capture.Seal();
    Q4T_CHECK(!capture.Complete() && capture.Failed());
  }
  NativeResponsesStream success(Ok);
  Q4T_CHECK(!success.RecordError(200, "application/json", "{}"));
  return true;
}

Q4T_TEST(native_responses_rejects_sequence_replay_malformed_and_foreign_owner) {
  Fixture fixture;
  std::vector<std::string> invalid = {
      "data: [DONE]\r\n\r\n",
      fixture.begin,
      fixture.delta.substr(0, 17),
      "HTTP/1.1 200 OK\r\n\r\n",
      "event: error\r\ndata: {}\r\n\r\n",
      "event: error\r\ndata: {\"type\":\"error\",\"sequence_number\":2,"
      "\"sequence_number\":2,\"code\":\"x\",\"message\":\"x\"}\r\n\r\n"};
  auto foreign = fixture.end;
  auto at = foreign.find("resp_native_fixture");
  Q4T_CHECK(at != std::string::npos);
  foreign.replace(at, std::string("resp_native_fixture").size(), "resp_other");
  invalid.push_back(foreign);
  for (const auto& wire : invalid) {
    NativeResponsesStream capture(Ok);
    Q4T_CHECK(capture.Begin() && capture.Append(fixture.begin));
    // Finish's item events have later sequences, so supply the missing deltas
    // before checking the foreign lifecycle identity independently.
    if (wire == foreign) Q4T_CHECK(capture.Append(fixture.delta));
    Q4T_CHECK(!capture.Append(wire) && capture.Failed());
    Q4T_CHECK(!capture.Append(fixture.end));
    capture.Seal();
    Q4T_CHECK(!capture.Complete());
  }
  return true;
}

Q4T_TEST(native_responses_lost_sink_receipt_and_throw_never_replay) {
  Fixture fixture;
  for (const int mode : {0, 1, 2}) {
    size_t calls = 0;
    NativeResponsesStream capture([&](const auto&) -> ObjectReply {
      if (++calls < 2) return {};
      if (mode == 2) throw std::runtime_error("sink failure");
      ObjectReply reply;
      reply.http_status = 503;
      reply.code = "sink_full";
      reply.message = "bounded CPU sink full";
      reply.commit_unknown = mode == 1;
      return reply;
    });
    Q4T_CHECK(capture.Begin());
    bool accepted = false;
    try {
      accepted = capture.Append(fixture.begin);
      Q4T_CHECK(mode != 2);
    } catch (const std::runtime_error&) {
      Q4T_CHECK(mode == 2);
    }
    Q4T_CHECK(!accepted && capture.Failed() && calls == 2);
    Q4T_CHECK(!capture.Append(fixture.begin) && calls == 2);
    if (mode == 1) Q4T_CHECK(capture.Error().commit_unknown);
    capture.Seal();
    Q4T_CHECK(!capture.Complete());
  }
  return true;
}

Q4T_TEST(native_responses_exact_byte_bounds_and_owned_terminal) {
  Fixture fixture;
  const auto wire = fixture.begin + fixture.delta + fixture.end;
  NativeResponsesStream exact(Ok, wire.size(), wire.size());
  Q4T_CHECK(exact.Begin() && exact.Append(wire));
  exact.Seal();
  Q4T_CHECK(exact.Complete() && exact.Bytes() == wire.size());
  NativeResponsesStream small(Ok, wire.size() - 1, wire.size());
  Q4T_CHECK(small.Begin() && !small.Append(wire) && small.Events() == 0);
  NativeResponsesStream frame(Ok, wire.size(), 10);
  Q4T_CHECK(frame.Begin() && !frame.Append(wire) && frame.Events() == 0);
  for (const size_t bound : {size_t{0}, kChatBodyBytes + 1}) {
    bool caught = false;
    try {
      NativeResponsesStream bad(Ok, bound);
    } catch (const std::invalid_argument&) {
      caught = true;
    }
    Q4T_CHECK(caught);
  }
  NativeResponsesStream tail(Ok);
  Q4T_CHECK(tail.Begin() && !tail.Append(wire + fixture.begin));
  tail.Seal();
  Q4T_CHECK(tail.Failed() && !tail.Complete());
  return true;
}

Q4T_TEST(native_responses_cancel_deadline_and_unfinished_never_complete) {
  Fixture fixture;
  for (const bool after_start : {false, true}) {
    NativeResponsesStream capture(Ok);
    HttpResponseContext http(
        HttpResponseContext::Detached{-1, nullptr, &capture});
    auto control =
        std::make_shared<RequestControl>("req_capture", "", Clock::now() + 1s);
    if (after_start)
      Q4T_CHECK(BeginStreamingResponse(-1, http.State()) &&
                WriteAll(-1, fixture.begin));
    control->Cancel(RequestControl::State::kExplicit);
    const WriteOptions options{Clock::now() + 1s, control.get()};
    Q4T_CHECK(!(after_start
                    ? WriteAll(-1, fixture.delta, options)
                    : BeginStreamingResponse(-1, http.State(), options)));
    capture.Seal();
    Q4T_CHECK(capture.Failed() && !capture.Complete());
  }
  NativeResponsesStream capture(Ok);
  HttpResponseContext http(
      HttpResponseContext::Detached{-1, nullptr, &capture});
  Q4T_CHECK(
      !BeginStreamingResponse(-1, http.State(), {Clock::now() - 1s, nullptr}));
  capture.Seal();
  Q4T_CHECK(!capture.Complete());
  NativeResponsesStream incomplete(Ok);
  Q4T_CHECK(incomplete.Begin() && incomplete.Append(fixture.begin));
  incomplete.Seal();
  Q4T_CHECK(incomplete.Failed() && !incomplete.Complete());
  return true;
}

Q4T_TEST(native_responses_raw_binary_and_duplicate_owners_are_rejected) {
  for (const int mode : {0, 1, 2}) {
    NativeResponsesStream capture(Ok);
    HttpResponseContext http(
        HttpResponseContext::Detached{-1, nullptr, &capture});
    const bool accepted =
        mode == 0   ? WriteAll(-1, "HTTP/1.1 200 OK\r\n\r\n")
        : mode == 1 ? BeginBinaryStreamResponse(-1, "audio/pcm")
                    : SendSimple(-1, 200, "OK", "{}", "application/json");
    Q4T_CHECK(!accepted);
    capture.Seal();
    Q4T_CHECK(!capture.Complete());
  }
  NativeResponsesStream capture(Ok);
  FiniteResponse finite;
  HttpResponseContext http(
      HttpResponseContext::Detached{-1, &finite, &capture});
  Q4T_CHECK(!BeginStreamingResponse(-1, http.State()) &&
            !SendSimple(-1, 200, "OK", "{}", "application/json"));
  capture.Seal();
  Q4T_CHECK(capture.Failed() && finite.Failed() && !capture.Complete());
  return true;
}
