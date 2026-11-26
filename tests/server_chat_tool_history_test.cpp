#include "q4t/server/chat_tool_history.h"

#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/refusal_frame.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/request_json.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::server::PlanToolResultOrder;

const std::string kUser = R"({"role":"user","content":"q"})";
const std::string kPair = R"({"role":"assistant","content":"","tool_calls":[
  {"id":"a","type":"function","function":{"name":"first","arguments":{}}},
  {"id":"b","type":"function","function":{"name":"second","arguments":"{}"}}
]})";
const std::string kLegacy = R"({"role":"assistant","tool_calls":[
  {"type":"function","function":{"name":"lookup","arguments":{"n":1}}}
]})";

std::string List(std::initializer_list<std::string> messages) {
  std::string out = "[";
  for (const auto& message : messages) {
    if (out.size() != 1) out += ',';
    out += message;
  }
  return out + ']';
}

std::string Call(const std::string& id, const std::string& name = "lookup") {
  return R"({"role":"assistant","tool_calls":[{"id":")" + id +
         R"(","type":"function","function":{"name":")" + name +
         R"(","arguments":{}}}]})";
}

std::string Result(const std::string& id, const std::string& text = "value") {
  return R"({"role":"tool","tool_call_id":")" + id + R"(","content":")" + text +
         R"("})";
}

bool ParseRequest(const std::string& messages, Json* request) {
  return q4t::io::ParseJson(
             R"({"messages":)" + messages +
                 R"(,"chat_template_kwargs":{"enable_thinking":false}})",
             request, true)
      .ok();
}

// A malformed JSON fixture must not masquerade as an input-contract rejection.
bool Reject(const std::string& messages) {
  Json parsed;
  return q4t::io::ParseJson(messages, &parsed).ok() &&
         !PlanToolResultOrder(parsed).ok();
}
}  // namespace

Q4T_TEST(chat_tool_history_json_adapter_preserves_refs_null_and_exact_values) {
  using namespace q4t::server;
  Json request;
  Q4T_CHECK(ParseRequest(
      List({kUser,
            R"({"role":"assistant","content":null,"tool_calls":[{"id":"a",
      "type":"function","function":{"name":"lookup","arguments":
      "{\"text\":\"null</parameter>中\",\"empty\":null,\"amount\":1e400}"}}]})",
            Result("a"), kUser}),
      &request));
  Json tools;
  Q4T_CHECK(
      q4t::io::ParseJson(R"([{"type":"function","function":{"name":"lookup",
    "parameters":{"type":"object","properties":{
      "text":{"$ref":"#/$defs/value"},"empty":{"$ref":"#/$defs/value"},
      "amount":{"type":"number"}},"required":["text","empty","amount"],
      "additionalProperties":false,"$defs":{"value":{"type":["string","null"]}}}}}])",
                         &tools, true));
  request.object.emplace_back("tools", tools);
  const auto original = OutputJson(request);
  Q4T_CHECK(PlanToolResultOrder(*request.GetArray("messages")));
  std::string prompt;
  Q4T_CHECK(RenderChatPrompt(request, {"q", "", "value", "q"}, &prompt));
  Q4T_CHECK(OutputJson(request) == original);
  const auto begin = prompt.find("<tools>\n") + 8;
  const auto end = prompt.find("\n</tools>", begin);
  Q4T_CHECK(begin != 7 && end != prompt.npos);
  Json model_tool;
  Q4T_CHECK(q4t::io::ParseJson(prompt.substr(begin, end - begin), &model_tool,
                               true, kSchemaJsonLimits));
  const auto* outer = model_tool.Find("function")->Find("parameters");
  const auto* nested = outer->Find("properties")->Find("arguments");
  Q4T_CHECK(nested->Find("properties")->Find("text")->GetString("$ref") ==
            "#/properties/arguments/$defs/value");
  OutputSchemaGraph graph;
  Q4T_CHECK(graph.Init(*outer, true, nullptr));
  Json expected;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"arguments":{"text":"null</parameter>中","empty":null,"amount":1e400}})",
      &expected, true, kSchemaJsonLimits));
  Q4T_CHECK(graph.Check(expected));
  Q4T_CHECK(prompt.find("<parameter=arguments>\n" +
                        EncodeSchemaJson(*expected.Find("arguments")) +
                        "\n</parameter>\n</function>") != prompt.npos);
  Q4T_CHECK(prompt.find("null</parameter>中") == prompt.npos);
  return true;
}

