#include "../src/server/schema_token_guard.h"

#include <cstring>
#include <stdexcept>
#include <unordered_map>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Parse(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true,
                                         {64, 65536, 1024 * 1024, true, true});
  if (!status) throw std::runtime_error(status.message());
  return value;
}
std::string Object(const std::string& child) {
  return R"({"type":"object","properties":{"v":)" + child +
         R"(},"required":["v"],"additionalProperties":false})";
}
Json Content(const std::string& schema) {
  return Parse(R"({"content":{"schema":)" + schema + "}}");
}
bool Allowed(std::span<const int32_t> mask, size_t index) {
  return (static_cast<uint32_t>(mask[index / 32]) >> (index % 32)) & 1;
}
uint16_t Bf16(float value) {
  uint32_t bits;
  std::memcpy(&bits, &value, sizeof(bits));
  return static_cast<uint16_t>(bits >> 16);
}
OutputConstraints WorkerPlan(const std::string& schema) {
  OutputConstraints plan;
  plan.active = plan.json_output = true;
  plan.structural_tag =
      R"({"type":"structural_tag","format":{"type":"q4t_schema","schema":)" +
      schema + R"(},"q4t_guard":)" + OutputJson(Content(schema)) + '}';
  return plan;
}

constexpr auto kClosed = "<|im_start|>assistant\n<think>\n\n</think>\n\n";
Json RichRequest(std::string_view extra = {}) {
  return Parse(
      R"({"messages":[{"role":"user","content":"query"}],)"
      R"("enable_thinking":false,"tools":[{"type":"function",)"
      R"("function":{"name":"read","strict":true,"parameters":)" +
      Object(R"({"anyOf":[{"type":"string","format":"time"},
                         {"type":"number","enum":[1e400]}]})") +
      R"(}},{"type":"function","function":{"name":"noop"}},)"
      R"({"type":"custom","custom":{"name":"raw","format":{"type":"text"}}}])" +
      (extra.empty() ? "" : ',' + std::string(extra)) + '}');
}
std::string RichCall(std::string_view arguments) {
  return "<tool_call>\n<function=read>\n<parameter=arguments>\n" +
         std::string(arguments) + "\n</parameter>\n</function>\n</tool_call>";
}
bool RequestRoundTrip(const Json& request, const std::string& text,
                      size_t calls, std::string_view content,
                      std::string_view refusal = {}) {
  OutputConstraints plan;
  auto status = PlanOutputConstraints(request, &plan);
  if (!status) std::fprintf(stderr, "plan: %s\n", status.message().c_str());
  Q4T_CHECK(status);
  std::vector<std::string> vocabulary{""};
  std::unordered_map<char, int32_t> ids;
  for (char c : text)
    if (!ids.contains(c)) {
      ids[c] = static_cast<int32_t>(vocabulary.size());
      vocabulary.emplace_back(1, c);
    }
  std::unique_ptr<StructuredDecoder> decoder;
  status = StructuredDecoder::Create(
      plan, vocabulary, static_cast<int>(vocabulary.size()), {0}, &decoder);
  if (!status) std::fprintf(stderr, "compile: %s\n", status.message().c_str());
  Q4T_CHECK(status);
  ChatResponseParser parser;
  Q4T_CHECK(parser.Init(request, kClosed, "receipt", true));
  parser.SetSchemaReceipts(&decoder->Receipts());
  const std::vector<uint16_t> logits(vocabulary.size(), Bf16(1));
  std::vector<ResponseDelta> deltas;
  int32_t token = -1;
  for (size_t i = 0; i < text.size(); ++i) {
    status = decoder->Select(logits, &token, ids.at(text[i]));
    if (!status)
      std::fprintf(stderr, "token %zu: %s\n", i, status.message().c_str());
    Q4T_CHECK(status);
    Q4T_CHECK(parser.Append(vocabulary[token], &deltas));
  }
  Q4T_CHECK(!deltas.empty());  // Actual provisional output precedes terminal.
  Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
  status = parser.Finish(false, &deltas);
  if (!status) std::fprintf(stderr, "finish: %s\n", status.message().c_str());
  Q4T_CHECK(status);
  const auto message = Parse(parser.MessageJson());
  const auto* actual_calls = message.GetArray("tool_calls");
  Q4T_CHECK((actual_calls ? actual_calls->array.size() : 0) == calls);
  Q4T_CHECK(message.GetString("content") == content);
  Q4T_CHECK(message.GetString("refusal") == refusal);
  std::string streamed_content, streamed_refusal;
  std::vector<std::string> arguments;
  for (const auto& delta : deltas) {
    const auto wire = Parse(delta.ToJson());
    streamed_content += wire.GetString("content");
    streamed_refusal += wire.GetString("refusal");
    if (const auto* tools = wire.GetArray("tool_calls"))
      for (const auto& tool : tools->array) {
        const auto index = tool.GetInt("index", -1);
        Q4T_CHECK(index >= 0 && static_cast<size_t>(index) <= arguments.size());
        if (static_cast<size_t>(index) == arguments.size())
          arguments.push_back({});
        const auto* value = tool.Find("function");
        arguments[index] += value ? value->GetString("arguments")
                                  : tool.Find("custom")->GetString("input");
      }
  }
  Q4T_CHECK(streamed_content == content && arguments.size() == calls);
  Q4T_CHECK(streamed_refusal == refusal);
  for (size_t i = 0; i < calls; ++i) {
    const auto& tool = actual_calls->array[i];
    const auto* value = tool.Find("function");
    Q4T_CHECK(arguments[i] == (value
                                   ? value->GetString("arguments")
                                   : tool.Find("custom")->GetString("input")));
  }
  return true;
}
}  // namespace

