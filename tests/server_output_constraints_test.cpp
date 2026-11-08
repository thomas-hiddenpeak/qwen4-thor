#include "q4t/server/output_constraints.h"

#include <algorithm>
#include <stdexcept>
#include <string>
#include <vector>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::server::ChatResponseParser;
using q4t::server::OutputConstraints;
using q4t::server::ResponseDelta;
constexpr auto kClosed = "<|im_start|>assistant\n<think>\n\n</think>\n\n";
constexpr auto kThinking = "<|im_start|>assistant\n<think>\n";
constexpr auto kSchema = R"({"type":"object","properties":{
  "path":{"type":"string","enum":["docs/STATUS.md"]},
  "line":{"type":"integer"}},"required":["path","line"],
  "additionalProperties":false})";

Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true, {32, 65536, 1048576, true}))
    throw std::runtime_error("invalid test JSON");
  return value;
}

Json Request(std::string_view options = {}, bool strict = false) {
  return Parse(std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                           R"("enable_thinking":false,"tools":[)"
                           R"({"type":"function","function":{"name":"read",)"
                           R"("strict":)") +
               (strict ? "true" : "false") + R"(,"parameters":)" + kSchema +
               R"(}},)"
               R"({"type":"function","function":{"name":"noop"}}])" +
               (options.empty() ? "" : "," + std::string(options)) + '}');
}

std::string Call(std::string_view name = "read",
                 std::string_view args =
                     "<parameter=path>docs/STATUS.md</parameter>\n"
                     "<parameter=line>1</parameter>") {
  return "<tool_call>\n<function=" + std::string(name) + ">\n" +
         std::string(args) + "\n</function>\n</tool_call>";
}
}  // namespace

Q4T_TEST(output_constraints_error_paths_survive_chat_validation) {
  const std::pair<const char*, const char*> cases[] = {
      {R"("tool_choice":"force")", "tool_choice"},
      {R"("parallel_tool_calls":0)", "parallel_tool_calls"},
      {R"("tool_choice":{"type":"function","function":{"name":"absent"}})",
       "tool_choice.function.name"},
      {R"("tool_choice":{"type":"function","function":{"name":"read","x":1}})",
       "tool_choice.function.x"},
      {R"("response_format":{"type":"json_schema","json_schema":{"name":"x"}})",
       "response_format.json_schema.schema"},
      {R"("tool_choice":"required","session_id":")"
       "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\"",
       "session_id"}};
  for (const auto& [options, expected] : cases) {
    const auto request = Request(options);
    std::string param = "stale";
    OutputConstraints plan;
    const auto direct =
        q4t::server::PlanOutputConstraints(request, &plan, &param);
    Q4T_CHECK(!direct && param == expected);
    param = "stale";
    const auto chat =
        q4t::server::ValidateChatContract(request, "m", false, &param);
    Q4T_CHECK(!chat && param == expected);
    Q4T_CHECK(direct.message() == chat.message());
  }
  std::string param = "stale";
  OutputConstraints plan;
  Q4T_CHECK(q4t::server::PlanOutputConstraints(Request(), &plan, &param));
  Q4T_CHECK(param.empty());
  return true;
}

Q4T_TEST(output_schema_nested_error_paths_and_escaped_keys) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "a.b[0]":{"type":"array","items":{"type":"object","properties":{
      "quote\"\n":{"type":"string","enum":["ok","ok"]}},
      "additionalProperties":false}}},"additionalProperties":false})");
  std::string param = "stale";
  Q4T_CHECK(!q4t::server::ValidateOutputSchema(
      schema, false, false, &param, "response_format.json_schema.schema"));
  Q4T_CHECK(param ==
            "response_format.json_schema.schema.properties"
            "[\"a.b[0]\"].items.properties[\"quote\\\"\\n\"].enum[1]");
  Q4T_CHECK(q4t::server::InputParamPath("x", "") == "x[\"\"]");
  Q4T_CHECK(q4t::server::InputParamPath("x", "0") == "x[\"0\"]");
  Q4T_CHECK(q4t::server::InputParamPath("x", "中文") == "x[\"中文\"]");
  Q4T_CHECK(
      q4t::server::ValidateOutputSchema(Parse(kSchema), true, false, &param));
  Q4T_CHECK(param.empty());
  return true;
}