Q4T_TEST(chat_tool_map_references_preserve_the_declared_value_contract) {
  using namespace q4t::server;
  Json tools;
  Q4T_CHECK(q4t::io::ParseJson(R"([{"type":"function","function":{
    "name":"lookup_release","strict":false,"parameters":{
      "type":"object","properties":{"versions":{"type":"object",
        "additionalProperties":{"$ref":"#/$defs/revision"}}},
      "required":["versions"],"additionalProperties":false,
      "$defs":{"revision":{"type":"integer","minimum":1}},
      "examples":[{"versions":{"$ref":"#/$defs/this-is-data"}}]}}}])",
      &tools, true));
  const auto& original = *tools.array[0].Find("function")->Find("parameters");
  const auto original_bytes = OutputJson(original);
  std::string definitions;
  Q4T_CHECK(RenderChatToolDefinitions(tools, &definitions));
  const auto begin = definitions.find("<tools>\n");
  const auto end = definitions.find("\n</tools>", begin);
  Q4T_CHECK(begin != definitions.npos && end != definitions.npos);
  Json tool;
  Q4T_CHECK(q4t::io::ParseJson(
      definitions.substr(begin + 8, end - begin - 8), &tool, true));
  const auto& wrapper = *tool.Find("function")->Find("parameters");
  const auto& nested = *wrapper.Find("properties")->Find("arguments");
  Q4T_CHECK(OutputJson(*nested.Find("examples")) ==
            OutputJson(*original.Find("examples")));
  Q4T_CHECK(OutputJson(original) == original_bytes);

  OutputSchemaGraph client, model;
  Q4T_CHECK(client.Init(original, false, nullptr, "schema",
                        OutputSchemaPurpose::kToolResult));
  Q4T_CHECK(model.Init(wrapper, false, nullptr, "schema",
                       OutputSchemaPurpose::kToolResult));
  for (const auto& [wire, valid] :
       {std::pair{R"({"versions":{"main":532,"light":534}})", true},
        {R"({"versions":{}})", true},
        {R"({"versions":{"main":"532"}})", false},
        {R"({"versions":{"light":0}})", false},
        {R"({"versions":{},"extra":1})", false}}) {
    Json arguments;
    Q4T_CHECK(q4t::io::ParseJson(wire, &arguments, true));
    Q4T_CHECK(client.Check(arguments).ok() == valid);
    Json framed;
    framed.type = Json::Type::kObject;
    framed.object.emplace_back("arguments", std::move(arguments));
    Q4T_CHECK(model.Check(framed).ok() == valid);
  }
  return true;
}

Q4T_TEST(chat_tool_history_native_values_keep_exact_decimal_spelling) {
  using q4t::server::RenderChatPrompt;
  Json request;
  Q4T_CHECK(ParseRequest(
      List({kUser,
            R"({"role":"assistant","tool_calls":[{"id":"a","type":"function",
      "function":{"name":"lookup","arguments":
      "{\"n\":1.0000000000000001,\"xs\":[0.30000000000000001]}"}}]})",
            Result("a"), kUser}),
      &request));
  std::string prompt;
  Q4T_CHECK(RenderChatPrompt(request, {"q", "", "value", "q"}, &prompt));
  Q4T_CHECK(prompt.find("<parameter=n>\n1.0000000000000001\n</parameter>") !=
            prompt.npos);
  Q4T_CHECK(
      prompt.find("<parameter=xs>\n[0.30000000000000001]\n</parameter>") !=
      prompt.npos);
  return true;
}

