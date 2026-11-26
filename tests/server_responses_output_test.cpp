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

Q4T_TEST(refusal_responses_parser_events_and_all_terminal_states) {
  for (const std::string ending :
       {"complete", "incomplete", "cancel", "fail"}) {
    Json request;
    Q4T_CHECK(q4t::io::ParseJson(
        R"({"messages":[{"role":"user","content":"query"}],
            "enable_thinking":false})",
        &request));
    ChatResponseParser parser;
    Q4T_CHECK(parser.Init(request, "<think>\n\n</think>\n\n", "r", true));
    ResponsesOutput output;
    auto options = Options();
    if (ending == "cancel")
      for (auto& [key, value] : options.object)
        if (key == "background") value.boolean = true;
    std::string wire, part;
    Q4T_CHECK(output.Init(options, "resp_refusal_" + ending, 42, false));
    Q4T_CHECK(output.Start(&wire));
    if (ending == "cancel") {
      Q4T_CHECK(output.StartRunning(&part));
      wire += part;
    }
    for (const auto* piece : {"<q4t_refusal>不能", "提供私有记录✅"}) {
      std::vector<ResponseDelta> deltas;
      Q4T_CHECK(parser.Append(piece, &deltas));
      for (const auto& delta : deltas) {
        Q4T_CHECK(output.Append(delta, "null", &part));
        wire += part;
      }
    }
    Q4T_CHECK(wire.find("response.refusal.delta") != wire.npos);
    Q4T_CHECK(wire.find("response.refusal.done") == wire.npos);
    std::vector<ResponseDelta> deltas;
    if (ending == "complete") {
      Q4T_CHECK(parser.Append("</q4t_refusal>", &deltas));
      Q4T_CHECK(parser.Finish(false, &deltas) && deltas.empty());
      Q4T_CHECK(output.Finish(false, 0, {10, 0, 9, 0, 0}, 43, &part));
      wire += part;
    } else if (ending == "incomplete") {
      Q4T_CHECK(parser.Append("</q4t_refu", &deltas));
      Q4T_CHECK(parser.FinishIncomplete(&deltas) && deltas.empty());
      Q4T_CHECK(output.Finish(true, 0, {10, 0, 9, 0, 0}, 43, &part));
      wire += part;
    } else if (ending == "cancel") {
      Q4T_CHECK(output.Cancel());
    } else {
      Q4T_CHECK(!parser.Finish(false, &deltas));
      Q4T_CHECK(
          output.Fail("response_parse_failed", "Unclosed refusal", &part));
      wire += part;
    }
    std::vector<Json> events;
    Q4T_CHECK(Events(wire, &events));
    const auto& snapshot = output.Snapshot();
    Q4T_CHECK(snapshot.GetString("status") ==
              (ending == "complete" ? "completed"
               : ending == "cancel" ? "cancelled"
               : ending == "fail"   ? "failed"
                                    : "incomplete"));
    const auto* items = snapshot.GetArray("output");
    Q4T_CHECK(items && items->array.size() == 1);
    const auto& message = items->array[0];
    Q4T_CHECK(message.GetString("type") == "message" &&
              message.GetString("role") == "assistant");
    const auto& refusal = message.GetArray("content")->array.at(0);
    Q4T_CHECK(refusal.GetString("type") == "refusal" &&
              refusal.GetString("refusal") == "不能提供私有记录✅");
    Q4T_CHECK(!refusal.Has("text") && !refusal.Has("logprobs"));
    std::string combined;
    size_t done = 0, part_done = 0, item_done = 0, completed = 0;
    for (const auto& event : events) {
      const auto type = event.GetString("type");
      if (type.starts_with("response.refusal.")) {
        Q4T_CHECK(event.GetString("item_id") == message.GetString("id"));
        Q4T_CHECK(event.GetInt("output_index", -1) == 0 &&
                  event.GetInt("content_index", -1) == 0);
        Q4T_CHECK(!event.Has("text") && !event.Has("logprobs"));
        if (type.ends_with(".delta"))
          combined += event.GetString("delta");
        else {
          ++done;
          Q4T_CHECK(event.GetString("refusal") == combined);
        }
      } else if (type == "response.content_part.done") {
        Q4T_CHECK(done == 1 &&
                  OutputJson(*event.Find("part")) == OutputJson(refusal));
        ++part_done;
      } else if (type == "response.output_item.done") {
        Q4T_CHECK(part_done == 1);
        ++item_done;
      } else if (type == "response.completed") {
        Q4T_CHECK(item_done == 1 && ending == "complete");
        ++completed;
      }
    }
    Q4T_CHECK(combined == refusal.GetString("refusal"));
    Q4T_CHECK(completed == (ending == "complete" ? 1 : 0));
    Q4T_CHECK(done == (ending == "complete" || ending == "incomplete" ? 1 : 0));
    Q4T_CHECK(Save("refusal-" + ending, output, wire));
  }
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
  Q4T_CHECK(output.Snapshot().Find("error")->GetString("code") ==
            "explicit_cancel");
  Q4T_CHECK(!output.Finish(false, 1, {}, 43, &part));
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  Json resource;
  Q4T_CHECK(q4t::io::ParseJson(output.Json(), &resource));
  const auto* error = resource.Find("error");
  Q4T_CHECK(error && error->GetString("code") == "server_error");
  Q4T_CHECK(error->GetString("type") == "explicit_cancel" &&
            error->GetString("message") == "request cancelled");
  Q4T_CHECK(OutputJson(*events.back().Find("response")) == output.Json());
  Q4T_CHECK(Save("failure", output, wire));
  return true;
}