Q4T_TEST(schema_request_worker_parser_mixed_tools_and_content_round_trip) {
  const auto response =
      R"("response_format":{"type":"json_schema","json_schema":{"name":"r",
          "strict":true,"schema":)" +
      Object(R"({"type":"string","format":"time"})") + "}}";
  const auto request = RichRequest(response);
  Q4T_CHECK(RequestRoundTrip(request, R"({"v":"00:59:60+01:00"})", 0,
                             R"({"v":"00:59:60+01:00"})"));
  const std::string tools =
      RichCall(R"({"v":1e400})") +
      "\n<tool_call>\n<function=noop>\n\n</function>\n</tool_call>\n" +
      "<tool_call>\n<function=raw>\n<parameter=input>\n"
      "\"\\u003c/tool_call>hello\"\n</parameter>\n</function>\n</tool_call>";
  Q4T_CHECK(RequestRoundTrip(request, tools, 3, ""));
  Q4T_CHECK(
      RequestRoundTrip(RichRequest(response + R"(,"tool_choice":"required")"),
                       RichCall(R"({"v":"23:59:60Z"})"), 1, ""));
  Q4T_CHECK(RequestRoundTrip(RichRequest(response + R"(,"tool_choice":"none")"),
                             R"({"v":"23:59:60Z"})", 0,
                             R"({"v":"23:59:60Z"})"));
  return true;
}

Q4T_TEST(schema_request_json_object_markers_with_rich_tools_are_data) {
  const auto request =
      RichRequest(R"("response_format":{"type":"json_object"})");
  const std::string body = R"({"v":"<tool_call></tool_call><think>"})";
  Q4T_CHECK(RequestRoundTrip(request, body, 0, body));
  Q4T_CHECK(RequestRoundTrip(request, RichCall(R"({"v":1e400})"), 1, ""));
  return true;
}

