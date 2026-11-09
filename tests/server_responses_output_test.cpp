#include "q4t/server/responses_output.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <limits>
#include <new>
#include <sys/socket.h>
#include <unistd.h>

#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
Json Options() {
  Json options;
  const auto status = q4t::io::ParseJson(R"({"model":"org/alias",
    "instructions":null,"metadata":{},"temperature":1,"top_p":1,
    "parallel_tool_calls":true,"tools":[],"tool_choice":"auto",
    "text":{"format":{"type":"text"}},"reasoning":{},"store":false,
    "background":false,"truncation":"disabled","access_programs":null})",
                                         &options);
  if (!status) throw std::runtime_error(status.message());
  return options;
}
bool Events(const std::string& wire, std::vector<Json>* out) {
  size_t begin = 0;
  while (begin < wire.size()) {
    const auto end = wire.find("\r\n\r\n", begin);
    if (end == wire.npos) return false;
    const auto data = wire.find("\r\ndata: ", begin);
    if (data == wire.npos || data > end ||
        wire.compare(begin, 7, "event: ") != 0)
      return false;
    Json event;
    if (!q4t::io::ParseJson(wire.substr(data + 8, end - data - 8), &event))
      return false;
    if (event.GetString("type") != wire.substr(begin + 7, data - begin - 7) ||
        event.GetInt("sequence_number", -1) !=
            static_cast<int64_t>(out->size()))
      return false;
    out->push_back(std::move(event));
    begin = end + 4;
  }
  return true;
}
ResponseDelta Text(std::string text, bool reasoning = false) {
  ResponseDelta result;
  result.kind = reasoning ? ResponseDelta::Kind::kReasoning
                          : ResponseDelta::Kind::kContent;
  result.text = std::move(text);
  return result;
}
ResponseDelta Tool(size_t index, std::string args, bool start,
                   bool custom = false) {
  ResponseDelta result;
  result.kind = ResponseDelta::Kind::kTool;
  result.tool_start = start;
  result.call = {index, start ? "call_" + std::to_string(index) : "",
                 start ? "lookup" : "", std::move(args), custom};
  return result;
}
bool Save(std::string_view name, const ResponsesOutput& output,
          const std::string& wire) {
  const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!directory) return true;  // Export is optional; all assertions still run.
  const auto root = std::filesystem::path(directory) / "responses";
  std::filesystem::create_directories(root);
  std::ofstream stream(root / (std::string(name) + ".wire"), std::ios::binary);
  stream << wire;
  std::ofstream json(root / (std::string(name) + ".json"));
  json << output.Json() << '\n';
  return stream.good() && json.good();
}
}  // namespace