Q4T_TEST(responses_beta_agent_result_preserves_scores_and_projects_history) {
  Json item;
  Q4T_CHECK(q4t::io::ParseJson(R"({"type":"multi_agent_call_output","id":"ag_1",
    "action":"list_agents","agent":{"agent_name":"root"},"call_id":"call_1",
    "output":[{"type":"output_text","text":"ok","annotations":[]}]})",
                               &item));
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_beta", 42, false) &&
            output.Start(&wire));
  Q4T_CHECK(output.AppendAgentItem(item, &part));
  wire += part;
  Q4T_CHECK(output.Fail("explicit_cancel", "stopped", &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  const auto projected = ResponsesWireValue(item);
  const auto* scores =
      projected.GetArray("output")->array[0].GetArray("logprobs");
  Q4T_CHECK(scores && scores->array.empty() &&
            OutputJson(ResponsesPublicValue(item)) == OutputJson(item));
  int item_events = 0;
  for (const auto& event : events) {
    if (const auto* visible = event.Find("item")) {
      Q4T_CHECK(OutputJson(*visible) == OutputJson(projected));
      ++item_events;
    }
  }
  Q4T_CHECK(item_events == 2);
  const auto* last = events.back().Find("response");
  Q4T_CHECK(last && OutputJson(*last) == output.Json() &&
            OutputJson(last->GetArray("output")->array[0]) ==
                OutputJson(projected));
  Json recorded;
  Q4T_CHECK(q4t::io::ParseJson(R"({"type":"multi_agent_call_output","output":[{
    "type":"output_text","text":"ok","logprobs":[{"token":"ok","logprob":-1,
    "bytes":[111,107],"top_logprobs":[]}]}]})",
                               &recorded));
  Q4T_CHECK(OutputJson(ResponsesWireValue(recorded)) == OutputJson(recorded));
  return true;
}

Q4T_TEST(responses_output_failure_classification) {
  for (const auto& [native, expected] :
       {std::pair{"client_tpm_exhausted", "rate_limit_exceeded"},
        {"client_rpm_exhausted", "rate_limit_exceeded"},
        {"client_prompt_limit_exceeded", "invalid_prompt"},
        {"context_length_exceeded", "invalid_prompt"},
        {"response_storage_exhausted", "server_error"},
        {"image_file_not_found", "image_file_not_found"},
        {"rate_limit_exceeded", "rate_limit_exceeded"},
        {"vector_store_timeout", "vector_store_timeout"}}) {
    ResponsesOutput output;
    std::string wire;
    Q4T_CHECK(output.Init(Options(), "resp_cause", 42, false));
    Q4T_CHECK(output.Start(&wire) && output.Fail(native, "detail", &wire));
    Json value;
    Q4T_CHECK(q4t::io::ParseJson(output.Json(), &value));
    const auto* error = value.Find("error");
    Q4T_CHECK(error && error->GetString("code") == expected);
    if (std::string_view(native) == expected)
      Q4T_CHECK(!error->Has("type"));
    else
      Q4T_CHECK(error->GetString("type") == native);
    Q4T_CHECK(OutputJson(ResponsesWireValue(value)) == output.Json());
  }
  for (const auto* input :
       {R"({"type":"error","code":"object_commit_unknown"})",
        R"({"object":"response","status":"cancelled","error":null})",
        R"({"type":"mcp_call","error":{"code":"tool_error"}})"}) {
    Json value;
    Q4T_CHECK(q4t::io::ParseJson(input, &value));
    Q4T_CHECK(OutputJson(ResponsesWireValue(value)) == OutputJson(value));
  }
  Json recorded;
  Q4T_CHECK(q4t::io::ParseJson(R"({"object":"response","status":"failed",
    "error":{"code":"runner_error","message":"detail","type":"original",
             "param":"input"},"output":[{"type":"mcp_call","error":{"code":53}}]})",
                               &recorded));
  const auto projected = ResponsesWireValue(recorded);
  Q4T_CHECK(projected.Find("error")->GetString("code") == "server_error" &&
            projected.Find("error")->GetString("type") == "original" &&
            projected.Find("error")->GetString("param") == "input" &&
            OutputJson(*projected.Find("output")) ==
                OutputJson(*recorded.Find("output")));
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
    } else
      Q4T_CHECK(!event.Has("obfuscation"));
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