Q4T_TEST(schema_parser_receipts_reject_missing_wrong_scope_value_and_count) {
  const auto request = RichRequest(R"("tool_choice":"required")");
  const auto& schema =
      *request.Find("tools")->array[0].Find("function")->Find("parameters");
  SchemaReceipt right, wrong_scope, wrong_value;
  Q4T_CHECK(MakeSchemaReceipt("read", schema, Parse(R"({"v":1e400})"), &right));
  Q4T_CHECK(MakeSchemaReceipt("write", schema, Parse(R"({"v":1e400})"),
                              &wrong_scope));
  Q4T_CHECK(
      MakeSchemaReceipt("read", schema, Parse(R"({"v":1e399})"), &wrong_value));
  const std::vector<std::vector<SchemaReceipt>> candidates{
      {}, {wrong_scope}, {wrong_value}, {right, right}, {right}};
  for (size_t i = 0; i < candidates.size() + 1; ++i) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "r", false));
    if (i < candidates.size()) parser.SetSchemaReceipts(&candidates[i]);
    Q4T_CHECK(parser.Append(RichCall(R"({"v":1e400})"), &deltas));
    Q4T_CHECK(!deltas.empty());
    Q4T_CHECK(parser.Finish(false, &deltas).ok() == (i == 4));
  }
  return true;
}

Q4T_TEST(schema_chat_exact_data_does_not_relax_generation_numeric_controls) {
  auto request = RichRequest(R"("tool_choice":"required")");
  const auto wire = OutputJson(request);
  Json parsed;
  Q4T_CHECK(q4t::io::ParseJson(wire, &parsed, true, kSchemaJsonLimits));
  Q4T_CHECK(ValidateChatContract(parsed, "m", false));
  Q4T_CHECK(!q4t::io::ParseJson(wire, &parsed, true, kRequestJsonLimits));
  for (const auto* key :
       {"temperature", "top_p", "frequency_penalty", "presence_penalty", "seed",
        "n", "top_logprobs", "thinking_token_budget", "request_timeout_ms",
        "max_tokens", "prefix_cache_write_tokens"}) {
    for (const auto* number : {"1e400", "1e-400"}) {
      auto invalid = request;
      invalid.object.emplace_back(key, Parse(number));
      std::string param;
      Q4T_CHECK(!ValidateChatContract(invalid, "m", false, &param));
      Q4T_CHECK(param == key);
    }
  }
  auto invalid = request;
  invalid.object.emplace_back("logit_bias", Parse(R"({"1":1e400})"));
  std::string param;
  Q4T_CHECK(!ValidateChatContract(invalid, "m", false, &param));
  Q4T_CHECK(param == "logit_bias[\"1\"]");
  return true;
}

Q4T_TEST(schema_request_closed_empty_object_and_referenced_array) {
  const auto schema = Parse(R"({"type":"object","properties":{
    "empty":{"type":"object","additionalProperties":false},
    "xs":{"$ref":"#/$defs/list","type":"array"}},
    "required":["empty","xs"],"additionalProperties":false,
    "$defs":{"list":{"type":"array","items":{"type":"integer"},
                     "maxItems":65}}})");
  Q4T_CHECK(ValidateOutputSchema(schema, true));
  Q4T_CHECK(ValidateOutputValue(schema, Parse(R"({"empty":{},"xs":[1,2]})")));
  Q4T_CHECK(
      !ValidateOutputValue(schema, Parse(R"({"empty":{"x":1},"xs":[]})")));
  auto request = RichRequest(
      R"("tool_choice":"none","response_format":{"type":"json_schema",)"
      R"("json_schema":{"name":"r","strict":true,"schema":)" +
      OutputJson(schema) + "}}");
  Q4T_CHECK(RequestRoundTrip(request, R"({"empty":{},"xs":[1,2]})", 0,
                             R"({"empty":{},"xs":[1,2]})"));
  const auto excessive = Parse(
      Object(R"({"type":"array","items":{"type":"string"},"maxItems":1e400})"));
  std::string param;
  const auto status = ValidateOutputSchema(excessive, true, false, &param);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_resource_exhausted:"));
  Q4T_CHECK(param == "schema.properties.v.maxItems");
  return true;
}

