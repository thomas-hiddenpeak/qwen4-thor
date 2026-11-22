#include "q4t/server/native_responses_stream.h"
#include "q4t/server/live_responses_turn.h"
#include "q4t/server/live_responses_execution.h"
#include "../src/server/responses_json.h"

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

Q4T_TEST(
    live_responses_turn_wraps_actual_stream_and_continues_once_after_join) {
  Fixture fixture;
  std::vector<Json> events;
  LiveResponsesTurn turn("delegation_real", "start", [&](Json value) {
    events.push_back(std::move(value));
    return ObjectReply{};
  });
  const auto result = Parse(
      R"({"type":"function_call_output","call_id":"call_lookup","output":"rev426"})");
  Q4T_CHECK(!turn.Result(result).ok());
  NativeResponsesStream stream(
      [&](const Json& native) { return turn.Event(native, 150); });
  Q4T_CHECK(stream.Begin() && stream.Append(fixture.begin) &&
            stream.Append(fixture.delta));
  Q4T_CHECK(!turn.Result(result).ok());
  Q4T_CHECK(stream.Append(fixture.end));
  Q4T_CHECK(turn.RequestContinuation("early").code ==
            "function_call_outputs_required");
  Q4T_CHECK(
      !turn
           .Result(Parse(
               R"({"type":"function_call_output","call_id":"another","output":"x"})"))
           .ok());
  Q4T_CHECK(
      !turn
           .Result(Parse(
               R"({"type":"custom_tool_call_output","call_id":"call_lookup","output":"x"})"))
           .ok());
  Q4T_CHECK(turn.Result(result).ok() && !turn.Result(result).ok());
  Q4T_CHECK(turn.RequestContinuation("resume").ok() &&
            !turn.RequestContinuation("duplicate").ok());
  Q4T_CHECK(!turn.TakeContinuation());
  const auto& created = events.at(0);
  Q4T_CHECK(created.GetString("type") == "session.delegation.created" &&
            created.GetInt("offset_ms") == 150 &&
            created.Find("delegation")->GetString("response_id") ==
                "resp_native_fixture" &&
            created.Find("delegation")->GetString("target") == "responses");
  for (size_t i = 1; i < events.size(); ++i) {
    const auto& wrapped = events[i];
    Q4T_CHECK(wrapped.GetString("type") == "response.event" &&
              wrapped.GetString("client_event_id") == "start" &&
              wrapped.GetString("delegation_id") == "delegation_real");
    if (const auto* response = wrapped.Find("event")->Find("response")) {
      Q4T_CHECK(response->Find("instructions")->IsNull() &&
                response->GetArray("tools")->array.empty() &&
                response->GetArray("output")->array.empty() &&
                !response->Has("input"));
    }
  }
  stream.Seal();
  Q4T_CHECK(stream.Complete() && turn.Finish(stream.Response()).ok());
  Q4T_CHECK(turn.TakeContinuation() == std::optional<std::string>("resume") &&
            !turn.TakeContinuation());
  auto history = Parse("[]");
  Q4T_CHECK(turn.AppendHistory(&history).ok() && history.array.size() == 3 &&
            history.array[1].GetString("call_id") == "call_lookup" &&
            history.array[2].GetString("output") == "rev426");
  return true;
}