Q4T_TEST(responses_cancel_delivers_the_sequenced_frame_before_failure) {
  using namespace q4t::server::detail;
  using namespace std::chrono_literals;
  using Clock = std::chrono::steady_clock;
  for (bool deadline : {false, true}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    ScopeExit close_sockets([&] {
      close(fd[0]);
      close(fd[1]);
    });
    HttpResponseContext http(fd[0]);
    ResponsesOutput output;
    std::string first, delta;
    Q4T_CHECK(output.Init(Options(), "resp_cancel", 42, false));
    Q4T_CHECK(output.Start(&first) &&
              output.Append(Text("partial"), "null", &delta));
    Q4T_CHECK(BeginStreamingResponse(fd[0], http.State()) &&
              WriteAll(fd[0], first));
    RequestControl control("req_cpu", "key",
                           Clock::now() + (deadline ? -1s : 10s));
    if (!deadline) control.Cancel();
    Q4T_CHECK(control.Cancelled());
    Q4T_CHECK(WriteAll(fd[0], delta,
                       {.control = &control, .finish_response_event = true}));
    std::string terminal;
    Q4T_CHECK(output.Fail(control.Reason(), "stopped", &terminal));
    Q4T_CHECK(WriteAll(fd[0], terminal));
    shutdown(fd[0], SHUT_WR);
    std::string raw;
    char received[4096];
    ssize_t count;
    while ((count = recv(fd[1], received, sizeof(received), 0)) > 0)
      raw.append(received, count);
    Q4T_CHECK(count == 0);
    const auto begin = raw.find("\r\n\r\n");
    std::vector<Json> events;
    Q4T_CHECK(begin != raw.npos && Events(raw.substr(begin + 4), &events));
    Q4T_CHECK(events.size() >= 2 &&
              events.back().GetString("type") == "response.failed" &&
              events[events.size() - 2].GetString("type") ==
                  "response.output_text.delta");
  }
  return true;
}

Q4T_TEST(responses_cancel_keeps_backpressure_and_shutdown_bounded) {
  using namespace q4t::server::detail;
  using namespace std::chrono_literals;
  using Clock = std::chrono::steady_clock;
  for (bool shutdown : {false, true}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    ScopeExit close_sockets([&] {
      close(fd[0]);
      close(fd[1]);
    });
    HttpResponseContext http(fd[0]);
    int limit = 2048;
    Q4T_CHECK(setsockopt(fd[0], SOL_SOCKET, SO_SNDBUF, &limit, sizeof(limit)) ==
              0);
    RequestControl control("req_cpu", "key", Clock::now() + 10s);
    control.Cancel(shutdown ? RequestControl::State::kShutdown
                            : RequestControl::State::kExplicit);
    const auto start = Clock::now();
    Q4T_CHECK(!WriteAll(fd[0], std::string(1 << 20, 'x'),
                        {.control = &control, .finish_response_event = true}));
    Q4T_CHECK(Clock::now() - start < 2s);
    Q4T_CHECK(!WriteAll(fd[0], "terminal"));
    char byte;
    Q4T_CHECK((recv(fd[1], &byte, 1, MSG_DONTWAIT) == 1) == !shutdown);
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
    const auto* error = events.back().Find("response")->Find("error");
    Q4T_CHECK(error && error->GetString("code") == "server_error" &&
              error->GetString("type") == "explicit_cancel");
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

Q4T_TEST(responses_file_search_output_lifecycle_closes_once_before_answer) {
  ResponsesOutput output;
  std::string wire, part;
  Q4T_CHECK(output.Init(Options(), "resp_search", 42, false));
  Q4T_CHECK(output.Start(&wire));
  size_t index;
  Q4T_CHECK(output.StartFileSearch(&index, &part));
  wire += part;
  Json queries, results;
  Q4T_CHECK(q4t::io::ParseJson(R"(["PLE SSD"])", &queries));
  Q4T_CHECK(q4t::io::ParseJson(R"([{"file_id":"file_real","filename":"MODEL.md",
    "score":0.5,"text":"PLE streams from SSD"}])",
                               &results));
  Q4T_CHECK(output.SearchFileSearch(index, queries, &part));
  wire += part;
  Q4T_CHECK(output.CompleteFileSearch(index, results, &part));
  wire += part;
  Q4T_CHECK(output.Append(Text("PLE streams from SSD."), "null", &part));
  wire += part;
  Q4T_CHECK(output.Finish(false, 0, {25, 0, 8}, 43, &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  int done = 0, completed = 0;
  for (const auto& event : events) {
    const auto type = event.GetString("type");
    Q4T_CHECK(type.find("function_call_arguments") == type.npos);
    if (type == "response.output_item.done" &&
        event.GetInt("output_index", -1) == static_cast<int64_t>(index))
      ++done;
    if (type == "response.file_search_call.completed") ++completed;
  }
  Q4T_CHECK(done == 1 && completed == 1 &&
            output.Snapshot().GetArray("output")->array[0].GetString(
                "status") == "completed");
  return true;
}

Q4T_TEST(responses_file_search_output_never_completes_unexecuted_tool) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_search", 42, false) &&
            output.Start(&wire));
  size_t index;
  Q4T_CHECK(output.StartFileSearch(&index, &wire));
  Q4T_CHECK(!output.Finish(false, 0, {}, 43, &wire) && wire.empty());
  Q4T_CHECK(output.Finish(true, 0, {}, 43, &wire));
  Q4T_CHECK(wire.find("file_search_call.completed") == wire.npos &&
            output.Snapshot().GetArray("output")->array[0].GetString(
                "status") == "incomplete");
  return true;
}

