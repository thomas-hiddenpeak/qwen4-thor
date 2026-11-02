#include "q4t/server/chat_response_parser.h"

#include <string>
#include <vector>

#include "q4t/server/http_response.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::server::ChatResponseParser;
using q4t::server::ResponseDelta;
constexpr auto kClosed = "<|im_start|>assistant\n<think>\n\n</think>\n\n";
constexpr auto kThinking = "<|im_start|>assistant\n<think>\n";

Json Request(bool tools = true) {
  Json request;
  q4t::io::ParseJson(R"({"messages":[{"role":"user","content":"test"}],
    "tools":[{"type":"function","function":{"name":"lookup",
      "parameters":{"type":"object","properties":{
        "text":{"type":"string"},"count":{"type":"integer"},
        "ratio":{"type":"number"},"enabled":{"type":"boolean"},
        "options":{"type":"object"},"items":{"type":"array"},
        "optional":{"type":["string","null"]}}}}},
      {"type":"function","function":{"name":"noop"}}]})",
                     &request);
  if (!tools) request.object.pop_back();
  return request;
}

std::string Call(const std::string& body = {},
                 const std::string& name = "lookup") {
  return "<tool_call>\n<function=" + name + ">\n" + body +
         "</function>\n</tool_call>";
}

// Reassemble actual SSE JSON fields as a client would. Compare with the
// non-streaming message; this checks the wire contract, not just parser state.
bool CheckWire(const Json& message, const std::vector<ResponseDelta>& deltas) {
  std::string content, reasoning;
  std::vector<Json> calls;
  for (const auto& delta : deltas) {
    const auto frame = q4t::server::detail::SseDeltaChunk(
        "request", "model", delta.ToJson(), "", 0);
    Q4T_CHECK(frame.starts_with("data: "));
    Json payload;
    Q4T_CHECK(
        q4t::io::ParseJson(frame.substr(6, frame.size() - 10), &payload).ok());
    const auto& choice = payload.Find("choices")->array.at(0);
    Q4T_CHECK(choice.Find("finish_reason")->IsNull());
    const auto* field = choice.Find("delta");
    content += field->GetString("content");
    reasoning += field->GetString("reasoning_content");
    if (const auto* tools = field->GetArray("tool_calls")) {
      for (const auto& call : tools->array) {
        Q4T_CHECK(call.GetInt("index", -1) ==
                  static_cast<int64_t>(calls.size()));
        Q4T_CHECK(call.GetString("type") == "function");
        Q4T_CHECK(call.GetString("id") ==
                  "call_request_" + std::to_string(calls.size()));
        calls.push_back(call);
      }
    }
  }
  Q4T_CHECK(content == message.GetString("content"));
  Q4T_CHECK(reasoning == message.GetString("reasoning_content"));
  const auto* expected = message.GetArray("tool_calls");
  Q4T_CHECK(calls.size() == (expected ? expected->array.size() : 0));
  for (size_t i = 0; i < calls.size(); ++i) {
    const auto& want = expected->array[i];
    Q4T_CHECK(calls[i].GetString("id") == want.GetString("id"));
    const auto* actual_fn = calls[i].Find("function");
    const auto* wanted_fn = want.Find("function");
    Q4T_CHECK(actual_fn->GetString("name") == wanted_fn->GetString("name"));
    Q4T_CHECK(actual_fn->GetString("arguments") ==
              wanted_fn->GetString("arguments"));
    Json arguments;
    Q4T_CHECK(q4t::io::ParseJson(actual_fn->GetString("arguments"), &arguments)
                  .ok() &&
              arguments.IsObject());
  }
  return true;
}

bool EverySplit(const Json& request, const std::string& text,
                const std::string& prefix, const std::string& expected_content,
                const std::string& expected_reasoning, size_t expected_calls,
                Json* final = nullptr) {
  ChatResponseParser full;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(full.Init(request, prefix, "request", true).ok());
  Q4T_CHECK(full.Append(text, &deltas).ok());
  Q4T_CHECK(full.Finish(false, &deltas).ok());
  const auto expected = full.MessageJson();
  Json message;
  Q4T_CHECK(q4t::io::ParseJson(expected, &message).ok());
  Q4T_CHECK(message.GetString("content") == expected_content);
  Q4T_CHECK(message.GetString("reasoning_content") == expected_reasoning);
  const auto* calls = message.GetArray("tool_calls");
  Q4T_CHECK((calls ? calls->array.size() : 0) == expected_calls);
  Q4T_CHECK(CheckWire(message, deltas));
  for (size_t split = 0; split <= text.size(); ++split) {
    ChatResponseParser stream;
    q4t::server::detail::Utf8Stream utf8;
    deltas.clear();
    Q4T_CHECK(stream.Init(request, prefix, "request", false).ok());
    Q4T_CHECK(stream.Append(utf8.Append(text.substr(0, split)), &deltas).ok());
    Q4T_CHECK(stream.Append(utf8.Append(text.substr(split)), &deltas).ok());
    Q4T_CHECK(stream.Append(utf8.Finish(), &deltas).ok());
    Q4T_CHECK(stream.Finish(false, &deltas).ok());
    Q4T_CHECK(CheckWire(message, deltas));
  }
  // One-byte feeds exercise more than one split within a delimiter/codepoint.
  ChatResponseParser stream;
  q4t::server::detail::Utf8Stream utf8;
  deltas.clear();
  Q4T_CHECK(stream.Init(request, prefix, "request", false).ok());
  for (const char byte : text)
    Q4T_CHECK(stream.Append(utf8.Append(std::string(1, byte)), &deltas).ok());
  Q4T_CHECK(stream.Append(utf8.Finish(), &deltas).ok());
  Q4T_CHECK(stream.Finish(false, &deltas).ok());
  Q4T_CHECK(CheckWire(message, deltas));
  if (final) *final = std::move(message);
  return true;
}
}  // namespace