Q4T_TEST(output_schema_container_and_missing_field_locations) {
  const std::pair<const char*, const char*> cases[] = {
      {R"(null)", "schema"},
      {R"({})", "schema.type"},
      {R"({"type":"object","properties":{}})", "schema.additionalProperties"},
      {R"({"type":"object","properties":{"x":{"type":"string"}},
            "required":["x","x"],"additionalProperties":false})",
       "schema.required[1]"},
      {R"({"type":"object","properties":{"x":{"type":"string"}},
            "additionalProperties":false})",
       "schema.required"},
      {R"({"type":"object","properties":{"x":{"type":"array"}},
            "required":["x"],"additionalProperties":false})",
       "schema.properties.x.items"},
      {R"({"type":"object","properties":{"x":{"type":["bogus","null"]}},
            "required":["x"],"additionalProperties":false})",
       "schema.properties.x.type[0]"}};
  for (const auto& [wire, expected] : cases) {
    std::string param = "stale";
    const auto schema = Parse(wire);
    const auto result =
        q4t::server::ValidateOutputSchema(schema, true, false, &param);
    Q4T_CHECK(!result && param == expected);
    Q4T_CHECK(result.message() ==
              q4t::server::ValidateOutputSchema(schema, true).message());
  }
  return true;
}

Q4T_TEST(output_constraints_tool_choice_plan) {
  OutputConstraints plan;
  for (const auto* options :
       {R"("tool_choice":"none")", R"("tool_choice":"required")",
        R"("tool_choice":{"type":"function","function":{"name":"read"}})",
        R"("tool_choice":"auto","parallel_tool_calls":false)"}) {
    const auto request = Request(options);
    Q4T_CHECK(q4t::server::ValidateChatContract(request, "m", false).ok());
    Q4T_CHECK(q4t::server::PlanOutputConstraints(request, &plan).ok());
    Q4T_CHECK(plan.active && !plan.structural_tag.empty());
    Q4T_CHECK(Parse(plan.structural_tag).GetString("type") == "structural_tag");
  }
  Q4T_CHECK(q4t::server::PlanOutputConstraints(Request(), &plan).ok());
  Q4T_CHECK(!plan.active);
  Q4T_CHECK(q4t::server::PlanOutputConstraints(Request({}, true), &plan).ok());
  Q4T_CHECK(plan.active);
  for (const auto* options :
       {R"("tool_choice":"force")", R"("parallel_tool_calls":0)",
        R"("tool_choice":{"type":"function","function":{"name":"absent"}})",
        R"("tool_choice":{"type":"function","function":{"name":"read","x":1}})",
        R"("tool_choice":"required","parse_response":false)",
        R"("tool_choice":"required","session_id":"x")"})
    Q4T_CHECK(
        !q4t::server::PlanOutputConstraints(Request(options), &plan).ok());
  return true;
}

Q4T_TEST(output_constraints_schema_profile_rejects_silent_downgrade) {
  Q4T_CHECK(q4t::server::ValidateOutputSchema(Parse(kSchema), true).ok());
  for (const auto* extra :
       {R"("$ref":"#/$defs/x")", R"("oneOf":[])", R"("pattern":".*")",
        R"("minimum":0)", R"("minLength":1)", R"("required":["missing"])",
        R"("additionalProperties":true)"}) {
    auto schema = Parse(kSchema);
    const auto patch = Parse("{" + std::string(extra) + "}").object[0];
    std::erase_if(schema.object,
                  [&](const auto& item) { return item.first == patch.first; });
    schema.object.push_back(patch);
    Q4T_CHECK(!q4t::server::ValidateOutputSchema(schema, true).ok());
  }
  auto schema = Parse(kSchema);
  for (auto& [key, value] : schema.object)
    if (key == "required") value.array.clear();
  Q4T_CHECK(!q4t::server::ValidateOutputSchema(schema, true).ok());
  Q4T_CHECK(q4t::server::ValidateOutputSchema(schema, false).ok());
  return true;
}