Q4T_TEST(responses_file_search_output_limit_skip_does_not_claim_searching) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_search", 42, false) &&
            output.Start(&wire));
  size_t index;
  Q4T_CHECK(output.StartFileSearch(&index, &wire));
  Json queries;
  Q4T_CHECK(q4t::io::ParseJson(R"(["skip"])", &queries));
  Q4T_CHECK(output.SkipFileSearch(index, queries, &wire) &&
            wire.find("file_search_call.searching") == wire.npos &&
            wire.find("file_search_call.completed") == wire.npos);
  Q4T_CHECK(output.Finish(false, 0, {}, 43, &wire));
  const auto& item = output.Snapshot().GetArray("output")->array[0];
  Q4T_CHECK(item.GetString("status") == "incomplete" &&
            item.GetArray("queries")->array[0].str == "skip" &&
            item.Find("results")->IsNull());
  return true;
}

Q4T_TEST(responses_file_search_output_failure_preserves_prior_completed_tool) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_search", 42, false) &&
            output.Start(&wire));
  Json queries, results;
  Q4T_CHECK(q4t::io::ParseJson(R"(["PLE"])", &queries) &&
            q4t::io::ParseJson("[]", &results));
  size_t first, second;
  Q4T_CHECK(output.StartFileSearch(&first, &wire) &&
            output.SearchFileSearch(first, queries, &wire) &&
            output.CompleteFileSearch(first, results, &wire));
  Q4T_CHECK(output.StartFileSearch(&second, &wire) &&
            output.FailFileSearch(second, &wire));
  Q4T_CHECK(wire.find("file_search_call.completed") == wire.npos &&
            output.Fail("lookup_failed", "source unavailable", &wire));
  const auto& items = output.Snapshot().GetArray("output")->array;
  Q4T_CHECK(items[first].GetString("status") == "completed" &&
            items[second].GetString("status") == "failed");
  return true;
}

namespace {
bool CitationSource(ResponsesOutput* output, std::string* wire) {
  Json queries, results;
  if (!q4t::io::ParseJson(R"(["PLE"])", &queries) ||
      !q4t::io::ParseJson(R"([{"file_id":"file_real","filename":"MODEL.md",
        "score":0.5,"text":"actual passage"}])",
                          &results))
    return false;
  size_t index;
  return output->StartFileSearch(&index, wire) &&
         output->SearchFileSearch(index, queries, wire) &&
         output->CompleteFileSearch(index, results, wire);
}
}  // namespace

Q4T_TEST(
    responses_file_citations_bind_actual_prior_source_and_unicode_position) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_cite", 42, false) &&
            output.Start(&wire) && CitationSource(&output, &wire));
  Q4T_CHECK(output.Append(Text("证😀[file:fi"), "null", &wire) &&
            output.CiteFileSearchSources(&wire) && wire.empty());
  Q4T_CHECK(output.Append(Text("le_real]"), "null", &wire) &&
            output.CiteFileSearchSources(&wire));
  Q4T_CHECK(wire.find("response.output_text.annotation.added") != wire.npos);
  const auto& part = output.Snapshot()
                         .GetArray("output")
                         ->array.back()
                         .GetArray("content")
                         ->array[0];
  const auto& annotation = part.GetArray("annotations")->array[0];
  Q4T_CHECK(
      annotation.GetString("file_id") == "file_real" &&
      annotation.GetString("filename") == "MODEL.md" &&
      annotation.GetInt("index") ==
          2 + static_cast<int64_t>(std::string("[file:file_real]").size()) &&
      part.GetString("text") == "证😀[file:file_real]");
  Q4T_CHECK(output.CiteFileSearchSources(&wire) && wire.empty());
  Q4T_CHECK(output.Finish(false, 0, {10, 0, 8}, 43, &wire));
  Q4T_CHECK(wire.find("file_citation") != wire.npos);
  return true;
}