Q4T_TEST(responses_output_text_reasoning_full_lifecycle) {
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_text", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.Append(Text("先计算。", true), "null", &part));
  wire += part;
  const auto reason_id =
      output.Snapshot().GetArray("output")->array[0].GetString("id");
  Q4T_CHECK(output.Append(Text("答案"), "null", &part));
  wire += part;
  Q4T_CHECK(output.Append(Text("是42✅"), R"({"content":[{"token":"42",
    "logprob":-0.5,"bytes":[52,50],"top_logprobs":[{"token":"43",
    "logprob":-2,"bytes":[52,51]}]}],"refusal":null})",
                          &part));
  wire += part;
  Q4T_CHECK(output.Finish(false, 0, {10, 4, 6, 2, 3}, 43, &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  Q4T_CHECK(events[0].GetString("type") == "response.created");
  Q4T_CHECK(events[1].GetString("type") == "response.in_progress");
  Q4T_CHECK(events.back().GetString("type") == "response.completed");
  Q4T_CHECK(wire.find("[DONE]") == wire.npos);
  const auto& snapshot = output.Snapshot();
  Q4T_CHECK(snapshot.GetString("status") == "completed");
  Q4T_CHECK(snapshot.Find("usage")->GetInt("total_tokens") == 16);
  Q4T_CHECK(snapshot.Find("usage")
                ->Find("input_tokens_details")
                ->GetInt("cache_write_tokens") == 3);
  const auto& items = snapshot.GetArray("output")->array;
  Q4T_CHECK(items.size() == 2 && items[0].GetString("id") == reason_id);
  Q4T_CHECK(items[0].GetArray("summary")->array.empty());
  const auto& content = items[1].GetArray("content")->array[0];
  Q4T_CHECK(content.GetString("text") == "答案是42✅");
  const auto& row = content.GetArray("logprobs")->array[0];
  Q4T_CHECK(OutputJson(*row.GetArray("bytes")) == "[52,50]");
  Q4T_CHECK(
      OutputJson(*row.GetArray("top_logprobs")->array[0].GetArray("bytes")) ==
      "[52,51]");
  for (const auto& event : events)
    if (event.GetString("type") == "response.output_text.delta" ||
        event.GetString("type") == "response.output_text.done")
      for (const auto& probability : event.GetArray("logprobs")->array) {
        Q4T_CHECK(!probability.Has("bytes"));
        for (const auto& top : probability.GetArray("top_logprobs")->array)
          Q4T_CHECK(!top.Has("bytes"));
      }
  Q4T_CHECK(!output.Finish(false, 0, {}, 44, &part));
  Q4T_CHECK(!output.Append(Text("late"), "null", &part));
  Q4T_CHECK(!output.Fail("late", "late", &part));
  Q4T_CHECK(Save("text", output, wire));
  return true;
}

Q4T_TEST(responses_summary_has_distinct_item_deltas_and_actual_usage) {
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_summary", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.Append(Text("17 plus 25 is 42. Verify the sum.", true),
                          "null", &part));
  wire += part;
  Q4T_CHECK(output.Append(Text("42"), "null", &part));
  wire += part;
  Q4T_CHECK(output.AppendSummary("Calculated ", &part));
  wire += part;
  const auto identity =
      output.Snapshot().GetArray("output")->array[2].GetString("id");
  Q4T_CHECK(output.AppendSummary("and checked the sum.", &part));
  wire += part;
  Q4T_CHECK(output.Finish(false, 0, {43, 0, 16, 10}, 44, &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  std::vector<std::string> summary_types;
  for (const auto& event : events) {
    const auto type = event.GetString("type");
    if (!type.starts_with("response.reasoning_summary_")) continue;
    summary_types.push_back(type);
    Q4T_CHECK(event.GetString("item_id") == identity &&
              event.GetInt("output_index") == 2 &&
              event.GetInt("summary_index", -1) == 0 &&
              !event.Has("content_index"));
  }
  Q4T_CHECK(summary_types ==
            std::vector<std::string>({"response.reasoning_summary_part.added",
                                      "response.reasoning_summary_text.delta",
                                      "response.reasoning_summary_text.delta",
                                      "response.reasoning_summary_text.done",
                                      "response.reasoning_summary_part.done"}));
  const auto& items = output.Snapshot().GetArray("output")->array;
  Q4T_CHECK(items.size() == 3 && items[0].GetArray("summary")->array.empty());
  Q4T_CHECK(!items[2].Has("content") && items[2].GetString("id") == identity);
  Q4T_CHECK(items[2].GetArray("summary")->array[0].GetString("text") ==
            "Calculated and checked the sum.");
  Q4T_CHECK(output.Snapshot().Find("usage")->GetInt("total_tokens") == 59);
  Q4T_CHECK(!output.AppendSummary("late", &part) && part.empty());
  Q4T_CHECK(Save("summary", output, wire));
  return true;
}

Q4T_TEST(responses_summary_incomplete_failed_and_cancelled_never_complete) {
  for (int terminal = 0; terminal < 3; ++terminal) {
    ResponsesOutput output;
    std::string wire, part;
    auto options = Options();
    if (terminal == 2)
      for (auto& [key, value] : options.object)
        if (key == "background") value.boolean = true;
    Q4T_CHECK(output.Init(options, "resp_summary_partial", 42, false));
    Q4T_CHECK(output.Start(&wire));
    if (terminal == 2) {
      Q4T_CHECK(output.StartRunning(&part));
      wire += part;
    }
    Q4T_CHECK(output.AppendSummary("Actual partial summary", &part));
    wire += part;
    if (terminal == 0) {
      Q4T_CHECK(output.Finish(true, 0, {40, 0, 9, 5}, 44, &part));
      wire += part;
    } else if (terminal == 1) {
      Q4T_CHECK(
          output.Fail("summary_failed", "actual executor failure", &part));
      wire += part;
    } else {
      Q4T_CHECK(output.Cancel());
    }
    std::vector<Json> events;
    Q4T_CHECK(Events(wire, &events));
    Q4T_CHECK(wire.find("response.completed") == wire.npos);
    if (terminal == 0) {
      bool partial = false;
      for (const auto& event : events)
        if (event.GetString("type") == "response.reasoning_summary_part.done")
          partial = event.GetString("status") == "incomplete";
      Q4T_CHECK(partial);
    } else {
      Q4T_CHECK(wire.find("summary_text.done") == wire.npos &&
                wire.find("summary_part.done") == wire.npos &&
                wire.find("response.output_item.done") == wire.npos);
    }
    Q4T_CHECK(!output.AppendSummary("late", &part));
    Q4T_CHECK(
        Save("summary_partial_" + std::to_string(terminal), output, wire));
  }
  return true;
}

Q4T_TEST(responses_output_parallel_function_custom_and_order) {
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_tools", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.Append(Tool(0, "{\"path\":", true), "null", &part));
  wire += part;
  Q4T_CHECK(output.Append(Tool(1, "STATUS", true, true), "null", &part));
  wire += part;
  Q4T_CHECK(
      output.Append(Tool(0, "\"docs/STATUS.md\"}", false), "null", &part));
  wire += part;
  Q4T_CHECK(output.Append(Tool(1, "\n末行", false, true), "null", &part));
  wire += part;
  Q4T_CHECK(output.Append(Text("调用已准备。"), "null", &part));
  wire += part;
  Q4T_CHECK(wire.find("arguments.done") == wire.npos);
  Q4T_CHECK(wire.find("custom_tool_call_input.done") == wire.npos);
  Q4T_CHECK(output.Finish(false, 2, {16, 0, 15, 0}, 44, &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  const auto& items = output.Snapshot().GetArray("output")->array;
  Q4T_CHECK(items.size() == 3);
  Q4T_CHECK(items[0].GetString("call_id") == "call_0");
  Q4T_CHECK(items[0].GetString("arguments") == "{\"path\":\"docs/STATUS.md\"}");
  Q4T_CHECK(items[1].GetString("input") == "STATUS\n末行");
  Q4T_CHECK(items[2].GetString("type") == "message");
  Q4T_CHECK(Save("tools", output, wire));
  return true;
}

Q4T_TEST(responses_output_incomplete_never_completes_partial_tool) {
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_partial", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.Append(Tool(0, "{\"path\":\"docs/", true), "null", &part));
  wire += part;
  Q4T_CHECK(!output.Finish(false, 0, {5, 0, 3, 0}, 43, &part));
  Q4T_CHECK(part.empty());
  Q4T_CHECK(output.Finish(true, 0, {5, 0, 3, 0}, 43, &part));
  wire += part;
  Q4T_CHECK(wire.find("arguments.done") == wire.npos);
  Q4T_CHECK(wire.find("response.completed") == wire.npos);
  const auto& response = output.Snapshot();
  Q4T_CHECK(response.GetString("status") == "incomplete");
  Q4T_CHECK(response.Find("completed_at")->IsNull());
  Q4T_CHECK(response.Find("incomplete_details")->GetString("reason") ==
            "max_output_tokens");
  Q4T_CHECK(response.GetArray("output")->array[0].GetString("status") ==
            "incomplete");
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  Q4T_CHECK(Save("partial", output, wire));
  return true;
}

