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
bool CheckWire(const Json& message, const std::vector<ResponseDelta>& deltas,
               const std::string& reasoning_field = "reasoning_content",
               bool hidden = false) {
  std::string content, reasoning;
  const auto other =
      reasoning_field == "reasoning" ? "reasoning_content" : "reasoning";
  Q4T_CHECK(!message.Has(other));
  Q4T_CHECK(!hidden || !message.Has(reasoning_field));
  std::vector<q4t::server::ResponseToolCall> calls;
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
    Q4T_CHECK(!field->Has(other));
    Q4T_CHECK(!hidden || !field->Has(reasoning_field));
    reasoning += field->GetString(reasoning_field);
    if (const auto* tools = field->GetArray("tool_calls")) {
      for (const auto& call : tools->array) {
        const auto index = call.GetInt("index", -1);
        Q4T_CHECK(index >= 0 && index <= static_cast<int64_t>(calls.size()));
        const auto* fn = call.Find("function");
        Q4T_CHECK(fn);
        if (index == static_cast<int64_t>(calls.size())) {
          Q4T_CHECK(call.GetString("type") == "function");
          Q4T_CHECK(call.GetString("id") ==
                    "call_request_" + std::to_string(calls.size()));
          calls.push_back({static_cast<size_t>(index),
                           call.GetString("id"),
                           fn->GetString("name"),
                           {}});
        } else {
          Q4T_CHECK(!call.Has("id") && !call.Has("type") && !fn->Has("name"));
        }
        calls[index].arguments += fn->GetString("arguments");
      }
    }
  }
  Q4T_CHECK(content == message.GetString("content"));
  Q4T_CHECK(reasoning == message.GetString(reasoning_field));
  const auto* expected = message.GetArray("tool_calls");
  Q4T_CHECK(calls.size() == (expected ? expected->array.size() : 0));
  for (size_t i = 0; i < calls.size(); ++i) {
    const auto& want = expected->array[i];
    Q4T_CHECK(calls[i].id == want.GetString("id"));
    const auto* wanted_fn = want.Find("function");
    Q4T_CHECK(calls[i].name == wanted_fn->GetString("name"));
    Q4T_CHECK(calls[i].arguments == wanted_fn->GetString("arguments"));
    Json arguments;
    Q4T_CHECK(q4t::io::ParseJson(calls[i].arguments, &arguments).ok() &&
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
  const auto field = request.GetString("reasoning_field", "reasoning_content");
  const bool hidden = !request.GetBool("include_reasoning", true);
  Q4T_CHECK(q4t::io::ParseJson(expected, &message).ok());
  Q4T_CHECK(message.GetString("content") == expected_content);
  Q4T_CHECK(message.GetString(field) == expected_reasoning);
  const auto* calls = message.GetArray("tool_calls");
  Q4T_CHECK((calls ? calls->array.size() : 0) == expected_calls);
  Q4T_CHECK(CheckWire(message, deltas, field, hidden));
  for (size_t split = 0; split <= text.size(); ++split) {
    ChatResponseParser stream;
    q4t::server::detail::Utf8Stream utf8;
    deltas.clear();
    Q4T_CHECK(stream.Init(request, prefix, "request", false).ok());
    Q4T_CHECK(stream.Append(utf8.Append(text.substr(0, split)), &deltas).ok());
    Q4T_CHECK(stream.Append(utf8.Append(text.substr(split)), &deltas).ok());
    Q4T_CHECK(stream.Append(utf8.Finish(), &deltas).ok());
    Q4T_CHECK(stream.Finish(false, &deltas).ok());
    Q4T_CHECK(CheckWire(message, deltas, field, hidden));
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
  Q4T_CHECK(CheckWire(message, deltas, field, hidden));
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

Q4T_TEST(chat_response_tool_fragments_wait_for_validated_terminal) {
  const auto request = Request();
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(request, kClosed, "request", true).ok());
  Q4T_CHECK(
      parser.Append("<tool_call><function=noop></function>", &deltas).ok());
  Q4T_CHECK(deltas.size() == 1 && !parser.HasToolCalls());
  Q4T_CHECK(deltas[0].tool_start && deltas[0].call.arguments == "{");
  Q4T_CHECK(parser.Append("</tool_call>", &deltas).ok());
  Q4T_CHECK(deltas.size() == 2 && parser.HasToolCalls());
  Q4T_CHECK(!deltas[1].tool_start && deltas[1].call.arguments == "}");
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
      Q4T_CHECK(!parser.HasToolCalls());
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
    Q4T_CHECK(!output.empty() && !boundary.HasToolCalls());
    Q4T_CHECK(
        boundary
            .Append(
                std::string("</tool_call>").substr(std::string(partial).size()),
                &output)
            .ok());
    Q4T_CHECK(boundary.Finish(false, &output).ok());
    Q4T_CHECK(output.size() > 1 && boundary.HasToolCalls());
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
    Q4T_CHECK(deltas.size() == 2 && deltas[0].call.index == i &&
              deltas[1].call.index == i);
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

Q4T_TEST(chat_response_reasoning_selection_and_hidden_splits) {
  const std::string reason = "秘密分析✅<tool_call>推理示例</tool_call>";
  const std::string answer = "正文中说思考不是隐藏区✅";
  for (const auto* field : {"reasoning", "reasoning_content"}) {
    for (bool include : {false, true}) {
      auto request = Request();
      Json options;
      Q4T_CHECK(q4t::io::ParseJson(
          std::string("{\"reasoning_field\":\"") + field +
              "\",\"include_reasoning\":" + (include ? "true}" : "false}"),
          &options));
      for (const auto& item : options.object) request.object.push_back(item);
      const auto expected = include ? reason : "";
      Q4T_CHECK(EverySplit(request, reason + "</think>" + answer + Call(),
                           kThinking, answer, expected, 1));
      Q4T_CHECK(EverySplit(request, "<think>" + reason + "</think>" + answer,
                           kClosed, answer, expected, 0));
      // A second reasoning region must follow the same hiding/field contract.
      Q4T_CHECK(EverySplit(
          request,
          reason + "</think>" + answer + "<think>" + reason + "</think>",
          kThinking, answer, expected + expected, 0));
    }
  }
  return true;
}

Q4T_TEST(chat_response_hidden_reasoning_preserves_failure_and_limits) {
  Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"messages":[],"include_reasoning":false})",
                               &request));
  for (bool collect : {false, true}) {
    for (bool limited : {false, true}) {
      ChatResponseParser parser;
      std::vector<ResponseDelta> deltas;
      Q4T_CHECK(parser.Init(request, kThinking, "r", collect));
      Q4T_CHECK(parser.Append("private✅</thi", &deltas));
      Q4T_CHECK(deltas.empty());
      Q4T_CHECK(parser.Finish(limited, &deltas).ok() == limited);
      Q4T_CHECK(deltas.empty());
      Q4T_CHECK(parser.MessageJson() == R"({"role":"assistant","content":""})");
    }
  }
  Q4T_CHECK(q4t::io::ParseJson(R"({"messages":[],"include_reasoning":false,
      "response_format":{"type":"json_object"}})",
                               &request));
  for (const auto* payload :
       {R"({"text":"<think>literal</think>✅"})", R"({"text":)", "[]"}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kThinking, "r", true));
    Q4T_CHECK(parser.Append(std::string("private</think>") + payload, &deltas));
    Q4T_CHECK(deltas.empty());  // Strict payload stays deferred until valid.
    const bool valid = std::string(payload).ends_with("}");
    Q4T_CHECK(parser.Finish(false, &deltas).ok() == valid);
    if (valid) {
      Json message;
      Q4T_CHECK(q4t::io::ParseJson(parser.MessageJson(), &message));
      Q4T_CHECK(message.GetString("content") == payload);
      Q4T_CHECK(CheckWire(message, deltas, "reasoning_content", true));
    } else {
      Q4T_CHECK(deltas.empty());
    }
  }
  ChatResponseParser bounded;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(bounded.Init(request, kThinking, "r", false));
  Q4T_CHECK(!bounded.Append(
      std::string(ChatResponseParser::kMaxToolBytes + 1, 'x'), &deltas));
  Q4T_CHECK(deltas.empty());
  return true;
}