Q4T_TEST(
    responses_file_citations_do_not_invent_sources_or_retroactive_evidence) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_cite", 42, false) &&
            output.Start(&wire));
  Q4T_CHECK(output.Append(Text("Earlier [file:file_real]"), "null", &wire));
  Q4T_CHECK(CitationSource(&output, &wire));
  Q4T_CHECK(output.Append(Text("Unknown [file:file_fake]"), "null", &wire));
  Q4T_CHECK(output.CiteFileSearchSources(&wire) && wire.empty());
  const auto& items = output.Snapshot().GetArray("output")->array;
  Q4T_CHECK(items[0]
                .GetArray("content")
                ->array[0]
                .GetArray("annotations")
                ->array.empty() &&
            items.back()
                .GetArray("content")
                ->array[0]
                .GetArray("annotations")
                ->array.empty());
  return true;
}

Q4T_TEST(
    responses_file_citations_preserve_repeated_occurrences_and_event_order) {
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(Options(), "resp_cite", 42, false) &&
            output.Start(&wire) && CitationSource(&output, &wire));
  Q4T_CHECK(output.Append(Text("A[file:file_real] B[file:file_real]"), "null",
                          &wire));
  Q4T_CHECK(output.CiteFileSearchSources(&wire));
  const auto& annotations = output.Snapshot()
                                .GetArray("output")
                                ->array.back()
                                .GetArray("content")
                                ->array[0]
                                .GetArray("annotations")
                                ->array;
  Q4T_CHECK(annotations.size() == 2 &&
            annotations[0].GetInt("index") < annotations[1].GetInt("index"));
  const auto first = wire.find("\"annotation_index\":0");
  const auto second = wire.find("\"annotation_index\":1");
  Q4T_CHECK(first != wire.npos && second != wire.npos && first < second);
  return true;
}

Q4T_TEST(responses_namespace_json_sse_and_fragments_keep_public_calls) {
  Json declarations;
  Q4T_CHECK(q4t::io::ParseJson(R"({"model":"cpu","tools":[
    {"type":"namespace","name":"crm","description":"records","tools":[
      {"type":"function","name":"read"},{"type":"custom","name":"query"}]}]})",
                               &declarations));
  auto options = Options();
  for (auto& [key, value] : options.object)
    if (key == "tools") value = *declarations.Find("tools");
  for (const bool incomplete : {false, true}) {
    ResponsesOutput output;
    std::string wire, part;
    Q4T_CHECK(output.Init(options, "resp_namespaced", 42, false));
    Q4T_CHECK(output.Start(&wire));
    for (size_t i = 0; i < 2; ++i) {
      const bool custom = i == 1;
      const auto name = ResponsesToolModelName("crm", custom ? "query" : "read",
                                               custom ? "custom" : "function");
      auto delta = Tool(i, custom ? "SELECT " : "{", true, custom);
      delta.call.name = name;
      Q4T_CHECK(output.Append(delta, "null", &part));
      wire += part;
      delta = Tool(i, custom ? "*" : "}", false, custom);
      delta.call.name = name;
      Q4T_CHECK(output.Append(delta, "null", &part));
      wire += part;
    }
    Q4T_CHECK(output.Finish(incomplete, incomplete ? 1 : 2, {10, 0, 4, 0}, 43,
                            &part));
    wire += part;
    Q4T_CHECK(wire.find("q4t_ns_") == std::string::npos);
    const auto& items = output.Snapshot().GetArray("output")->array;
    Q4T_CHECK(items[0].GetString("name") == "read" &&
              items[0].GetString("namespace") == "crm" &&
              items[0].GetString("arguments") == "{}");
    Q4T_CHECK(items[1].GetString("name") == "query" &&
              items[1].GetString("namespace") == "crm");
    std::vector<Json> events;
    Q4T_CHECK(Events(wire, &events));
    Q4T_CHECK(events.back().GetString("type") ==
              (incomplete ? "response.incomplete" : "response.completed"));
    Q4T_CHECK(
        Save(incomplete ? "namespace-incomplete" : "namespace", output, wire));
  }
  return true;
}

Q4T_TEST(responses_namespace_wrong_model_alias_never_leaks_as_a_public_name) {
  Json options;
  Q4T_CHECK(q4t::io::ParseJson(R"({"tools":[{"type":"namespace","name":"crm",
    "description":"records","tools":[{"type":"function","name":"read"}]}]})",
                               &options));
  ResponsesOutput output;
  std::string wire;
  Q4T_CHECK(output.Init(options, "resp_namespace_errors", 42, false));
  Q4T_CHECK(output.Start(&wire));
  auto delta = Tool(0, "{", true);
  delta.call.name = ResponsesToolModelName("support", "read", "function");
  Q4T_CHECK(!output.Append(delta, "null", &wire));
  Q4T_CHECK(wire.empty() &&
            output.Snapshot().GetArray("output")->array.empty());
  delta.call.name = ResponsesToolModelName("crm", "read", "function");
  Q4T_CHECK(output.Append(delta, "null", &wire));
  delta.tool_start = false;
  delta.call.name = ResponsesToolModelName("support", "read", "function");
  Q4T_CHECK(!output.Append(delta, "null", &wire));
  Q4T_CHECK(wire.empty());
  return true;
}