Q4T_TEST(responses_output_failure_has_no_success_or_tool_done) {
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_failure", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.Append(Tool(0, "{", true), "null", &part));
  wire += part;
  Q4T_CHECK(output.Fail("explicit_cancel", "request cancelled", &part));
  wire += part;
  Q4T_CHECK(wire.find("arguments.done") == wire.npos);
  Q4T_CHECK(wire.find("response.output_item.done") == wire.npos);
  Q4T_CHECK(wire.find("response.completed") == wire.npos);
  Q4T_CHECK(output.Snapshot().GetString("status") == "failed");
  Q4T_CHECK(output.Snapshot().Find("usage")->IsNull());
  Q4T_CHECK(!output.Finish(false, 1, {}, 43, &part));
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  Q4T_CHECK(Save("failure", output, wire));
  return true;
}

Q4T_TEST(responses_output_rejects_identity_changes_and_orphan_fragments) {
  for (int kind = 0; kind < 5; ++kind) {
    ResponsesOutput output;
    std::string part;
    Q4T_CHECK(output.Init(Options(), "resp_identity", 42, false));
    Q4T_CHECK(output.Start(&part));
    Q4T_CHECK(output.Append(Tool(0, "{", true), "null", &part));
    auto delta = Tool(0, "}", false);
    if (kind == 0) delta.call.id = "changed";
    if (kind == 1) delta.call.name = "changed";
    if (kind == 2) delta.call.custom = true;
    if (kind == 3) delta.tool_start = true;
    if (kind == 4) delta.call.index = 4;
    Q4T_CHECK(!output.Append(delta, "null", &part));
    Q4T_CHECK(part.empty());
    Q4T_CHECK(output.Fail("bad_output", "invalid fragment", &part));
  }
  return true;
}

Q4T_TEST(responses_output_usage_limits_and_no_double_terminal) {
  ResponsesOutput output;
  std::string part;
  Q4T_CHECK(output.Init(Options(), "resp_bounds", 42, false, 4096));
  Q4T_CHECK(output.Start(&part));
  Q4T_CHECK(!output.Start(&part));
  for (const ResponsesUsage usage :
       {ResponsesUsage{-1, 0, 1, 0},
        {1, 2, 1, 0},
        {1, 0, 1, 2},
        {1, 0, 1, 0, -1},
        {1, 0, 1, 0, 2},
        {std::numeric_limits<int64_t>::max(), 0, 1, 0}})
    Q4T_CHECK(!output.Finish(false, 0, usage, 43, &part) && part.empty());
  Q4T_CHECK(!output.Append(Text(std::string(1000, 'x')), "null", &part));
  Q4T_CHECK(part.empty());
  Q4T_CHECK(output.Fail("response_storage_exhausted", "bounded", &part));
  Q4T_CHECK(output.Terminal());
  return true;
}

Q4T_TEST(responses_output_obfuscation_and_malformed_probabilities) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_padding", 42));
  Q4T_CHECK(output.Start(&wire));
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  Q4T_CHECK(!events[0].Has("obfuscation"));
  for (const auto json :
       {"{}", "{", R"({"content":[{"token":"x"}]})",
        R"({"content":[{"token":"x","logprob":1}]})",
        R"({"content":[{"token":"x","logprob":-1}]})",
        R"({"content":[{"token":"x","logprob":-1,"bytes":[256]}]})",
        R"({"content":[{"token":"x","logprob":-1,"bytes":[-1]}]})",
        R"({"content":[{"token":"x","logprob":-1,"bytes":[1.5]}]})"})
    Q4T_CHECK(!output.Append(Text("x"), json, &wire) && wire.empty());
  Q4T_CHECK(output.Snapshot().GetArray("output")->array.empty());
  Q4T_CHECK(output.Append(
      Text("�"),
      R"({"content":[{"token":"�","bytes":[226],"logprob":-1,"top_logprobs":[]}]})",
      &wire));
  Q4T_CHECK(Events(wire, &events));
  bool padded_delta = false;
  for (const auto& event : events) {
    if (event.GetString("type").ends_with(".delta")) {
      Q4T_CHECK(!event.GetString("obfuscation").empty());
      padded_delta = true;
    } else Q4T_CHECK(!event.Has("obfuscation"));
  }
  Q4T_CHECK(padded_delta);
  Q4T_CHECK(output.Finish(false, 0, {1, 0, 1, 0}, 43, &wire));
  const auto& content = output.Snapshot()
                            .GetArray("output")
                            ->array[0]
                            .GetArray("content")
                            ->array[0];
  Q4T_CHECK(
      OutputJson(*content.GetArray("logprobs")->array[0].GetArray("bytes")) ==
      "[226]");
  return true;
}