namespace {
using namespace q4t::server::responses_detail;
Json Created() {
  return Parse(
      R"({"type":"response.created","response":{"id":"resp_cpu","status":"in_progress",
    "object":"response","input":"private","output":[],"tools":[]}})");
}
Json Done(size_t index, Json item) {
  return Object({{"type", String("response.output_item.done")},
                 {"output_index", Number(index)},
                 {"item", std::move(item)}});
}
Json Function(const char* id) {
  return Object({{"type", String("function_call")},
                 {"id", String(std::string("fc_") + id)},
                 {"name", String("lookup")},
                 {"call_id", String(id)},
                 {"arguments", String("{}")},
                 {"status", String("completed")}});
}
Json Answer(const char* id) {
  return Object({{"type", String("function_call_output")},
                 {"call_id", String(id)},
                 {"output", String("value")}});
}
}  // namespace
Q4T_TEST(live_responses_turn_later_calls_invalidate_only_queued_continuation) {
  std::vector<Json> events;
  LiveResponsesTurn turn("delegation_1", "primary", [&](Json value) {
    events.push_back(std::move(value));
    return ObjectReply{};
  });
  Q4T_CHECK(turn.Event(Created(), 1).ok() &&
            !events.back().Find("event")->Find("response")->Has("input"));
  Q4T_CHECK(turn.Event(Done(0, Function("a")), 2).ok() &&
            turn.Result(Answer("a")).ok());
  Q4T_CHECK(turn.RequestContinuation("early").ok());
  Q4T_CHECK(turn.Event(Done(1, Function("b")), 2).ok());
  const auto error = events[events.size() - 2];
  Q4T_CHECK(error.GetString("type") == "error" &&
            error.GetString("client_event_id") == "early" &&
            error.Find("error")->GetString("client_event_id") == "early" &&
            error.Find("error")->GetString("code") ==
                "function_call_outputs_required");
  Q4T_CHECK(turn.Result(Answer("b")).ok());
  auto response = Object({{"id", String("resp_cpu")},
                          {"status", String("completed")},
                          {"output", Array({Function("a"), Function("b")})}});
  Q4T_CHECK(turn.Event(Object({{"type", String("response.completed")},
                               {"response", response}}),
                       2)
                .ok());
  Q4T_CHECK(turn.Finish(response).ok() && !turn.TakeContinuation());
  Q4T_CHECK(turn.RequestContinuation("later").ok() &&
            turn.TakeContinuation() == std::optional<std::string>("later"));
  return true;
}

Q4T_TEST(live_responses_turn_binds_program_parent_and_mcp_approval_separately) {
  LiveResponsesTurn turn("delegation_2", "",
                         [](Json) { return ObjectReply{}; });
  Q4T_CHECK(turn.Event(Created(), 0).ok());
  auto call = Function("a");
  Set(&call, "caller", Parse(R"({"type":"program","caller_id":"prog_a"})"));
  Q4T_CHECK(turn.Event(Done(0, call), 0).ok());
  auto answer = Answer("a");
  Q4T_CHECK(!turn.Result(answer).ok());
  Set(&answer, "caller", Parse(R"({"type":"program","caller_id":"prog_b"})"));
  Q4T_CHECK(!turn.Result(answer).ok());
  Set(&answer, "caller", *call.Find("caller"));
  Q4T_CHECK(turn.Result(answer).ok());
  Q4T_CHECK(
      turn.Event(
              Done(
                  1,
                  Parse(
                      R"({"type":"mcp_approval_request","id":"mcpr_1","name":"read","server_label":"docs","arguments":"{}"})")),
              0)
          .ok());
  Q4T_CHECK(turn.RequestContinuation("early").code == "tool_outputs_required");
  Q4T_CHECK(!turn.Result(Answer("mcpr_1")).ok());
  Q4T_CHECK(
      turn
          .Result(Parse(
              R"({"type":"mcp_approval_response","approval_request_id":"mcpr_1","approve":false})"))
          .ok());
  Q4T_CHECK(!turn.WaitingForResults());
  return true;
}