Q4T_TEST(responses_custom_output_projection_all_terminal_states) {
  for (const auto* ending : {"complete", "incomplete", "fail", "cancel"}) {
    auto options = Options();
    for (auto& [key, value] : options.object)
      if (key == "background") value.boolean = true;
    ResponsesOutput output;
    std::string wire, part;
    Q4T_CHECK(output.Init(options, "resp_custom_projection", 42, false));
    Q4T_CHECK(output.Start(&wire));
    Q4T_CHECK(output.StartRunning(&part));
    wire += part;
    Q4T_CHECK(output.Append(Tool(0, "raw\n中文", true, true), "null", &part));
    wire += part;
    if (std::string(ending) == "cancel") {
      Q4T_CHECK(output.Cancel());
    } else {
      if (std::string(ending) == "fail")
        Q4T_CHECK(output.Fail("worker_failed", "fixture", &part));
      else
        Q4T_CHECK(output.Finish(std::string(ending) == "incomplete",
                                std::string(ending) == "complete" ? 1 : 0,
                                {8, 0, 3, 0}, 43, &part));
      wire += part;
    }
    Json response;
    Q4T_CHECK(q4t::io::ParseJson(output.Json(), &response));
    Q4T_CHECK(!response.GetArray("output")->array[0].Has("status"));
    Q4T_CHECK(output.Snapshot().GetArray("output")->array[0].Has("status"));
    Q4T_CHECK((wire.find("custom_tool_call_input.done") != wire.npos) ==
              (std::string(ending) == "complete"));
    Q4T_CHECK(Save(std::string("custom-projection-") + ending, output, wire));
  }
  return true;
}

Q4T_TEST(agent_session_delta_padding_is_per_subscription) {
  q4t::io::Json delta;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"type":"agent.session.turn.output_text.delta","delta":"42","event_id":"evt_same"})",
      &delta));
  const auto retained = q4t::server::OutputJson(delta);
  q4t::server::ApplyDeltaObfuscation(&delta, true);
  Q4T_CHECK(!delta.GetString("obfuscation").empty());
  Q4T_CHECK(delta.GetString("event_id") == "evt_same");
  q4t::server::ApplyDeltaObfuscation(&delta, false);
  Q4T_CHECK(q4t::server::OutputJson(delta) == retained);
  q4t::io::Json done;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"type":"agent.session.turn.item.done","obfuscation":"old"})", &done));
  q4t::server::ApplyDeltaObfuscation(&done, true);
  Q4T_CHECK(!done.Has("obfuscation"));
  return true;
}

Q4T_TEST(
    hosted_python_file_links_bind_committed_unicode_paths_and_skip_missing_files) {
  ResponsesOutput output;
  std::string wire;
  size_t index;
  Json files;
  Q4T_CHECK(q4t::io::ParseJson(
      R"([{"container_id":"cntr_a","id":"cfile_a","path":"/mnt/data/报告 x.txt","source":"assistant"}])",
      &files));
  Q4T_CHECK(output.Init(Options(), "resp_report", 42, false) &&
            output.Start(&wire));
  Q4T_CHECK(output.StartPython("cntr_a", "print(42)", &index, &wire) &&
            output.EndPython(index, "42", false, &files, &wire));
  const std::string url = "sandbox:/mnt/data/报告%20x.txt";
  Q4T_CHECK(
      output.Append(Text("证😀(" + url + ") (sandbox:/mnt/data/foreign.txt)"),
                    "null", &wire));
  int exports = 0;
  auto save = [&](const std::string& message, const Json& file) {
    if (message.empty() || file.GetString("id") != "cfile_a")
      return q4t::Status::Fail("unexpected file");
    ++exports;
    return q4t::Status{};
  };
  Q4T_CHECK(output.CitePythonFiles(save, &wire) && exports == 1 &&
            wire.find("response.output_text.annotation.added") != wire.npos);
  const auto& refs = output.Snapshot()
                         .GetArray("output")
                         ->array.back()
                         .GetArray("content")
                         ->array[0]
                         .GetArray("annotations")
                         ->array;
  Q4T_CHECK(refs.size() == 1 && refs[0].GetInt("start_index") == 3 &&
            refs[0].GetInt("end_index") ==
                static_cast<int64_t>(3 + url.size() - 4) &&
            refs[0].GetString("filename") == "报告 x.txt" &&
            refs[0].GetString("container_id") == "cntr_a");
  Q4T_CHECK(output.CitePythonFiles(save, &wire) && wire.empty() &&
            exports == 1);
  files.array.clear();
  Q4T_CHECK(output.StartPython("cntr_a", "pass", &index, &wire) &&
            output.EndPython(index, "", false, &files, &wire));
  Q4T_CHECK(output.Append(Text(url), "null", &wire) &&
            output.CitePythonFiles(save, &wire) && wire.empty() &&
            exports == 1);
  return true;
}