Q4T_TEST(chat_tool_history_arbitrary_json_keys_are_data_without_definitions) {
  using namespace q4t::server;
  Json request;
  Q4T_CHECK(ParseRequest(
      List({kUser,
            R"({"role":"assistant","content":null,"tool_calls":[{"id":"a",
      "type":"function","function":{"name":"lookup","arguments":{
      "":"empty","a</parameter>":"value","quote\"\n":[1,2],"bad>key":1}}}]})",
            Result("a")}),
      &request));
  Q4T_CHECK(PlanToolResultOrder(*request.GetArray("messages")));
  std::string prompt;
  Q4T_CHECK(RenderChatPrompt(request, {"q", "", "value"}, &prompt));
  Q4T_CHECK(prompt.find("<parameter=arguments>\n") != prompt.npos);
  Q4T_CHECK(prompt.find("<parameter=a</parameter>") == prompt.npos);
  const auto* arguments = request.GetArray("messages")
                              ->array[1]
                              .GetArray("tool_calls")
                              ->array[0]
                              .Find("function")
                              ->Find("arguments");
  Q4T_CHECK(prompt.find(EncodeSchemaJson(*arguments)) != prompt.npos);
  return true;
}

Q4T_TEST(chat_tool_history_identified_order) {
  Json messages;
  Q4T_CHECK(q4t::io::ParseJson(
                List({kUser, kPair, Result("b", "B"), Result("a", "A"), kUser}),
                &messages)
                .ok());
  std::vector<size_t> order;
  Q4T_CHECK(PlanToolResultOrder(messages, &order).ok());
  const std::vector<size_t> expected{0, 1, 3, 2, 4};
  Q4T_CHECK(order == expected);
  Q4T_CHECK(PlanToolResultOrder(messages).ok());
  // Planning never mutates the caller's JSON or the original result order.
  Q4T_CHECK(messages.array[2].GetString("tool_call_id") == "b");
  Q4T_CHECK(messages.array[2].GetString("content") == "B");
  return true;
}

Q4T_TEST(chat_tool_history_missing_duplicate_orphan) {
  for (const auto& history :
       {List({kUser, kPair}),
        List({kUser, kPair, Result("a"), kUser, Result("b")}),
        List({kUser, kPair, Result("a"), Result("a")}),
        List({kUser, kPair, Result("a"), Result("other")}),
        List({kUser, Result("a")}),
        List({kUser, kPair, Result("a"), Result("b"), Result("a")}),
        List({kUser, kPair, Result("a"),
              R"({"role":"tool","content":"missing id"})"}),
        List({kUser, kLegacy, Result("a")})}) {
    Q4T_CHECK(Reject(history));
  }
  Json messages;
  Q4T_CHECK(q4t::io::ParseJson(List({kUser, kPair}), &messages).ok());
  std::vector<size_t> order{9, 8, 7};
  Q4T_CHECK(!PlanToolResultOrder(messages, &order).ok());
  Q4T_CHECK(order.empty());
  return true;
}

Q4T_TEST(chat_tool_history_identified_shapes) {
  for (const char* invalid :
       {R"({"role":"assistant","tool_calls":[{"id":"a","name":"lookup"}]})",
        R"({"role":"assistant","tool_calls":[{"id":null,"type":"function",
           "function":{"name":"lookup"}}]})",
        R"({"role":"assistant","tool_calls":[{"id":"a","type":"custom",
           "function":{"name":"lookup"}}]})",
        R"({"role":"assistant","tool_calls":[{"id":"a","type":"function",
           "function":{"name":"lookup"}},{"type":"function",
           "function":{"name":"second"}}]})",
        R"({"role":"assistant","tool_calls":[{"id":"a","type":"function",
           "function":{"name":"lookup"}},{"id":"a","type":"function",
           "function":{"name":"second"}}]})"}) {
    Q4T_CHECK(Reject(List({kUser, invalid, Result("a")})));
  }
  Q4T_CHECK(Reject(List({kUser, Call(""), Result("")})));
  Q4T_CHECK(Reject(List({kUser, Call("a", "bad>name"), Result("a")})));
  Q4T_CHECK(Reject(
      List({kUser, R"({"role":"user","tool_call_id":"a","content":"x"})"})));
  Q4T_CHECK(
      Reject(List({kUser, Call("a"),
                   R"({"role":"tool","tool_call_id":false,"content":"x"})"})));
  return true;
}