Q4T_TEST(responses_output_incomplete_parser_preserves_chat_contract) {
  Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"messages":[{"role":"user","content":"go"}],
    "tools":[{"type":"function","function":{"name":"noop",
    "parameters":{"type":"object","properties":{}}}}]})",
                               &request));
  for (bool responses : {false, true}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(
        request, "<|im_start|>assistant\n<think>\n\n</think>\n\n", "id", true));
    Q4T_CHECK(parser.Append("<tool_call><function=noop></function>", &deltas));
    Q4T_CHECK(parser.CompletedToolCalls() == 0);
    Q4T_CHECK(!deltas.empty() && deltas[0].tool_start);
    std::string arguments;
    for (const auto& delta : deltas) arguments += delta.call.arguments;
    Q4T_CHECK(arguments == "{");
    const auto fragments = deltas.size();
    const auto status = responses ? parser.FinishIncomplete(&deltas)
                                  : parser.Finish(true, &deltas);
    Q4T_CHECK(status.ok() == responses);
    Q4T_CHECK(parser.CompletedToolCalls() == 0);
    Q4T_CHECK(deltas.size() == fragments);
    Q4T_CHECK(!parser.Append("</tool_call>", &deltas));
  }
  return true;
}

Q4T_TEST(responses_output_http_failure_uses_native_terminal_once) {
  using namespace q4t::server::detail;
  for (bool direct : {false, true}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    ScopeExit close_sockets([&] {
      close(fd[0]);
      close(fd[1]);
    });
    ResponsesOutput output;
    HttpResponseContext context(fd[0]);
    std::string start;
    Q4T_CHECK(output.Init(Options(), "resp_http", 42, false));
    Q4T_CHECK(output.Start(&start));
    int handoffs = 0;
    context.SetBeforeTerminal(
        [](void* value) noexcept { ++*static_cast<int*>(value); }, &handoffs);
    context.SetResponsesStream(ResponsesOutput::RenderStreamError, &output,
                               output.Sequence());
    ScopeExit clear([&] {
      context.SetResponsesStream(nullptr, nullptr, nullptr);
      context.SetBeforeTerminal(nullptr, nullptr);
    });
    Q4T_CHECK(BeginStreamingResponse(fd[0], context.State()));
    Q4T_CHECK(WriteAll(fd[0], start));
    if (direct)
      SendError(fd[0], 409, "request cancelled", {}, "explicit_cancel");
    else
      SendStreamError(fd[0], context.State(), 409, "request cancelled", {},
                      "explicit_cancel");
    SendError(fd[0], 500, "must not append", {}, "internal_error");
    SendExceptionResponse(fd[0], *context.State());
    Q4T_CHECK(handoffs == 1 && output.Terminal());
    char buffer[8192];
    const auto count = recv(fd[1], buffer, sizeof(buffer), MSG_DONTWAIT);
    Q4T_CHECK(count > 0);
    const std::string wire(buffer, static_cast<size_t>(count));
    Q4T_CHECK(wire.find("[DONE]") == wire.npos);
    Q4T_CHECK(wire.find("must not append") == wire.npos);
    const auto begin = wire.find("\r\n\r\n");
    Q4T_CHECK(begin != wire.npos);
    std::vector<Json> events;
    Q4T_CHECK(Events(wire.substr(begin + 4), &events));
    Q4T_CHECK(events.size() == 3);
    Q4T_CHECK(events.back().GetString("type") == "response.failed");
    Q4T_CHECK(
        events.back().Find("response")->Find("error")->GetString("code") ==
        "explicit_cancel");
  }
  return true;
}