Q4T_TEST(responses_local_shell_stream_exposes_only_typed_actions) {
  Json options;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"model":"test","tools":[{"type":"local_shell"}],)"
      R"("tool_choice":{"type":"local_shell"}})", &options));
  ResponsesOutput output;
  std::string start, added, fragment, terminal;
  Q4T_CHECK(output.Init(options, "resp_local", 42, false));
  Q4T_CHECK(output.Start(&start));
  auto call = Tool(0, R"({"command":["cat",)", true);
  call.call.name = "q4t_builtin_local_shell";
  Q4T_CHECK(output.Append(call, "null", &added));
  auto branch = output;
  Q4T_CHECK(
      output.Append(Tool(0, R"("docs/MODEL.md"]})", false), "null", &fragment));
  Q4T_CHECK(fragment.empty());
  Q4T_CHECK(output.Finish(false, 1, {5, 0, 3, 0}, 43, &terminal));
  auto wire = start + added + terminal;
  Q4T_CHECK(wire.find("function_call_arguments") == wire.npos);
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  Json expected;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"type":"allowed_tools","mode":"required",)"
      R"("tools":[{"type":"local_shell"}]})", &expected));
  for (const auto& event : events) {
    if (const auto* resource = event.Find("response")) {
      Q4T_CHECK(resource->Has("tool_choice") &&
                OutputJson(*resource->Find("tool_choice")) == OutputJson(expected));
    }
  }
  const auto original = output.Snapshot();
  const auto visible = ResponsesWireValue(original);
  Q4T_CHECK(original.Find("tool_choice")->GetString("type") == "local_shell" &&
            OutputJson(*visible.Find("tool_choice")) == OutputJson(expected) &&
            OutputJson(ResponsesWireValue(visible)) == OutputJson(visible));
  const auto& item = output.Snapshot().GetArray("output")->array[0];
  Q4T_CHECK(item.GetString("type") == "local_shell_call" &&
            item.GetString("status") == "completed" &&
            item.GetString("call_id") == "call_0" &&
            item.GetString("id") != item.GetString("call_id"));
  Q4T_CHECK(item.Find("action")->GetArray("command")->array[1].str ==
            "docs/MODEL.md");
  Q4T_CHECK(!branch.Finish(false, 1, {5, 0, 3, 0}, 43, &terminal));
  Q4T_CHECK(branch.Finish(true, 0, {5, 0, 3, 0}, 43, &terminal));
  Q4T_CHECK(branch.Snapshot().GetArray("output")->array[0].GetString(
                "status") == "incomplete");
  return true;
}

Q4T_TEST(responses_client_patch_emits_a_complete_typed_operation) {
  Json options, chat;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"tools":[{"type":"apply_patch"}],"tool_choice":{"type":"apply_patch"}})",
      &options));
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"messages":[{"role":"user","content":"Update draft"}]})", &chat));
  Json tools, choice;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesTools(options, &tools, &choice, &error));
  Json restricted, allowed, mapped_tools, mapped_choice;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"tools":[{"type":"apply_patch","allowed_callers":["programmatic"]}],"tool_choice":{"type":"apply_patch"}})",
      &restricted));
  Q4T_CHECK(!NormalizeResponsesTools(restricted, &mapped_tools, &mapped_choice,
                                     &error));
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"tools":[{"type":"apply_patch"}],"tool_choice":{"type":"allowed_tools","mode":"required","tools":[{"type":"apply_patch"}]}})",
      &allowed));
  Q4T_CHECK(NormalizeResponsesTools(allowed, &mapped_tools, &mapped_choice,
                                    &error));
  chat.object.emplace_back("tools", tools);
  chat.object.emplace_back("tool_choice", choice);
  const std::string prefix =
      "<tool_call><function=q4t_builtin_apply_patch>"
      "<parameter=type>update_file</parameter>"
      "<parameter=path>draft.md</parameter><parameter=diff>@@\n-old\n+new";
  const std::string suffix = "</parameter></function></tool_call>";
  ChatResponseParser parser;
  parser.EmitToolAfterValidation(kApplyPatchToolName);
  Q4T_CHECK(parser.Init(chat, "<think>\n\n</think>\n\n", "resp_patch", false));
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Append(prefix, &deltas) && deltas.empty());
  auto interrupted = parser;
  Q4T_CHECK(interrupted.FinishIncomplete(&deltas) && deltas.empty() &&
            interrupted.CompletedToolCalls() == 0);
  Q4T_CHECK(parser.Append(suffix, &deltas) && deltas.size() == 1 &&
            parser.CompletedToolCalls() == 1);
  auto completed = deltas[0];
  Q4T_CHECK(completed.tool_start && completed.call.name == kApplyPatchToolName);
  ResponsesOutput output;
  std::string initial, added, terminal;
  Q4T_CHECK(output.Init(options, "resp_patch", 42, false) &&
            output.Start(&initial));
  auto rejected = output;
  auto invalid = completed;
  invalid.call.arguments = R"({"type":"update_file","path":"draft.md"})";
  Q4T_CHECK(!rejected.Append(invalid, "null", &added) && added.empty());
  Q4T_CHECK(output.Append(completed, "null", &added));
  auto failed = output;
  Q4T_CHECK(failed.Fail("server_error", "model interrupted", &terminal));
  Q4T_CHECK(failed.Snapshot().GetArray("output")->array[0].GetString(
                "status") == "in_progress" &&
            terminal.find("response.output_item.done") == terminal.npos);
  auto incomplete = output;
  Q4T_CHECK(incomplete.Finish(true, 0, {}, 43, &terminal) &&
            terminal.find("response.output_item.done") == terminal.npos);
  Q4T_CHECK(output.Finish(false, 1, {}, 43, &terminal));
  std::vector<Json> events;
  const auto wire = initial + added + terminal;
  Q4T_CHECK(Events(wire, &events) &&
            wire.find("function_call_arguments") == wire.npos);
  const auto& item = output.Snapshot().GetArray("output")->array[0];
  Q4T_CHECK(item.GetString("type") == "apply_patch_call" &&
            item.GetString("status") == "completed" &&
            item.GetString("call_id") == completed.call.id &&
            item.Find("operation")->GetString("path") == "draft.md" &&
            item.Find("operation")->GetString("diff") == "@@\n-old\n+new");
  return true;
}