Q4T_TEST(chat_tool_history_legacy_compatibility) {
  Json messages;
  const auto history =
      List({kUser, kLegacy, R"({"role":"tool","content":"one"})",
            R"({"role":"tool","content":"two"})", kUser, kPair, Result("b"),
            Result("a")});
  Q4T_CHECK(q4t::io::ParseJson(history, &messages).ok());
  std::vector<size_t> order;
  Q4T_CHECK(PlanToolResultOrder(messages, &order).ok());
  const std::vector<size_t> expected{0, 1, 2, 3, 4, 5, 7, 6};
  Q4T_CHECK(order == expected);
  // The legacy route retains native template behavior, not ID guarantees.
  Q4T_CHECK(
      q4t::io::ParseJson(List({kUser, R"({"role":"tool","content":"legacy"})"}),
                         &messages)
          .ok());
  Q4T_CHECK(PlanToolResultOrder(messages).ok());
  return true;
}

Q4T_TEST(chat_tool_history_batch_boundaries) {
  Json messages;
  Q4T_CHECK(
      q4t::io::ParseJson(List({kUser, Call("a"), Result("a"), kUser,
                               Call("a", "different"), Result("a", "new")}),
                         &messages)
          .ok());
  Q4T_CHECK(PlanToolResultOrder(messages).ok());
  Q4T_CHECK(Reject(
      List({kUser, Call("a"), Result("a"), kUser, Call("b"), Result("a")})));
  Q4T_CHECK(
      Reject(List({kUser, Call("a"), Call("b"), Result("a"), Result("b")})));
  Q4T_CHECK(q4t::io::ParseJson(List({kUser, kPair, Result("b"), Result("a"),
                                     kUser, kPair, Result("b"), Result("a")}),
                               &messages)
                .ok());
  std::vector<size_t> order;
  Q4T_CHECK(PlanToolResultOrder(messages, &order).ok());
  const std::vector<size_t> expected{0, 1, 3, 2, 4, 5, 7, 6};
  Q4T_CHECK(order == expected);
  return true;
}

Q4T_TEST(chat_tool_history_bounds) {
  Json messages;
  const std::string id(256, 'a');
  const std::string name(256, 'n');
  Q4T_CHECK(
      q4t::io::ParseJson(List({kUser, Call(id, name), Result(id)}), &messages)
          .ok());
  Q4T_CHECK(PlanToolResultOrder(messages).ok());
  Q4T_CHECK(Reject(List({kUser, Call(id + 'a'), Result(id + 'a')})));
  Q4T_CHECK(Reject(List({kUser, Call(id, name + 'n'), Result(id)})));
  for (int count : {128, 129}) {
    std::string history =
        '[' + kUser + R"(,{"role":"assistant","tool_calls":[)";
    for (int k = 0; k < count; ++k) {
      if (k) history += ',';
      history +=
          R"({"id":")" + std::to_string(k) +
          R"(","type":"function","function":{"name":"f","arguments":{}}})";
    }
    history += "]}";
    for (int k = count - 1; k >= 0; --k)
      history += ',' + Result(std::to_string(k));
    history += ']';
    Q4T_CHECK(q4t::io::ParseJson(history, &messages).ok());
    std::vector<size_t> order;
    const auto status = PlanToolResultOrder(messages, &order);
    Q4T_CHECK(status.ok() == (count == 128));
    if (status) {
      Q4T_CHECK(order.size() == 130 && order[2] == 129 && order[129] == 2);
    } else {
      Q4T_CHECK(order.empty());
    }
  }
  return true;
}