Q4T_TEST(output_schema_array_bounds_exact_spelling) {
  for (const auto* key : {"minItems", "maxItems"}) {
    for (const auto* value :
         {"1.0000000000000001", "63.9999999999999999", "1.5", "-1", "true"}) {
      auto schema =
          Parse(R"({"type":"object","properties":{"xs":{)"
                R"("type":"array","items":{"type":"string"},")" +
                std::string(key) + "\":" + value +
                R"(}},"required":["xs"],"additionalProperties":false})");
      Q4T_CHECK(!q4t::server::ValidateOutputSchema(schema, true).ok());
    }
  }
  auto schema = Parse(R"({"type":"object","properties":{"xs":{"type":"array",
    "items":{"type":"string"},"minItems":1.0,"maxItems":64e0}},
    "required":["xs"],"additionalProperties":false})");
  Q4T_CHECK(q4t::server::ValidateOutputSchema(schema, true).ok());
  return true;
}

Q4T_TEST(output_constraints_nested_values_and_exact_integers) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "values":{"type":"array","items":{"type":["integer","null"]},
              "minItems":1,"maxItems":2}},"required":["values"],
    "additionalProperties":false})");
  Q4T_CHECK(q4t::server::ValidateOutputSchema(schema, true).ok());
  for (const auto* value :
       {R"({"values":[null,9007199254740993]})", R"({"values":[1.00e2]})"})
    Q4T_CHECK(q4t::server::ValidateOutputValue(schema, Parse(value)).ok());
  for (const auto* value :
       {R"({"values":[1.0000000000000001]})", R"({"values":[1e-1]})",
        R"({"values":[]})", R"({"values":[1,2,3]})", R"({"values":[true]})",
        R"({"values":[1],"extra":1})", R"({})"})
    Q4T_CHECK(!q4t::server::ValidateOutputValue(schema, Parse(value)).ok());
  return true;
}

Q4T_TEST(output_constraints_batch_barrier_and_named_choice) {
  const auto request = Request(R"("tool_choice":"required",
                                 "parallel_tool_calls":false)");
  for (const auto& text : {Call() + "\n" + Call("noop", ""), Call("absent", ""),
                           Call("read", "<parameter=line>1</parameter>")}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
    Q4T_CHECK(!parser.Append(text, &deltas).ok());
    // A preceding valid call may have emitted provisional fragments; a later
    // invalid call must still prevent the whole batch's successful terminal.
    Q4T_CHECK(!parser.Finish(false, &deltas));
  }
  const auto named = Request(
      R"("tool_choice":{"type":"function","function":{"name":"read"}})");
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(named, kClosed, "id", false).ok());
  Q4T_CHECK(!parser.Append(Call("noop", ""), &deltas).ok());
  Q4T_CHECK(deltas.empty());
  return true;
}

Q4T_TEST(output_constraints_all_chunk_splits_and_tool_errors) {
  const auto request = Request(R"("tool_choice":"required")");
  const auto text = Call();
  for (size_t split = 0; split <= text.size(); ++split) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
    Q4T_CHECK(parser.Append(text.substr(0, split), &deltas).ok());
    Q4T_CHECK(parser.Append(text.substr(split), &deltas).ok());
    Q4T_CHECK(!deltas.empty());
    Q4T_CHECK(parser.Finish(false, &deltas).ok());
    size_t calls = 0;
    std::string arguments;
    for (const auto& delta : deltas) {
      if (delta.kind != ResponseDelta::Kind::kTool) continue;
      if (delta.tool_start) {
        ++calls;
        Q4T_CHECK(delta.call.id == "call_id_0" && delta.call.name == "read");
      }
      Q4T_CHECK(delta.call.index == 0);
      arguments += delta.call.arguments;
    }
    Q4T_CHECK(calls == 1);
    Q4T_CHECK(Parse(arguments).GetString("path") == "docs/STATUS.md");
  }
  for (const auto* args :
       {"<parameter=path>other</parameter><parameter=line>1</parameter>",
        "<parameter=path>docs/STATUS.md</parameter><parameter=line>1.5</"
        "parameter>",
        "<parameter=path>docs/STATUS.md</parameter><parameter=line>1</"
        "parameter>"
        "<parameter=extra>ignored</parameter>"}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
    Q4T_CHECK(!parser.Append(Call("read", args), &deltas).ok());
    Q4T_CHECK(deltas.empty());
  }
  return true;
}