Q4T_TEST(responses_shell_stream_keeps_commands_and_completion_distinct) {
  Json options;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"tools":[{"type":"shell","environment":{"type":"local"}}],
          "tool_choice":{"type":"shell"}})",
      &options));
  ResponsesOutput out;
  std::string wire, part;
  Q4T_CHECK(out.Init(options, "resp_shell", 42, false) && out.Start(&wire));
  auto delta = Tool(0, R"({"commands":["pwd","date"],"timeout_ms":700})", true);
  delta.call.name = kShellToolName;
  ResponsesOutput small;
  Q4T_CHECK(small.Init(options, "resp_limit", 42, false, 4096) &&
            small.Start(&part));
  auto many = delta;
  many.call.arguments = R"({"commands":["","","","","","","",""]})";
  Q4T_CHECK(!small.Append(many, "null", &part) && part.empty() &&
            small.Snapshot().GetArray("output")->array.empty());
  auto rejected = out;
  auto invalid = delta;
  invalid.call.arguments = R"({"commands":["pwd",2]})";
  Q4T_CHECK(!rejected.Append(invalid, "null", &part) && part.empty());
  Q4T_CHECK(out.Append(delta, "null", &part));
  wire += part;
  auto interrupted = out;
  Q4T_CHECK(interrupted.Finish(true, 0, {}, 43, &part) &&
            interrupted.Snapshot().GetArray("output")->array[0].GetString(
                "status") == "incomplete" &&
            part.find("shell_call_command.done") == part.npos);
  auto fragmented = out;
  Q4T_CHECK(!fragmented.Append(Tool(0, "{}", false), "null", &part) &&
            part.empty());
  Q4T_CHECK(out.Finish(false, 1, {}, 43, &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events) &&
            wire.find("function_call_arguments") == wire.npos);
  std::vector<std::string> observed;
  const std::vector<std::string> commands = {"pwd", "date"};
  for (const auto& event : events) {
    const auto type = event.GetString("type");
    if (type == "response.output_item.added") {
      const auto* item = event.Find("item");
      Q4T_CHECK(item && item->GetString("type") == "shell_call" &&
                item->Find("action")->GetArray("commands")->array.empty());
    }
    if (!type.starts_with("response.shell_call_command.")) continue;
    const size_t n = event.GetInt("command_index", -1);
    Q4T_CHECK(n < commands.size() && event.GetInt("output_index", -1) == 0 &&
              !event.Has("item_id"));
    const bool added = type.ends_with(".added");
    Q4T_CHECK(event.GetString(type.ends_with(".delta") ? "delta" : "command") ==
              (added ? "" : commands[n]));
    observed.push_back(type.substr(type.rfind('.') + 1) + std::to_string(n));
  }
  const std::vector<std::string> expected = {"added0", "delta0", "added1",
                                             "delta1", "done0",  "done1"};
  Q4T_CHECK(observed == expected);
  const auto& item = out.Snapshot().GetArray("output")->array.front();
  Q4T_CHECK(item.GetString("call_id") == delta.call.id &&
            item.GetString("status") == "completed" &&
            item.Find("environment")->GetString("type") == "local" &&
            item.Find("caller")->GetString("type") == "direct" &&
            item.Find("action")->GetArray("commands")->array.size() == 2 &&
            item.Find("action")->GetInt("timeout_ms") == 700 &&
            item.Find("action")->Find("max_output_length")->IsNull());
  return Save("shell-commands", out, wire);
}