Q4T_TEST(schema_request_accepts_128_declared_tools_and_bounds_the_next) {
  auto request = Parse(R"({"messages":[{"role":"user","content":"choose"}],
      "enable_thinking":false,"tool_choice":"required",
      "parallel_tool_calls":false,"tools":[]})");
  auto* tools = &request.object.back().second;
  for (int i = 0; i < 128; ++i)
    tools->array.push_back(Parse(R"({"type":"function","function":{"name":"f)" +
                                 std::to_string(i) + R"("}})"));
  Q4T_CHECK(RequestRoundTrip(
      request, "<tool_call>\n<function=f127>\n\n</function>\n</tool_call>", 1,
      ""));
  tools->array.push_back(
      Parse(R"({"type":"function","function":{"name":"f128"}})"));
  OutputConstraints plan;
  std::string param;
  Q4T_CHECK(!PlanOutputConstraints(request, &plan, &param) && param == "tools");
  return true;
}

Q4T_TEST(schema_guard_filters_completed_format_tokens_and_receipts) {
  const auto schema = Object(R"({"type":"string","format":"time"})");
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(Content(schema)));
  Q4T_CHECK(guard.Accept(R"({"v":"23:59:60)", false));
  const std::vector<std::string> vocabulary{
      "", "Z\"}", "+01:00\"}", "-00:00\"}", "Z", "Z\"}trailing"};
  std::vector<int32_t> mask(1, -1);
  const std::vector<int32_t> stops{0};
  Q4T_CHECK(guard.Filter(vocabulary, stops, mask));
  Q4T_CHECK(!Allowed(mask, 0) && Allowed(mask, 1) && !Allowed(mask, 2));
  Q4T_CHECK(Allowed(mask, 3) && Allowed(mask, 4) && !Allowed(mask, 5));
  Q4T_CHECK(!guard.Accept(vocabulary[2], false));
  Q4T_CHECK(guard.Accept(vocabulary[1], false));
  Q4T_CHECK(guard.Accept("", true));
  SchemaReceipt expected;
  Q4T_CHECK(MakeSchemaReceipt("", Parse(schema), Parse(R"({"v":"23:59:60Z"})"),
                              &expected));
  Q4T_CHECK(guard.Receipts() == std::vector<SchemaReceipt>{expected});
  return true;
}

Q4T_TEST(schema_guard_thinking_tools_and_payload_marker_boundaries) {
  const auto schema = Object(R"({"type":"string"})");
  const auto config = Parse(
      R"({"thinking":true,"tools":[{"name":"read",)"
      R"("begin":"<tool_call>\n<function=read>\n<parameter=arguments>\n",)"
      R"("end":"\n</parameter>\n</function>\n</tool_call>","schema":)" +
      schema + "}]}");
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(config));
  const std::string stream =
      "thought <tool_call> bad JSON </think>\nBefore\n"
      "<tool_call>\n<function=read>\n<parameter=arguments>\n"
      "{\"v\":\"\\u003c/tool_call>中😀\"}\n</parameter>\n</function>\n</"
      "tool_call>\n"
      "<tool_call>\n<function=read>\n<parameter=arguments>\n"
      "{\"v\":\"second\"}\n</parameter>\n</function>\n</tool_call>";
  for (char c : stream) Q4T_CHECK(guard.Accept(std::string(1, c), false));
  Q4T_CHECK(guard.Accept("", true));
  Q4T_CHECK(guard.Receipts().size() == 2);
  SchemaReceipt expected;
  Q4T_CHECK(MakeSchemaReceipt("read", Parse(schema),
                              Parse(R"({"v":"</tool_call>中😀"})"), &expected));
  Q4T_CHECK(guard.Receipts()[0] == expected);
  return true;
}