Q4T_TEST(live_responses_turn_rejects_unconfirmed_terminal_or_delivery) {
  LiveResponsesTurn turn("delegation_3", "",
                         [](Json) { return ObjectReply{}; });
  Q4T_CHECK(turn.Event(Created(), 0).ok() &&
            turn.Event(Done(0, Function("a")), 0).ok());
  Q4T_CHECK(turn.Result(Answer("a")).ok() &&
            turn.RequestContinuation("queued").ok());
  auto response = Object({{"id", String("resp_cpu")},
                          {"status", String("completed")},
                          {"output", Array({Function("b")})}});
  Q4T_CHECK(turn.Event(Object({{"type", String("response.completed")},
                               {"response", response}}),
                       2)
                .ok());
  Q4T_CHECK(!turn.Finish(response).ok() && !turn.TakeContinuation());
  auto original = Parse("[]");
  Q4T_CHECK(!turn.AppendHistory(&original).ok() && original.array.empty());
  size_t count = 0;
  LiveResponsesTurn other("delegation_x", "", [&](Json) {
    ++count;
    ObjectReply uncertain;
    uncertain.commit_unknown = true;
    return uncertain;
  });
  Q4T_CHECK(other.Event(Created(), 0).commit_unknown);
  Q4T_CHECK(!other.Event(Created(), 0).ok() && count == 1);
  return true;
}

Q4T_TEST(live_responses_turn_distinguishes_hosted_tools_and_matching_checks) {
  LiveResponsesTurn turn("delegation_4", "",
                         [](Json) { return ObjectReply{}; });
  Q4T_CHECK(turn.Event(Created(), 0).ok());
  auto search =
      Parse(R"({"type":"tool_search_call","id":"ts_1","call_id":"search",
     "execution":"server","arguments":{},"status":"completed"})");
  Q4T_CHECK(turn.Event(Done(0, search), 0).ok() && !turn.HasCalls());
  Set(&search, "id", String("ts_2"));
  Set(&search, "execution", String("client"));
  Set(&search, "call_id", String("search2"));
  Q4T_CHECK(turn.Event(Done(1, search), 0).ok() && turn.WaitingForResults());
  auto output = Parse(
      R"({"type":"tool_search_output","execution":"server","call_id":"search2","tools":[]})");
  Q4T_CHECK(!turn.Result(output).ok());
  Set(&output, "execution", String("client"));
  Q4T_CHECK(turn.Result(output).ok() && !turn.WaitingForResults());
  auto computer = Parse(R"({"type":"computer_call","id":"cu_1","call_id":"ui",
     "pending_safety_checks":[{"id":"chk_a"},{"id":"chk_b"}],
     "action":{"type":"screenshot"},"status":"completed"})");
  Q4T_CHECK(turn.Event(Done(2, computer), 0).ok());
  auto screen = Parse(R"({"type":"computer_call_output","call_id":"ui",
     "output":{"type":"computer_screenshot","file_id":"file_fixture"}})");
  Q4T_CHECK(!turn.Result(screen).ok());
  Set(&screen, "acknowledged_safety_checks",
      Parse(R"([{"id":"chk_a"},{"id":"chk_a"}])"));
  Q4T_CHECK(!turn.Result(screen).ok());
  Set(&screen, "acknowledged_safety_checks",
      Parse(R"([{"id":"chk_b"},{"id":"chk_a"}])"));
  Q4T_CHECK(turn.Result(screen).ok() && !turn.WaitingForResults());
  return true;
}