Q4T_TEST(chat_template_tool_result_order) {
  Json request;
  Q4T_CHECK(ParseRequest(
      List({kUser, kPair, Result("b", "B"), Result("a", "A")}), &request));
  const std::vector<std::string> contents{"q", "", "B", "A"};
  std::string prompt;
  Q4T_CHECK(q4t::server::RenderChatPrompt(request, contents, &prompt).ok());
  const std::string expected =
      "<|im_start|>system\n" + std::string(q4t::server::kRefusalInstruction) +
      "<|im_end|>\n"
      "<|im_start|>user\nq<|im_end|>\n"
      "<|im_start|>assistant\n<think>\n\n</think>\n\n"
      "<tool_call>\n<function=first>\n</function>\n</tool_call>\n"
      "<tool_call>\n<function=second>\n</function>\n</tool_call><|im_end|>\n"
      "<|im_start|>user\n<tool_response>\nA\n</tool_response>\n"
      "<tool_response>\nB\n</tool_response><|im_end|>\n"
      "<|im_start|>assistant\n<think>\n\n</think>\n\n";
  Q4T_CHECK(prompt == expected);
  Json ordered;
  Q4T_CHECK(ParseRequest(
      List({kUser, kPair, Result("a", "A"), Result("b", "B")}), &ordered));
  Q4T_CHECK(q4t::server::RenderChatPrompt(ordered, {"q", "", "A", "B"}, &prompt)
                .ok());
  Q4T_CHECK(prompt == expected);
  // Historical functions need not still appear in the current tool registry.
  Q4T_CHECK(
      q4t::server::ValidateChatContract(request, "qwen3.8-flash-next", false)
          .ok());
  return true;
}

Q4T_TEST(chat_contract_tool_history_gate) {
  Json request;
  Q4T_CHECK(ParseRequest(List({kUser, Call("a"), Result("other")}), &request));
  Q4T_CHECK(
      !q4t::server::ValidateChatContract(request, "qwen3.8-flash-next", false)
           .ok());
  std::string prompt = "must be cleared on failure";
  Q4T_CHECK(
      !q4t::server::RenderChatPrompt(request, {"q", "", "bad"}, &prompt).ok());
  Q4T_CHECK(prompt.empty());
  Json raw;
  Q4T_CHECK(
      q4t::io::ParseJson(R"({"prompt":"<tool_call>verbatim"})", &raw).ok());
  Q4T_CHECK(
      q4t::server::ValidateChatContract(raw, "qwen3.8-flash-next", false).ok());
  return true;
}

Q4T_TEST(chat_tool_history_media_order_guard) {
  for (const auto& part :
       {R"({"type":"image_url","image_url":{"url":"not-decoded"}})",
        R"({"video_frames":["not-decoded"]})",
        R"({"text":"also text","image_url":{"url":"not-decoded"}})"}) {
    const auto result = R"({"role":"tool","tool_call_id":"b","content":[)" +
                        std::string(part) + "]}";
    Json request;
    Q4T_CHECK(
        ParseRequest(List({kUser, kPair, result, Result("a")}), &request));
    // Media is user-only even with experimental allow_media. Both ordered and
    // reordered tool results must reject it before template/decoder work.
    Q4T_CHECK(
        !q4t::server::ValidateChatContract(request, "qwen3.8-flash-next", true)
             .ok());
    Q4T_CHECK(
        ParseRequest(List({kUser, kPair, Result("a"), result}), &request));
    Q4T_CHECK(
        !q4t::server::ValidateChatContract(request, "qwen3.8-flash-next", true)
            .ok());
  }
  Json request;
  Q4T_CHECK(ParseRequest(List({kUser, kPair,
                               R"({"role":"tool","tool_call_id":"b","content":[
           {"type":"text","text":"B"}]})",
                               Result("a", "A")}),
                         &request));
  Q4T_CHECK(
      q4t::server::ValidateChatContract(request, "qwen3.8-flash-next", false)
          .ok());
  return true;
}