Q4T_TEST(output_constraints_none_required_and_length_terminals) {
  for (const auto* choice : {"none", "required"}) {
    const auto request =
        Request("\"tool_choice\":\"" + std::string(choice) + '"');
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
    Q4T_CHECK(parser.Append("ordinary text", &deltas).ok());
    Q4T_CHECK(!deltas.empty() && deltas.front().text == "ordinary text");
    Q4T_CHECK(parser.Finish(false, &deltas).ok() ==
              (std::string(choice) == "none"));
  }
  const auto request = Request(R"("tool_choice":"required")");
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
  Q4T_CHECK(parser.Append(Call(), &deltas).ok());
  Q4T_CHECK(!deltas.empty() && !parser.Finish(true, &deltas));
  const auto none = Request(R"("tool_choice":"none")");
  ChatResponseParser forbidden;
  deltas.clear();
  Q4T_CHECK(forbidden.Init(none, kClosed, "id", false).ok());
  Q4T_CHECK(!forbidden.Append(Call(), &deltas).ok() && deltas.empty());
  return true;
}

Q4T_TEST(output_constraints_json_markers_are_data) {
  const auto request = Parse(R"({"messages":[{"role":"user","content":"q"}],
      "response_format":{"type":"json_object"}})");
  const std::string body =
      R"({"quoted":"<think><tool_call></tool_call>","x":1})";
  for (size_t split = 0; split <= body.size(); ++split) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kThinking, "id", false).ok());
    Q4T_CHECK(parser.Append("reason</think>\n\n", &deltas).ok());
    Q4T_CHECK(parser.Append(body.substr(0, split), &deltas).ok());
    Q4T_CHECK(parser.Append(body.substr(split), &deltas).ok());
    Q4T_CHECK(!deltas.empty() && !parser.HasToolCalls());
    Q4T_CHECK(parser.Finish(false, &deltas).ok());
    const auto message = Parse(parser.MessageJson());
    Q4T_CHECK(message.GetString("reasoning_content") == "reason");
    Q4T_CHECK(Parse(message.GetString("content")).GetInt("x") == 1);
  }
  for (const auto* bad : {R"({"x":1,"x":2})", "[1]", "{", "{} trailing"}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
    Q4T_CHECK(parser.Append(bad, &deltas).ok());
    Q4T_CHECK(!parser.Finish(false, &deltas).ok() && !deltas.empty());
  }
  return true;
}

Q4T_TEST(output_constraints_json_schema_and_combinations) {
  auto request = Parse(
      std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                  R"("response_format":{"type":"json_schema","json_schema":{)"
                  R"("name":"record","strict":true,"schema":)") +
      kSchema + "}}}");
  OutputConstraints plan;
  Q4T_CHECK(q4t::server::PlanOutputConstraints(request, &plan).ok());
  Q4T_CHECK(plan.active && plan.json_output);
  for (const auto* content :
       {R"({"path":"other","line":1})", R"({"path":"docs/STATUS.md"})"}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "id", false).ok());
    Q4T_CHECK(parser.Append(content, &deltas).ok());
    Q4T_CHECK(!parser.Finish(false, &deltas).ok() && !deltas.empty());
  }
  const auto with_tools =
      Request(R"("response_format":{"type":"json_object"})");
  Q4T_CHECK(q4t::server::PlanOutputConstraints(with_tools, &plan).ok());
  Q4T_CHECK(
      q4t::server::PlanOutputConstraints(
          Request(
              R"("tool_choice":"none","response_format":{"type":"json_object"})"),
          &plan)
          .ok());
  return true;
}