namespace {
struct Execution {
  ClientAdmission clients;
  ClientIdentity client = clients.Authenticate("", "");
  ClientLease permit;
  HttpBodyBudget input{1024};
  std::shared_ptr<RequestControl> control = std::make_shared<RequestControl>(
      "live_owned", "", Clock::now() + 5s, client.Id());
  Json settings = Parse(R"({"model":"native","instructions":"Backend only"})");
  Execution() {
    if (!permit.Acquire(clients, client, ClientLane::kData))
      throw std::runtime_error("Live test client capacity");
  }
  auto Run(const std::function<void(const std::string&)>& run,
           NativeResponsesStream::Sink sink = Ok) {
    return ExecuteLiveResponses(
        settings,
        Array({Object(
            {{"role", String("user")}, {"content", String("version?")}})}),
        client, permit, control, input, std::move(sink), run);
  }
};
}  // namespace
Q4T_TEST(live_responses_execution_uses_online_admission_and_real_stream_once) {
  Execution f;
  Fixture fixture;
  size_t calls = 0, events = 0;
  bool owner = false;
  auto result = f.Run(
      [&](const auto& body) {
        ++calls;
        const auto request = Parse(body);
        auto* http = HttpResponseContext::Current(-1);
        owner = http && http->CapturesResponses() &&
                http->RateDomain() == ClientRateDomain::kOnline &&
                http->TraceId() == f.control->id() &&
                f.input.Used() == body.size() && request.GetBool("stream") &&
                !request.GetBool("store", true) &&
                request.GetString("instructions") == "Backend only" &&
                request.GetArray("input")->array.size() == 1;
        http->Usage().Enable();
        http->Usage().BeginStep();
        http->Usage().Observe({12, 7});
        http->Usage().EndStep();
        if (!BeginStreamingResponse(-1, http->State()) ||
            !WriteAll(-1, fixture.begin) || !WriteAll(-1, fixture.delta) ||
            !WriteAll(-1, fixture.end))
          throw std::runtime_error("Live native fixture write");
      },
      [&](const Json&) {
        ++events;
        return ObjectReply{};
      });
  Q4T_CHECK(owner && calls == 1 && events > 5 && result.reply.ok() &&
            result.terminal.GetString("id") == "resp_native_fixture" &&
            result.usage && result.usage->input == 12 &&
            result.usage->output == 7 && f.input.Used() == 0);
  Q4T_CHECK(f.Run([&](const auto&) { ++calls; }).reply.code ==
                "response_already_invoked" &&
            calls == 1);
  return true;
}
Q4T_TEST(live_responses_execution_protects_owner_body_quota_and_known_errors) {
  Execution f;
  size_t calls = 0;
  const auto quota = [&](const auto&) {
    ++calls;
    SendError(-1, 429, "Current limit", {}, "rate_limit_exceeded", "tools");
  };
  f.control = std::make_shared<RequestControl>("foreign", "", Clock::now() + 5s,
                                               "another");
  Q4T_CHECK(f.Run(quota).reply.code == "live_response_owner_invalid");
  f.control = std::make_shared<RequestControl>(
      "cancelled", "", Clock::now() - 1s, f.client.Id());
  Q4T_CHECK(f.Run(quota).reply.code == "deadline_exceeded");
  f.control = std::make_shared<RequestControl>("mine", "", Clock::now() + 5s,
                                               f.client.Id());
  Set(&f.settings, "instructions", String(std::string(2000, 'x')));
  Q4T_CHECK(f.Run(quota).reply.code == "body_capacity_exhausted" && calls == 0);
  Set(&f.settings, "instructions", String("valid"));
  auto error = f.Run(quota);
  Q4T_CHECK(
      calls == 1 && f.input.Used() == 0 && error.reply.http_status == 429 &&
      error.reply.code == "rate_limit_exceeded" && error.terminal.IsNull());
  return true;
}
Q4T_TEST(live_responses_execution_partial_drain_and_output_loss_never_replay) {
  Fixture fixture;
  Execution f;
  auto partial = f.Run([&](const auto&) {
    auto* http = HttpResponseContext::Current(-1);
    BeginStreamingResponse(-1, http->State());
    WriteAll(-1, fixture.begin);
  });
  Q4T_CHECK(!partial.reply.ok() && partial.terminal.IsNull() && !partial.usage);
  Execution second;
  auto lost = second.Run(
      [&](const auto&) {
        auto* http = HttpResponseContext::Current(-1);
        BeginStreamingResponse(-1, http->State());
        WriteAll(-1, fixture.begin);
      },
      [](const Json&) {
        ObjectReply reply;
        reply.code = "lost_receipt";
        reply.http_status = 503;
        reply.commit_unknown = true;
        return reply;
      });
  Q4T_CHECK(lost.reply.code == "lost_receipt" && lost.reply.commit_unknown &&
            second.input.Used() == 0);
  return true;
}