Q4T_TEST(schema_guard_anyof_large_exact_numbers_and_scope_identity) {
  const auto schema =
      Object(R"({"anyOf":[{"type":"number","enum":[1e400,1e-400]},
    {"type":"string","format":"ipv4"}]})");
  for (const auto& value :
       {R"({"v":1e400})", R"({"v":1e-400})", R"({"v":"192.0.2.1"})"}) {
    SchemaTokenGuard guard;
    Q4T_CHECK(guard.Init(Content(schema)));
    Q4T_CHECK(guard.Accept(value, false) && guard.Accept("", true));
    Q4T_CHECK(guard.Receipts().size() == 1);
  }
  for (const auto& value : {R"({"v":1e399})", R"({"v":"192.0.2.999"})"}) {
    SchemaTokenGuard guard;
    Q4T_CHECK(guard.Init(Content(schema)));
    Q4T_CHECK(!guard.Accept(value, false));
  }
  SchemaReceipt first, reordered, wrong_scope;
  const auto value = Parse(R"({"z":1e400,"x":0.30})");
  Q4T_CHECK(MakeSchemaReceipt("read", Parse(schema), value, &first));
  Q4T_CHECK(MakeSchemaReceipt("read", Parse(schema),
                              Parse(R"({"x":3e-1,"z":10e399})"), &reordered));
  Q4T_CHECK(MakeSchemaReceipt("write", Parse(schema), value, &wrong_scope));
  Q4T_CHECK(first == reordered && first != wrong_scope);
  return true;
}

Q4T_TEST(schema_guard_dependency_error_is_not_an_invalid_candidate) {
  SchemaTokenGuard guard;
  Q4T_CHECK(
      guard.Init(Content(Object(R"({"type":"string","format":"hostname"})")),
                 [](const Json&, std::string_view, size_t*, bool*) {
                   return q4t::Status::Fail(
                       "schema_dependency_unavailable: injected IDNA");
                 }));
  Q4T_CHECK(guard.Accept(R"({"v":"xn--bcher-kva)", false));
  const std::vector<std::string> vocabulary{"\"}"};
  std::vector<int32_t> mask(1, -1);
  const auto status = guard.Filter(vocabulary, {}, mask);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_dependency_unavailable:"));
  Q4T_CHECK(Allowed(mask, 0) && guard.Receipts().empty());
  return true;
}

Q4T_TEST(schema_guard_large_vocabulary_skips_uncompleted_string_checks) {
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(Content(Object(R"({"type":"string"})"))));
  Q4T_CHECK(guard.Accept(R"({"v":")" + std::string(4096, 'x'), false));
  const std::vector<std::string> vocabulary(300000, "中");
  std::vector<int32_t> mask((vocabulary.size() + 31) / 32, -1);
  Q4T_CHECK(guard.Filter(vocabulary, {}, mask));
  for (size_t i = 0; i < vocabulary.size(); ++i) Q4T_CHECK(Allowed(mask, i));
  Q4T_CHECK(guard.Accept("\"}", false) && guard.Accept("", true));
  return true;
}

Q4T_TEST(schema_guard_refusal_only_legacy_tool_has_bounded_mask_work) {
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(Parse(R"({"refusal":true,"thinking":false})")));
  const std::string prefix =
      "<tool_call>\n<function=q4t_builtin_tool_search>\n"
      "<parameter=arguments_json>\n";
  Q4T_CHECK(guard.Accept(prefix, false));
  std::vector<std::string> vocabulary(300000, "a");
  vocabulary[0] = "";
  vocabulary[1] = "";  // Non-stop empty/special vocabulary must stay masked.
  std::vector<int32_t> mask((vocabulary.size() + 31) / 32, -1);
  const std::vector<int32_t> stops{0};
  const auto status = guard.Filter(vocabulary, stops, mask);
  if (!status) std::fprintf(stderr, "%s\n", status.message().c_str());
  Q4T_CHECK(status && !Allowed(mask, 0) && !Allowed(mask, 1));
  for (size_t i = 2; i < vocabulary.size(); ++i) Q4T_CHECK(Allowed(mask, i));
  Q4T_CHECK(
      guard.Accept("{\"paths\":[\"project\"]}\n</parameter>\n"
                   "</function>\n</tool_call>",
                   false));
  Q4T_CHECK(guard.Accept("", true) && guard.Receipts().empty());
  return true;
}

