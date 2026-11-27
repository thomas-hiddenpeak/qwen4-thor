#include "q4t/server/responses_output.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/responses_json.h"
#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::responses_detail;
Json Value(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("MCP output fixture");
  return value;
}
Json Options(bool background = false) {
  auto value = Value(R"({"model":"mcp-output-fixture","instructions":null,
    "metadata":{},"temperature":1,"top_p":1,"parallel_tool_calls":true,
    "tools":[],"tool_choice":"auto","text":{"format":{"type":"text"}},
    "reasoning":{},"store":true,"background":false,"truncation":"disabled"})");
  Set(&value, "background", Boolean(background));
  return value;
}
Json Call() {
  return Value(R"({"type":"mcp_call","id":"mcp_stream","server_label":"project",
    "name":"read","arguments":"","approval_request_id":null,
    "status":"in_progress","output":null,"error":null})");
}
Json Catalog() {
  return Value(R"({"type":"mcp_list_tools","id":"mcpl_stream",
    "server_label":"project","tools":[]})");
}
bool Shapes(const ResponsesOutput& output) {
  for (const auto& item : output.Snapshot().GetArray("output")->array) {
    ChatPlanError error;
    const auto status = ValidateResponsesItemShape(
        item, ResponsesItemShape::kOutput, &error, "output");
    if (!status) {
      std::fprintf(stderr, "%s: %s\n", error.param.c_str(),
                   status.message().c_str());
      return false;
    }
  }
  return true;
}
bool Events(const std::string& wire, std::vector<Json>* events) {
  size_t offset = 0;
  while (offset < wire.size()) {
    const auto end = wire.find("\r\n\r\n", offset);
    const auto data = wire.find("\r\ndata: ", offset);
    if (end == wire.npos || data == wire.npos || data > end) return false;
    auto item = Value(wire.substr(data + 8, end - data - 8));
    if (item.GetInt("sequence_number", -1) !=
            static_cast<int64_t>(events->size()) ||
        wire.substr(offset + 7, data - offset - 7) != item.GetString("type"))
      return false;
    events->push_back(std::move(item));
    offset = end + 4;
  }
  return true;
}
bool Save(std::string_view name, const ResponsesOutput& output,
          const std::string& wire) {
  const auto* directory = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!directory) return true;
  const auto root = std::filesystem::path(directory) / "mcp-output";
  std::filesystem::create_directories(root);
  std::ofstream json(root / (std::string(name) + ".json"));
  std::ofstream stream(root / (std::string(name) + ".wire"));
  json << output.Json() << '\n';
  stream << wire;
  return json.good() && stream.good();
}
}  // namespace

Q4T_TEST(responses_mcp_output_catalog_call_deltas_and_answer_lifecycle) {
  ResponsesOutput output;
  std::string wire, part;
  size_t list, call;
  Q4T_CHECK(output.Init(Options(), "resp_mcp", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.StartMcpItem(Catalog(), &list, &part));
  wire += part;
  auto catalog = Catalog();
  Set(&catalog, "tools", Value(R"([{"name":"read","input_schema":{
    "type":"object","properties":{"path":{"type":"string"}}}}])"));
  Q4T_CHECK(output.CompleteMcpItem(list, catalog, &part));
  wire += part;
  Q4T_CHECK(output.StartMcpItem(Call(), &call, &part));
  wire += part;
  for (const auto* delta : {"{\"path\":", "\"docs/MODEL.md\"}"}) {
    Q4T_CHECK(output.AppendMcpArguments(call, delta, &part));
    wire += part;
  }
  Q4T_CHECK(!output.Finish(false, 0, {}, 43, &part));
  Q4T_CHECK(output.CompleteMcpArguments(call, &part));
  wire += part;
  Q4T_CHECK(output.CallingMcp(call));
  auto finished = output.Snapshot().GetArray("output")->array[call];
  Set(&finished, "status", String("completed"));
  Set(&finished, "output", String("Project documentation fixture"));
  Q4T_CHECK(output.CompleteMcpItem(call, finished, &part));
  wire += part;
  ResponseDelta text;
  text.kind = ResponseDelta::Kind::kContent;
  text.text = "The project document was retrieved.";
  Q4T_CHECK(output.Append(text, "null", &part));
  wire += part;
  Q4T_CHECK(output.Finish(false, 0, {20, 0, 12, 0}, 43, &part));
  wire += part;
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events) && Shapes(output));
  std::string args;
  size_t deltas = 0, done = 0, completed = 0;
  for (const auto& event : events) {
    if (event.GetString("type") == "response.mcp_call_arguments.delta") {
      ++deltas;
      args += event.GetString("delta");
      Q4T_CHECK(!done && !completed &&
                event.GetString("item_id") == "mcp_stream");
    } else if (event.GetString("type") == "response.mcp_call_arguments.done") {
      ++done;
      Q4T_CHECK(event.GetString("arguments") == args && !completed);
    } else if (event.GetString("type") == "response.mcp_call.completed") {
      ++completed;
      Q4T_CHECK(done == 1);
    }
  }
  Q4T_CHECK(deltas == 2 && done == 1 && completed == 1);
  Q4T_CHECK(Save("response", output, wire));
  return true;
}