Q4T_TEST(chat_response_reasoning_and_prefilled_prefix) {
  const auto request = Request();
  const std::string reason = "先分析✅ <tool_call>只是思考中的示例</tool_call>";
  Q4T_CHECK(EverySplit(request, reason + "</think>\n\n答案", kThinking,
                       "\n\n答案", reason, 0));
  Q4T_CHECK(EverySplit(request, "<think>思考</think>正文", kClosed, "正文",
                       "思考", 0));
  return true;
}

Q4T_TEST(chat_response_tools_typed_and_multiple) {
  const auto request = Request();
  const std::string body =
      "<parameter=text>\n  00123 \"中国\"\\path  \n</parameter>\n"
      "<parameter=count>9007199254740993</parameter>\n"
      "<parameter=ratio>1.25e-3</parameter>\n"
      "<parameter=enabled>true</parameter>\n"
      "<parameter=options>{\"nested\":[1,\"x\",null]}</parameter>\n"
      "<parameter=items>[false,{\"a\":2}]</parameter>\n"
      "<parameter=optional>null</parameter>\n";
  Json message;
  Q4T_CHECK(EverySplit(request,
                       "先查询。" + Call(body) + "\n" + Call({}, "noop"),
                       kClosed, "先查询。\n", "", 2, &message));
  const auto& call = message.Find("tool_calls")->array[0];
  const auto arguments = call.Find("function")->GetString("arguments");
  Q4T_CHECK(arguments.find("9007199254740993") != arguments.npos);
  Json parsed;
  Q4T_CHECK(q4t::io::ParseJson(arguments, &parsed).ok());
  Q4T_CHECK(parsed.GetString("text") == "  00123 \"中国\"\\path  ");
  Q4T_CHECK(parsed.GetBool("enabled"));
  Q4T_CHECK(parsed.Find("optional")->IsNull());
  Q4T_CHECK(parsed.Find("options")->IsObject());
  Q4T_CHECK(parsed.Find("items")->IsArray());
  return true;
}

Q4T_TEST(chat_response_tool_publication_is_atomic) {
  const auto request = Request();
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(request, kClosed, "request", true).ok());
  Q4T_CHECK(
      parser.Append("<tool_call><function=noop></function>", &deltas).ok());
  Q4T_CHECK(deltas.empty() && !parser.HasToolCalls());
  Q4T_CHECK(parser.Append("</tool_call>", &deltas).ok());
  Q4T_CHECK(deltas.size() == 1 && parser.HasToolCalls());
  Q4T_CHECK(parser.Finish(false, &deltas).ok());
  Json message;
  Q4T_CHECK(q4t::io::ParseJson(parser.MessageJson(), &message).ok());
  Q4T_CHECK(message.Find("content")->IsNull());
  Q4T_CHECK(CheckWire(message, deltas));
  Q4T_CHECK(!parser.Append("more", &deltas).ok());
  return true;
}

Q4T_TEST(chat_response_rejects_malformed_and_unknown_tools) {
  const auto request = Request();
  for (const auto& text :
       {Call({}, "unknown"), Call("oops"),
        Call("<parameter=count>1.5</parameter>"),
        Call("<parameter=count>1.0000000000000001</parameter>"),
        Call("<parameter=count>1e-1</parameter>"),
        Call("<parameter=count>NaN</parameter>"),
        Call("<parameter=count>7</parameter><parameter=count>8</parameter>"),
        Call("<parameter=enabled>yes</parameter>"),
        Call("<parameter=items>{}</parameter>"),
        Call("<parameter=options>{\"a\":1,\"a\":2}</parameter>"),
        Call("<parameter=items>[1,]</parameter>"),
        Call("<parameter=untyped>{\"a\":1,\"a\":2}</parameter>"),
        std::string("<tool_call><function=noop></tool_call>")}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "request", false).ok());
    Q4T_CHECK(!parser.Append(text, &deltas).ok());
    Q4T_CHECK(deltas.empty() && !parser.HasToolCalls());
    Q4T_CHECK(!parser.Finish(false, &deltas).ok());
  }
  return true;
}