Q4T_TEST(responses_output_incomplete_checks_completed_rich_schema_receipts) {
  Json request, value;
  Q4T_CHECK(q4t::io::ParseJson(R"({"messages":[{"role":"user","content":"go"}],
    "tools":[{"type":"function","function":{"name":"read","strict":true,
    "parameters":{"type":"object","properties":{"value":{"type":"string",
    "pattern":"^[a-z]+$"}},"required":["value"],"additionalProperties":false}}}]})",
                               &request, true, kSchemaJsonLimits));
  Q4T_CHECK(q4t::io::ParseJson(R"({"value":"yes"})", &value));
  for (bool valid : {false, true}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(
        request, "<|im_start|>assistant\n<think>\n\n</think>\n\n", "id", true));
    std::vector<SchemaReceipt> receipts;
    if (valid) {
      SchemaReceipt receipt;
      const auto* schema = request.GetArray("tools")
                               ->array[0]
                               .Find("function")
                               ->Find("parameters");
      Q4T_CHECK(MakeSchemaReceipt("read", *schema, value, &receipt));
      receipts.push_back(receipt);
    }
    parser.SetSchemaReceipts(&receipts);
    Q4T_CHECK(parser.Append(
        "<tool_call>\n<function=read>\n<parameter=arguments>\n"
        "{\"value\":\"yes\"}\n</parameter>\n</function>\n</tool_call>",
        &deltas));
    Q4T_CHECK(parser.CompletedToolCalls() == 1);
    Q4T_CHECK(parser.FinishIncomplete(&deltas).ok() == valid);
  }
  return true;
}