Q4T_TEST(chat_response_string_arguments_stream_before_parameter_close) {
  auto request = Request();
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(request, kClosed, "request", true));
  Q4T_CHECK(
      parser.Append("<tool_call><function=lookup><parameter=text>\r", &deltas));
  Q4T_CHECK(!parser.HasToolCalls() && deltas.size() == 2);
  Q4T_CHECK(parser.Append("\n中国✅\\\"abc", &deltas));
  std::string partial;
  for (const auto& delta : deltas) partial += delta.call.arguments;
  Q4T_CHECK(partial.find("中国✅\\\\\\\"") != partial.npos);
  Q4T_CHECK(partial.starts_with("{\"text\":\"") && !partial.ends_with('}'));
  Q4T_CHECK(!parser.HasToolCalls());
  Q4T_CHECK(parser.Append("def\r\n</parameter></function>", &deltas));
  Q4T_CHECK(!parser.HasToolCalls());
  Q4T_CHECK(parser.Append("</tool_call>", &deltas));
  Q4T_CHECK(parser.Finish(false, &deltas));
  Json message;
  Q4T_CHECK(q4t::io::ParseJson(parser.MessageJson(), &message));
  Q4T_CHECK(CheckWire(message, deltas));
  return true;
}

Q4T_TEST(chat_response_partial_error_has_no_successful_terminal) {
  const auto request = Request();
  for (const auto* suffix : {"", "</parameter><parameter=text>duplicate",
                             "</parameter></function>oops</tool_call>"}) {
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kClosed, "request", false));
    Q4T_CHECK(parser.Append(
        "<tool_call><function=lookup><parameter=text>partial", &deltas));
    Q4T_CHECK(!deltas.empty() && !parser.HasToolCalls());
    parser.Append(suffix, &deltas);
    Q4T_CHECK(!parser.Finish(false, &deltas));
    Q4T_CHECK(!parser.HasToolCalls());
  }
  return true;
}