Q4T_TEST(responses_mcp_output_approval_is_complete_without_fabricated_call) {
  ResponsesOutput output;
  std::string wire, part;
  size_t index;
  Q4T_CHECK(output.Init(Options(), "resp_approval", 42, false));
  Q4T_CHECK(output.Start(&wire));
  const auto approval = Value(R"({"type":"mcp_approval_request","id":"mcpr_x",
    "server_label":"project","name":"read","arguments":"{\"path\":\"docs/MODEL.md\"}"})");
  Q4T_CHECK(output.StartMcpItem(approval, &index, &part));
  wire += part;
  Q4T_CHECK(!output.CallingMcp(index));
  Q4T_CHECK(!output.StartMcpItem(approval, &index, &part));
  Q4T_CHECK(output.Finish(false, 0, {10, 0, 4, 0}, 43, &part));
  wire += part;
  Q4T_CHECK(Shapes(output) &&
            output.Snapshot().GetArray("output")->array.size() == 1);
  Q4T_CHECK(!output.Snapshot().GetArray("output")->array[0].Has("status"));
  Q4T_CHECK(wire.find("response.mcp_call") == wire.npos);
  Q4T_CHECK(Save("approval", output, wire));
  return true;
}

Q4T_TEST(responses_mcp_output_native_error_data_matches_failure_and_dispatch) {
  detail::HttpResponseContext context(detail::HttpResponseContext::Detached{});
  context.SetResponsesView(PythonResponsesView::kStringMcpError);
  ResponsesOutput output;
  std::string wire, part;
  size_t call;
  Q4T_CHECK(output.Init(Options(), "resp_native_error", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.StartMcpItem(Call(), &call, &part));
  wire += part;
  Q4T_CHECK(output.AppendMcpArguments(call, "{}", &part));
  wire += part;
  Q4T_CHECK(output.CompleteMcpArguments(call, &part));
  wire += part;
  auto failed = output.Snapshot().GetArray("output")->array[call];
  const auto native = Value(R"({"content":[{"type":"text","text":"missing"}],
    "structuredContent":{"reason":"missing"},"isError":true})");
  Set(&failed, "status", String("failed"));
  Set(&failed, "output", String(OutputJson(native)));
  Set(&failed, "error",
      Object({{"type", String("mcp_tool_execution_error")},
              {"content", *native.Find("content")}}));
  Q4T_CHECK(!output.CompleteMcpItem(call, failed, &part));
  Q4T_CHECK(output.CallingMcp(call));
  for (const int variant : {0, 1, 2}) {
    auto forged = failed;
    if (!variant) {
      Set(&Field(forged, "error"), "content", Array());
    } else if (variant == 1) {
      auto success = native;
      Set(&success, "isError", Boolean(false));
      Set(&forged, "output", String(OutputJson(success)));
    } else {
      Set(&forged, "error",
          Value(R"({"type":"http_error","code":503,"message":"down"})"));
    }
    Q4T_CHECK(!output.CompleteMcpItem(call, forged, &part));
  }
  Q4T_CHECK(output.CompleteMcpItem(call, failed, &part));
  wire += part;
  Q4T_CHECK(output.Finish(false, 0, {}, 43, &part));
  wire += part;
  Q4T_CHECK(Shapes(output));
  Q4T_CHECK(wire.find("response.mcp_call.failed") != wire.npos);
  Q4T_CHECK(output.Snapshot().GetArray("output")->array[call].GetString(
                "output") == OutputJson(native));
  ObjectStore objects({});
  ResponsesExecution execution(objects);
  const std::string error = OutputJson(*failed.Find("error"));
  const auto projected = execution.PublicWire(wire);
  Q4T_CHECK(wire != projected);
  std::vector<Json> canonical, legacy;
  Q4T_CHECK(Events(wire, &canonical) && Events(projected, &legacy));
  Q4T_CHECK(canonical.size() == legacy.size());
  size_t changed = 0;
  for (size_t i = 0; i < canonical.size(); ++i) {
    if (OutputJson(canonical[i]) == OutputJson(legacy[i])) continue;
    auto& event = legacy[i];
    auto& item = event.Has("item") ? Field(event, "item")
                                   : Field(Field(event, "response"),
                                           "output").array.at(call);
    Q4T_CHECK(item.GetString("error") == error);
    Set(&item, "error", Value(item.GetString("error")));
    Q4T_CHECK(OutputJson(event) == OutputJson(canonical[i]));
    ++changed;
  }
  Q4T_CHECK(changed == 2);
  Q4T_CHECK(Value(output.Json()).GetArray("output")->array.at(call)
                .GetString("error") == error);
  context.SetResponsesView(PythonResponsesView::kCurrent);
  Q4T_CHECK(execution.PublicWire(wire) == wire);
  Q4T_CHECK(Value(output.Json()).GetArray("output")->array.at(call)
                .Find("error")->IsObject());
  Q4T_CHECK(Save("native-error", output, wire));
  return true;
}