Q4T_TEST(chat_response_exact_integer_spellings) {
  const auto request = Request();
  for (const auto* value :
       {"9007199254740993", "1.0", "1.25e2", "100e-2", "-0.0000", "10.0e+20"}) {
    Json message;
    Q4T_CHECK(EverySplit(
        request,
        Call(std::string("<parameter=count>") + value + "</parameter>"),
        kClosed, "", "", 1, &message));
    const auto arguments = message.Find("tool_calls")
                               ->array[0]
                               .Find("function")
                               ->GetString("arguments");
    Q4T_CHECK(arguments == std::string("{\"count\":") + value + '}');
  }
  return true;
}

Q4T_TEST(chat_response_truncation_and_length) {
  const auto request = Request();
  for (bool length : {false, true}) {
    for (const auto* text :
         {"<tool_call>", "<tool_call><function=noop>", "<tool_c"}) {
      ChatResponseParser parser;
      std::vector<ResponseDelta> deltas;
      Q4T_CHECK(parser.Init(request, kClosed, "request", true).ok());
      Q4T_CHECK(parser.Append(text, &deltas).ok());
      Q4T_CHECK(!parser.Finish(length, &deltas).ok());
      Q4T_CHECK(deltas.empty());
    }
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kThinking, "request", true).ok());
    Q4T_CHECK(parser.Append("still thinking <", &deltas).ok());
    Q4T_CHECK(parser.Finish(length, &deltas).ok() == length);
    if (length) {
      Json message;
      Q4T_CHECK(q4t::io::ParseJson(parser.MessageJson(), &message).ok());
      Q4T_CHECK(message.GetString("reasoning_content") == "still thinking <");
    }
  }
  Q4T_CHECK(EverySplit(request, "a < b <", kClosed, "a < b <", "", 0));
  return true;
}

Q4T_TEST(chat_response_raw_compatibility_and_no_tools) {
  const std::string literal = "<think>x</think>" + Call();
  Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"prompt":"raw"})", &request).ok());
  Q4T_CHECK(EverySplit(request, literal, "raw", literal, "", 0));
  Q4T_CHECK(
      q4t::io::ParseJson(R"({"messages":[],"parse_response":false})", &request)
          .ok());
  Q4T_CHECK(EverySplit(request, literal, "raw", literal, "", 0));
  request = Request(false);
  Q4T_CHECK(EverySplit(request, Call(), kClosed, Call(), "", 0));
  return true;
}

Q4T_TEST(chat_response_bounds) {
  const auto request = Request();
  const std::string prefix = "<function=lookup><parameter=text>";
  const std::string suffix = "</parameter></function>";
  const std::string block = prefix +
                            std::string(ChatResponseParser::kMaxToolBytes -
                                            prefix.size() - suffix.size(),
                                        'x') +
                            suffix;
  // A partial closing delimiter must not count as bytes in the tool body.
  for (const auto* partial : {"", "<", "</tool_cal"}) {
    ChatResponseParser boundary;
    std::vector<ResponseDelta> output;
    Q4T_CHECK(boundary.Init(request, kClosed, "request", false).ok());
    Q4T_CHECK(boundary.Append("<tool_call>" + block + partial, &output).ok());
    Q4T_CHECK(output.empty());
    Q4T_CHECK(
        boundary
            .Append(
                std::string("</tool_call>").substr(std::string(partial).size()),
                &output)
            .ok());
    Q4T_CHECK(boundary.Finish(false, &output).ok());
    Q4T_CHECK(output.size() == 1);
  }
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(request, kClosed, "request", false).ok());
  Q4T_CHECK(parser.Append("<tool_call>", &deltas).ok());
  Q4T_CHECK(
      !parser
           .Append(std::string(ChatResponseParser::kMaxToolBytes + 1, 'x'),
                   &deltas)
           .ok());
  ChatResponseParser many;
  Q4T_CHECK(many.Init(request, kClosed, "request", false).ok());
  for (size_t i = 0; i < ChatResponseParser::kMaxCalls; ++i) {
    deltas.clear();
    Q4T_CHECK(many.Append(Call(), &deltas).ok());
    Q4T_CHECK(deltas.size() == 1 && deltas[0].call.index == i);
  }
  deltas.clear();
  Q4T_CHECK(!many.Append(Call(), &deltas).ok());
  Q4T_CHECK(deltas.empty());
  return true;
}

Q4T_TEST(chat_response_request_definitions_are_checked) {
  for (
      const auto* tools :
      {R"([{"type":"function","function":{"name":"x","strict":"true"}}])",
       R"([{"type":"function","function":{"name":"x","parameters":[]}}])",
       R"([{"type":"function","function":{"name":"x","parameters":{"properties":[]}}}])",
       R"([{"type":"function","function":{"name":"x"}},{"type":"function","function":{"name":"x"}}])",
       R"([{"type":"custom","name":"x"}])"}) {
    Json request;
    Q4T_CHECK(
        q4t::io::ParseJson(
            std::string("{\"messages\":[],\"tools\":") + tools + '}', &request)
            .ok());
    ChatResponseParser parser;
    Q4T_CHECK(!parser.Init(request, kClosed, "request", true).ok());
  }
  return true;
}