Q4T_TEST(schema_worker_masks_native_semantics_and_returns_value_receipt) {
  const auto schema = Object(R"({"type":"string","format":"time"})");
  const std::vector<std::string> vocabulary{"", R"({"v":"23:59:60Z"})",
                                            R"({"v":"12:00:60Z"})",
                                            R"({"v":"00:59:60+01:00"})"};
  std::unique_ptr<StructuredDecoder> decoder;
  const auto created = StructuredDecoder::Create(WorkerPlan(schema), vocabulary,
                                                 4, {0}, &decoder);
  if (!created) std::fprintf(stderr, "%s\n", created.message().c_str());
  Q4T_CHECK(created);
  const std::vector<uint16_t> logits(4, Bf16(1));
  int32_t token = -1;
  Q4T_CHECK(!decoder->Select(logits, &token, 2));
  Q4T_CHECK(decoder->Select(logits, &token, 3) && token == 3);
  Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
  SchemaReceipt expected;
  Q4T_CHECK(
      MakeSchemaReceipt("", Parse(schema), Parse(vocabulary[3]), &expected));
  Q4T_CHECK(decoder->Receipts() == std::vector<SchemaReceipt>{expected});
  return true;
}

Q4T_TEST(schema_worker_idna_and_exact_number_constraints_use_same_path) {
  const auto schema = Object(R"({"anyOf":[{"type":"string","format":"hostname"},
    {"type":"number","enum":[1e400,1e-400]}]})");
  const std::vector<std::string> vocabulary{"",
                                            R"({"v":"xn--bcher-kva"})",
                                            R"({"v":"xn--a"})",
                                            R"({"v":1e400})",
                                            R"({"v":1e-400})",
                                            R"({"v":1e399})"};
  for (int selected : {1, 3, 4}) {
    std::unique_ptr<StructuredDecoder> decoder;
    const auto created = StructuredDecoder::Create(
        WorkerPlan(schema), vocabulary, 6, {0}, &decoder);
    if (!created) std::fprintf(stderr, "%s\n", created.message().c_str());
    Q4T_CHECK(created);
    const std::vector<uint16_t> logits(6, Bf16(1));
    int32_t token = -1;
    Q4T_CHECK(!decoder->Select(logits, &token, 2));
    Q4T_CHECK(!decoder->Select(logits, &token, 5));
    Q4T_CHECK(decoder->Select(logits, &token, selected));
    Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
    SchemaReceipt expected;
    Q4T_CHECK(MakeSchemaReceipt("", Parse(schema), Parse(vocabulary[selected]),
                                &expected));
    Q4T_CHECK(decoder->Receipts() == std::vector<SchemaReceipt>{expected});
  }
  return true;
}

Q4T_TEST(schema_worker_binding_mismatch_cannot_bypass_semantic_checks) {
  const auto schema = Object(R"({"type":"string","format":"time"})");
  auto plan = WorkerPlan(schema);
  auto document = Parse(plan.structural_tag);
  auto wrong = Content(Object(R"({"type":"string"})"));
  for (auto& [key, value] : document.object)
    if (key == "q4t_guard") value = wrong;
  plan.structural_tag = OutputJson(document);
  const std::vector<std::string> vocabulary{"", R"({"v":"12:00:60Z"})"};
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(!StructuredDecoder::Create(plan, vocabulary, 2, {0}, &decoder));
  Q4T_CHECK(!decoder);
  return true;
}

Q4T_TEST(refusal_worker_schema_tool_and_text_branches_round_trip) {
  const auto schema =
      R"("response_format":{"type":"json_schema","json_schema":{"name":"r",
          "strict":true,"schema":)" +
      Object(R"({"type":"string","format":"time"})") + "}}";
  for (const auto& extra : std::vector<std::string>{
           schema, R"("response_format":{"type":"json_object"})",
           R"("tool_choice":"required")", R"("tool_choice":"none")",
           R"("tool_choice":{"type":"function","function":{"name":"read"}})"}) {
    Q4T_CHECK(RequestRoundTrip(RichRequest(extra),
                               "\n<q4t_refusal>拒绝 \"data\"</q4t_refusal>\n",
                               0, "", "拒绝 \"data\""));
  }
  auto no_rich_tools = Parse(R"({"messages":[{"role":"user","content":"q"}],
      "enable_thinking":false,"tool_choice":"none"})");
  Q4T_CHECK(RequestRoundTrip(no_rich_tools, "<q4t_refusal>no</q4t_refusal>", 0,
                             "", "no"));
  Q4T_CHECK(RequestRoundTrip(no_rich_tools,
                             "literal <q4t_refusal>data</q4t_refusal>", 0,
                             "literal <q4t_refusal>data</q4t_refusal>"));
  return true;
}