Q4T_TEST(responses_mcp_output_truncation_never_completes_arguments_or_call) {
  ResponsesOutput output;
  std::string wire, part;
  size_t call, list;
  Q4T_CHECK(output.Init(Options(), "resp_partial", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.StartMcpItem(Call(), &call, &part));
  wire += part;
  Q4T_CHECK(output.AppendMcpArguments(call, "{\"path\":", &part));
  wire += part;
  Q4T_CHECK(!output.CompleteMcpArguments(call, &part));
  Q4T_CHECK(!output.CallingMcp(call));
  Q4T_CHECK(output.StartMcpItem(Catalog(), &list, &part));
  wire += part;
  Q4T_CHECK(!output.Finish(false, 0, {}, 43, &part));
  Q4T_CHECK(output.Finish(true, 0, {}, 43, &part));
  wire += part;
  Q4T_CHECK(wire.find("response.mcp_call_arguments.done") == wire.npos &&
            wire.find("response.mcp_call.completed") == wire.npos &&
            wire.find("response.mcp_list_tools.failed") != wire.npos);
  Q4T_CHECK(output.Snapshot().GetArray("output")->array[call].GetString(
                "status") == "incomplete");
  Q4T_CHECK(Shapes(output));
  return true;
}

Q4T_TEST(responses_mcp_output_fail_and_cancel_preserve_sdk_item_shapes) {
  for (bool cancel : {false, true}) {
    ResponsesOutput output;
    std::string wire, part;
    size_t call, list;
    Q4T_CHECK(output.Init(Options(cancel), "resp_fault", 42, false));
    Q4T_CHECK(output.Start(&wire));
    if (cancel) Q4T_CHECK(output.StartRunning(&part));
    Q4T_CHECK(output.StartMcpItem(Catalog(), &list, &part));
    Q4T_CHECK(output.StartMcpItem(Call(), &call, &part));
    if (cancel) {
      Q4T_CHECK(output.Cancel());
    } else {
      Q4T_CHECK(output.Fail("request_cancelled", "Cancelled", &part));
      Q4T_CHECK(part.find("output_item.done") == part.npos &&
                part.find("arguments.done") == part.npos);
    }
    Q4T_CHECK(Shapes(output));
    Q4T_CHECK(output.Snapshot().GetArray("output")->array[call].GetString(
                  "status") == "incomplete");
    Q4T_CHECK(!output.Snapshot().GetArray("output")->array[list].Has("status"));
    Q4T_CHECK(!output.CallingMcp(call));
  }
  return true;
}

Q4T_TEST(responses_mcp_output_rejects_identity_changes_and_capacity) {
  ResponsesOutput output;
  std::string wire;
  size_t call;
  Q4T_CHECK(output.Init(Options(), "resp_identity", 42, false, 8192));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.StartMcpItem(Call(), &call, &wire));
  const auto before = output.Json();
  Q4T_CHECK(!output.AppendMcpArguments(call, std::string(8192, 'x'), &wire));
  Q4T_CHECK(output.Json() == before && wire.empty());
  Q4T_CHECK(output.AppendMcpArguments(call, "{}", &wire));
  Q4T_CHECK(output.CompleteMcpArguments(call, &wire));
  Q4T_CHECK(!output.CompleteMcpArguments(call, &wire));
  Q4T_CHECK(!output.AppendMcpArguments(call, " ", &wire));
  auto final = output.Snapshot().GetArray("output")->array[call];
  Set(&final, "status", String("completed"));
  Set(&final, "output", String("ok"));
  Q4T_CHECK(!output.CompleteMcpItem(call, final, &wire));
  Q4T_CHECK(output.CallingMcp(call));
  for (const auto* field :
       {"id", "name", "server_label", "arguments", "approval_request_id"}) {
    auto changed = final;
    Set(&changed, field, String("altered"));
    Q4T_CHECK(!output.CompleteMcpItem(call, changed, &wire));
  }
  Q4T_CHECK(output.CompleteMcpItem(call, final, &wire));
  Q4T_CHECK(!output.CompleteMcpItem(call, final, &wire));
  return true;
}