Q4T_TEST(responses_output_http_exception_uses_fixed_native_error) {
  using namespace q4t::server::detail;
  for (const auto kind :
       {ExceptionKind::kHostMemory, ExceptionKind::kInternal}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    ScopeExit close_sockets([&] {
      close(fd[0]);
      close(fd[1]);
    });
    HttpResponseContext context(fd[0]);
    int64_t sequence = 8;
    bool called = false;
    context.SetResponsesStream(
        [](void* owner, int, const std::string&, std::string_view,
           std::string_view) -> std::string {
          *static_cast<bool*>(owner) = true;
          throw std::bad_alloc();
        },
        &called, &sequence);
    ScopeExit clear(
        [&] { context.SetResponsesStream(nullptr, nullptr, nullptr); });
    Q4T_CHECK(BeginStreamingResponse(fd[0], context.State()));
    SendExceptionResponse(fd[0], *context.State(), kind);
    SendExceptionResponse(fd[0], *context.State(), kind);
    Q4T_CHECK(!called && sequence == 9);
    char buffer[4096];
    const auto count = recv(fd[1], buffer, sizeof(buffer), MSG_DONTWAIT);
    Q4T_CHECK(count > 0);
    const std::string wire(buffer, static_cast<size_t>(count));
    Q4T_CHECK(wire.find("event: error\r\ndata: ") != wire.npos);
    Q4T_CHECK(wire.find("\"sequence_number\":8") != wire.npos);
    Q4T_CHECK(wire.find("[DONE]") == wire.npos);
    Q4T_CHECK(wire.find(kind == ExceptionKind::kHostMemory
                            ? "host_memory_exhausted"
                            : "internal_error") != wire.npos);
    Q4T_CHECK(*context.State() == ResponseState::kTerminal);
  }
  return true;
}

Q4T_TEST(responses_output_http_preheader_errors_still_have_http_status) {
  using namespace q4t::server::detail;
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  ScopeExit close_sockets([&] {
    close(fd[0]);
    close(fd[1]);
  });
  ResponsesOutput output;
  HttpResponseContext context(fd[0]);
  context.SetResponsesStream(ResponsesOutput::RenderStreamError, &output,
                             output.Sequence());
  ScopeExit clear(
      [&] { context.SetResponsesStream(nullptr, nullptr, nullptr); });
  SendError(fd[0], 400, "bad input", {}, "invalid_request", "input[0].role");
  char buffer[4096];
  const auto count = recv(fd[1], buffer, sizeof(buffer), MSG_DONTWAIT);
  Q4T_CHECK(count > 0);
  const std::string wire(buffer, static_cast<size_t>(count));
  Q4T_CHECK(wire.starts_with("HTTP/1.1 400 Bad Request\r\n"));
  Q4T_CHECK(wire.find("input[0].role") != wire.npos);
  Q4T_CHECK(wire.find("event:") == wire.npos && *output.Sequence() == 0);
  return true;
}