Q4T_TEST(refusal_worker_rejects_truncation_empty_and_mixed_ordinary_branch) {
  for (const auto& extra : std::vector<std::string>{
           R"("tool_choice":"none")",
           R"("response_format":{"type":"json_object"})"}) {
    const auto request = RichRequest(extra);
    OutputConstraints plan;
    Q4T_CHECK(PlanOutputConstraints(request, &plan));
    const std::vector<std::string> vocabulary{
        "",
        "<q4t_refusal>",
        "no",
        "</q4t_refusal>",
        " ",
        "bad",
        "</q4t_refusal>tool",
        "<q4t_refusal>no</q4t_refusal>trailing"};
    std::unique_ptr<StructuredDecoder> decoder;
    Q4T_CHECK(StructuredDecoder::Create(plan, vocabulary, vocabulary.size(),
                                        {0}, &decoder));
    const std::vector<uint16_t> logits(vocabulary.size(), Bf16(1));
    int32_t token = -1;
    Q4T_CHECK(!decoder->Select(logits, &token, 7));
    Q4T_CHECK(decoder->Select(logits, &token, 1));
    Q4T_CHECK(!decoder->Select(logits, &token, 0));
    Q4T_CHECK(!decoder->Select(logits, &token, 3));
    Q4T_CHECK(decoder->Select(logits, &token, 2));
    Q4T_CHECK(!decoder->Select(logits, &token, 6));
    Q4T_CHECK(decoder->Select(logits, &token, 3));
    Q4T_CHECK(!decoder->Select(logits, &token, 5));
    Q4T_CHECK(decoder->Select(logits, &token, 4));
    Q4T_CHECK(decoder->Select(logits, &token, 0));
    Q4T_CHECK(decoder->Terminated());
    Q4T_CHECK(decoder->Receipts() ==
              std::vector<SchemaReceipt>{MakeRefusalReceipt("no")});
  }
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(schema_worker_dependency_and_resource_errors_recover_per_request) {
  const auto schema = Object(R"({"type":"string","format":"hostname"})");
  const std::vector<std::string> vocabulary{"", R"({"v":"xn--bcher-kva"})"};
  for (const auto& [fault, expected] :
       std::vector<std::pair<std::string, GrammarFailure>>{
           {"dependency", GrammarFailure::kDependency},
           {"resource", GrammarFailure::kResource}}) {
    auto plan = WorkerPlan(schema);
    plan.structural_tag.pop_back();
    plan.structural_tag += ",\"q4t_test_schema_failure\":\"" + fault + "\"}";
    std::unique_ptr<StructuredDecoder> decoder;
    GrammarFailure failure = GrammarFailure::kNone;
    Q4T_CHECK(!StructuredDecoder::Create(plan, vocabulary, 2, {0}, &decoder, {},
                                         &failure));
    Q4T_CHECK(failure == expected && !decoder);
  }
  // A new request creates a fresh bounded checker. No model/controller
  // process is restarted to recover validation capacity or dependencies.
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(StructuredDecoder::Create(WorkerPlan(schema), vocabulary, 2, {0},
                                      &decoder));
  std::vector<uint16_t> logits(2, Bf16(1));
  int32_t token = -1;
  Q4T_CHECK(decoder->Select(logits, &token, 1));
  Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
  Q4T_CHECK(decoder->Receipts().size() == 1);
  return true;
}
#endif