Q4T_TEST(responses_mcp_output_tool_and_listing_errors_close_exactly_once) {
  ResponsesOutput output;
  std::string wire, part;
  size_t list, call;
  Q4T_CHECK(output.Init(Options(), "resp_tool_errors", 42, false));
  Q4T_CHECK(output.Start(&wire));
  Q4T_CHECK(output.StartMcpItem(Catalog(), &list, &part));
  wire += part;
  auto failed = Catalog();
  Set(&failed, "error", String("MCP target unavailable"));
  Q4T_CHECK(output.CompleteMcpItem(list, failed, &part));
  wire += part;
  Q4T_CHECK(output.StartMcpItem(Call(), &call, &part));
  wire += part;
  Q4T_CHECK(output.AppendMcpArguments(call, "{}", &part));
  wire += part;
  Q4T_CHECK(output.CompleteMcpArguments(call, &part));
  wire += part;
  failed = output.Snapshot().GetArray("output")->array[call];
  Set(&failed, "status", String("failed"));
  Set(&failed, "error",
      Value(
          R"({"type":"http_error","code":401,"message":"Authentication required"})"));
  Q4T_CHECK(output.CompleteMcpItem(call, failed, &part));
  wire += part;
  Q4T_CHECK(output.Finish(false, 0, {}, 43, &part));
  wire += part;
  Q4T_CHECK(Shapes(output));
  Q4T_CHECK(output.Snapshot().GetArray("output")->array[call].GetString(
                "status") == "failed");
  std::vector<Json> events;
  Q4T_CHECK(Events(wire, &events));
  size_t done = 0;
  for (const auto& event : events)
    done += event.GetString("type") == "response.output_item.done";
  Q4T_CHECK(done == 2 && wire.find("response.mcp_call.completed") == wire.npos);
  return true;
}