Q4T_TEST(output_constraints_template_filters_preserve_request) {
  OutputConstraints plan;
  const auto request = Request(R"("tool_choice":"none")");
  Q4T_CHECK(q4t::server::PlanOutputConstraints(request, &plan).ok());
  const auto filtered = q4t::server::ConstraintTemplateRequest(request, plan);
  Q4T_CHECK(filtered.GetArray("tools")->array.empty());
  Q4T_CHECK(request.GetArray("tools")->array.size() == 2);
  Q4T_CHECK(filtered.GetArray("messages")->array.size() == 1);
  const auto named = Request(
      R"("tool_choice":{"type":"function","function":{"name":"noop"}})");
  Q4T_CHECK(q4t::server::PlanOutputConstraints(named, &plan).ok());
  const auto single = q4t::server::ConstraintTemplateRequest(named, plan);
  Q4T_CHECK(single.GetArray("tools")->array.size() == 1);
  Q4T_CHECK(
      single.GetArray("tools")->array[0].Find("function")->GetString("name") ==
      "noop");
  return true;
}

Q4T_TEST(output_constraints_allowed_tools_filter_template_grammar_and_output) {
  for (const auto* mode : {"auto", "required"}) {
    const auto request = Request(
        std::string(
            R"("tool_choice":{"type":"allowed_tools","allowed_tools":{"mode":")") +
        mode +
        R"(","tools":[{"type":"function","function":{"name":"noop"}}]}})");
    OutputConstraints plan;
    Q4T_CHECK(q4t::server::PlanOutputConstraints(request, &plan));
    Q4T_CHECK(plan.active && plan.restrict_tools && plan.AllowsTool("noop"));
    Q4T_CHECK(!plan.AllowsTool("read"));
    Q4T_CHECK(plan.structural_tag.find("<function=noop>") != std::string::npos);
    Q4T_CHECK(plan.structural_tag.find("<function=read>") == std::string::npos);
    const auto filtered = q4t::server::ConstraintTemplateRequest(request, plan);
    Q4T_CHECK(filtered.GetArray("tools")->array.size() == 1);
    std::string prompt;
    Q4T_CHECK(q4t::server::RenderChatPrompt(filtered, {"q"}, &prompt));
    Q4T_CHECK(prompt.find("\"name\": \"noop\"") != prompt.npos);
    Q4T_CHECK(prompt.find("\"name\": \"read\"") == prompt.npos);
    for (const auto* name : {"noop", "read"}) {
      ChatResponseParser parser;
      std::vector<ResponseDelta> deltas;
      Q4T_CHECK(parser.Init(request, kClosed, "id", false));
      Q4T_CHECK(parser.Append(Call(name, ""), &deltas).ok() ==
                (std::string(name) == "noop"));
      Q4T_CHECK(parser.Finish(false, &deltas).ok() ==
                (std::string(name) == "noop"));
    }
  }
  const auto empty = Request(
      R"("tool_choice":{"type":"allowed_tools","allowed_tools":{"mode":"auto","tools":[]}})");
  OutputConstraints plan;
  Q4T_CHECK(q4t::server::PlanOutputConstraints(empty, &plan));
  Q4T_CHECK(!plan.AllowsTool("read") && !plan.AllowsTool("noop"));
  Q4T_CHECK(q4t::server::ConstraintTemplateRequest(empty, plan)
                .GetArray("tools")
                ->array.empty());
  return true;
}

Q4T_TEST(output_constraints_allowed_tools_errors_are_precise) {
  for (
      const auto& [choice, path] :
      std::vector<std::pair<std::string, std::string>>{
          {R"({"mode":"none","tools":[]})", "mode"},
          {R"({"mode":"required","tools":[]})", "tools"},
          {R"({"mode":"auto","tools":[{"type":"function","function":{"name":"absent"}}]})",
           "tools[0].function.name"},
          {R"({"mode":"auto","tools":[{"type":"function","function":{"name":"read"}},{"type":"function","function":{"name":"read"}}]})",
           "tools[1].function.name"},
          {R"({"mode":"auto","tools":[{"type":"function","function":{"name":"read","extra":1}}]})",
           "tools[0].function.extra"}}) {
    const auto request =
        Request(R"("tool_choice":{"type":"allowed_tools","allowed_tools":)" +
                choice + '}');
    OutputConstraints plan;
    std::string param;
    Q4T_CHECK(!q4t::server::PlanOutputConstraints(request, &plan, &param));
    Q4T_CHECK(param == "tool_choice.allowed_tools." + path);
  }
  return true;
}