Q4T_TEST(chat_tool_history_no_silent_argument_loss) {
  for (const auto* fields :
       {R"("name":"lookup")", R"("name":"lookup","argumnts":{})",
        R"("name":"lookup","arguments":null)",
        R"("name":"lookup","arguments":[])",
        R"("name":"lookup","arguments":{},"extra":1)"}) {
    for (const bool ids : {false, true}) {
      const auto call = std::string(R"({"role":"assistant","tool_calls":[{)") +
          (ids ? R"("id":"a",)" : "") +
          R"("type":"function","function":{)" + fields + "}}]}";
      const auto history = List({kUser, call, ids ? Result("a") :
          R"({"role":"tool","content":"result"})"});
      Q4T_CHECK(Reject(history));
      Json request;
      Q4T_CHECK(ParseRequest(history, &request));
      Q4T_CHECK(!q4t::server::ValidateChatContract(request, "m", false).ok());
      std::string prompt;
      Q4T_CHECK(!q4t::server::RenderChatPrompt(
          request, {"q", "", "result"}, &prompt).ok());
    }
  }
  for (const auto* content : {"", R"(,"content":null)", R"(,"content":1)"}) {
    Q4T_CHECK(Reject(List({kUser, Call("a"),
                           std::string(R"({"role":"tool","tool_call_id":"a")") +
                               content + '}'})));
    Q4T_CHECK(Reject(List(
        {kUser, kLegacy, std::string(R"({"role":"tool")") + content + '}'})));
  }
  return true;
}

Q4T_TEST(chat_tool_history_preserves_types_in_flat_parameters) {
  using namespace q4t::server;
  const auto render = [](const std::string& value, const std::string& schema,
                         std::string* prompt) {
    Json args;
    if (!q4t::io::ParseJson("{\"value\":" + value + "}", &args, true))
      return false;
    const auto arguments = OutputJson(args);
    Json encoded;
    encoded.type = Json::Type::kString;
    encoded.str = arguments;
    const auto call =
        R"({"role":"assistant","tool_calls":[{"id":"a","type":"function",)"
        R"("function":{"name":"lookup","arguments":)" +
        OutputJson(encoded) + "}}]}";
    Json request;
    if (!ParseRequest(List({kUser, call, Result("a")}), &request)) return false;
    if (!schema.empty()) {
      Json tools;
      if (!q4t::io::ParseJson(
              R"([{"type":"function","function":{"name":"lookup","parameters":)"
              R"({"type":"object","properties":{"value":{"type":)" +
                  schema +
                  R"(}},"required":["value"],"additionalProperties":false}}}])",
              &tools, true))
        return false;
      request.object.emplace_back("tools", std::move(tools));
    }
    if (!ValidateChatContract(request, "m", false) ||
        !RenderChatPrompt(request, {"q", "", "value"}, prompt))
      return false;
    const std::string marker =
        "[Historical function call arguments as received]\n";
    const auto offset = prompt->find(marker);
    if (offset != prompt->npos) {
      const auto start = offset + marker.size();
      const auto end = prompt->find("<|im_end|>", start);
      Json record;
      if (end == prompt->npos ||
          !q4t::io::ParseJson(prompt->substr(start, end - start), &record) ||
          record.GetString("name") != "lookup" ||
          record.GetString("arguments") != arguments)
        return false;
    }
    return true;
  };
  for (const auto& [typed, text] :
       std::vector<std::pair<std::string, std::string>>{
           {"null", R"("null")"},
           {"true", R"("true")"},
           {"10", R"("10")"},
           {"[1]", R"("[1]")"},
           {R"({"x":1})", R"("{\"x\": 1}")"}}) {
    for (const std::string schema :
         {"", R"("string")", R"(["string","null"])"}) {
      std::string typed_prompt, string_prompt;
      Q4T_CHECK(render(typed, schema, &typed_prompt));
      Q4T_CHECK(render(text, schema, &string_prompt));
      Q4T_CHECK(typed_prompt != string_prompt);
      if (schema == R"("string")")
        Q4T_CHECK(string_prompt.find("<parameter=value>\n") !=
                  string_prompt.npos);
    }
  }
  std::string ordinary;
  Q4T_CHECK(render(R"("release candidate")", "", &ordinary));
  Q4T_CHECK(
      ordinary.find("<parameter=value>\nrelease candidate\n</parameter>") !=
      ordinary.npos);
  return true;
}

Q4T_TEST(chat_tool_history_preserves_rejected_argument_text_for_repair) {
  const auto feedback = Result("a", "Please supply an integer.");
  for (const std::string arguments :
       {R"("")", R"("[]")", R"("0")", R"("{\"x\":1,\"x\":2}")",
        R"("{\"date\":\"<date>\"")", R"("line one\n第二行")"}) {
    const auto payload =
        R"("type":"function","function":{"name":"lookup","arguments":)" +
        arguments + "}}]}";
    const auto call =
        R"({"role":"assistant","tool_calls":[{"id":"a",)" + payload;
    const auto history = List({kUser, call, feedback});
    Json request;
    Q4T_CHECK(ParseRequest(history, &request));
    Q4T_CHECK(q4t::server::ValidateChatContract(request, "m", false));
    std::string prompt;
    Q4T_CHECK(q4t::server::RenderChatPrompt(
        request, {"q", "", "Please supply an integer."}, &prompt));
    const std::string marker =
        "[Historical function call arguments as received]\n";
    auto start = prompt.find(marker);
    Q4T_CHECK(start != std::string::npos);
    start += marker.size();
    const auto end = prompt.find("<|im_end|>", start);
    Q4T_CHECK(end != std::string::npos);
    const auto encoded = prompt.substr(start, end - start);
    Q4T_CHECK(encoded.find('<') == std::string::npos);
    Json observed, expected;
    Q4T_CHECK(q4t::io::ParseJson(encoded, &observed));
    Q4T_CHECK(q4t::io::ParseJson(arguments, &expected));
    Q4T_CHECK(observed.GetString("name") == "lookup");
    Q4T_CHECK(observed.Find("arguments") &&
              observed.Find("arguments")->IsString());
    Q4T_CHECK(observed.GetString("arguments") == expected.str);
    Q4T_CHECK(prompt.find("Please supply an integer.", end) !=
              std::string::npos);
    Q4T_CHECK(Reject(List({kUser, call, Result("missing")})));
    const auto legacy = R"({"role":"assistant","tool_calls":[{)" + payload;
    Q4T_CHECK(
        Reject(List({kUser, legacy, R"({"role":"tool","content":"error"})"})));
  }
  return true;
}

Q4T_TEST(chat_tool_history_legacy_shapes_are_explicit) {
  for (const auto* call : {
      R"({"type":"custom","function":{"name":"lookup","arguments":{}}})",
      R"({"name":"lookup","arguments":{},"extra":1})",
      R"({"type":"custom","name":"lookup","arguments":{}})",
      R"({"function":{"name":"lookup","arguments":{}},"extra":1})",
      R"({"id":"a","name":"lookup","arguments":{}})", R"(null)"})
    Q4T_CHECK(Reject(List({kUser,
        std::string(R"({"role":"assistant","tool_calls":[)") + call + "]}",
        R"({"role":"tool","content":"result"})"})));
  for (const auto* call : {
      R"({"name":"lookup","arguments":{}})",
      R"({"type":"function","name":"lookup","arguments":"{}"})",
      R"({"function":{"name":"lookup","arguments":{"n":9007199254740993}}})"}) {
    Json req;
    Q4T_CHECK(ParseRequest(List({kUser,
        std::string(R"({"role":"assistant","tool_calls":[)") + call + "]}",
        R"({"role":"tool","content":""})"}), &req));
    Q4T_CHECK(q4t::server::ValidateChatContract(req, "m", false).ok());
    std::string prompt;
    Q4T_CHECK(q4t::server::RenderChatPrompt(req, {"q", "", ""}, &prompt).ok());
  }
  return true;
}

Q4T_TEST(chat_template_tool_result_names_follow_ids) {
  Json request;
  Q4T_CHECK(ParseRequest(
      List(
          {kUser, kPair,
           R"({"role":"tool","tool_call_id":"b","name":"second","content":"B"})",
           R"({"role":"tool","tool_call_id":"a","name":"first","content":"A"})"}),
      &request));
  Q4T_CHECK(
      q4t::server::ValidateChatContract(request, "qwen3.8-flash-next", false));
  std::string prompt;
  Q4T_CHECK(
      q4t::server::RenderChatPrompt(request, {"q", "", "B", "A"}, &prompt));
  Q4T_CHECK(
      prompt.find("<tool_response>\nname=\"first\"\nA\n</tool_response>\n"
                  "<tool_response>\nname=\"second\"\nB\n</tool_response>") !=
      std::string::npos);
  return true;
}
