#include "q4t/server/chat_contract.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/chat_template.h"
#include "q4t/test.h"

#include <string>

namespace {
bool Accept(const std::string& input, bool media = false) {
  q4t::io::Json req;
  if (!q4t::io::ParseJson(input, &req, true).ok()) return false;
  return q4t::server::ValidateChatContract(
      req, "qwen3.8-flash-next", media).ok();
}
}  // namespace

Q4T_TEST(chat_contract_error_param_is_explicit) {
  const std::pair<const char*, const char*> cases[] = {
      {R"({"prompt":"x","temperature":1})", "temperature"},
      {R"({"prompt":"x","max_completion_tokens":0})", "max_completion_tokens"},
      {R"({"prompt":"x","model":"wrong"})", "model"},
      {R"({"prompt":"x","stream_options":{"include_usage":1}})",
       "stream_options.include_usage"},
      {R"({"messages":[{"role":"function","content":"x"}]})",
       "messages[0].role"},
      {R"({"prompt":"x","unknown":true})", "unknown"},
      {R"([])", "body"}};
  for (const auto& [wire, expected] : cases) {
    q4t::io::Json request;
    Q4T_CHECK(q4t::io::ParseJson(wire, &request, true));
    std::string param = "stale";
    Q4T_CHECK(!q4t::server::ValidateChatContract(request, "qwen3.8-flash-next",
                                                 false, &param));
    Q4T_CHECK(param == expected);
  }
  q4t::io::Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"prompt":"x"})", &request, true));
  std::string param = "stale";
  Q4T_CHECK(q4t::server::ValidateChatContract(request, "qwen3.8-flash-next",
                                              false, &param));
  Q4T_CHECK(param.empty());
  return true;
}

Q4T_TEST(chat_contract_greedy_neutral_options) {
  Q4T_CHECK(Accept(R"({"prompt":"hello","temperature":0,"top_p":1,
    "n":1,"seed":20260920,"presence_penalty":0,"frequency_penalty":0,
    "repetition_penalty":1,"stop":[],"logprobs":false,"top_logprobs":0,
    "logit_bias":{},"response_format":{"type":"text"},"user":"test",
    "model":"qwen3.8-flash-next","stream":true,
    "stream_options":{"include_usage":true},"max_tokens":1})"));
  Q4T_CHECK(Accept(R"({"prompt":"hello","seed":null,"stop":null})"));
  Q4T_CHECK(Accept(R"({"prompt":"hello","max_tokens":2147483647})"));
  return true;
}

Q4T_TEST(chat_contract_rejects_unimplemented_constraints) {
  for (const char* field : {
           R"("temperature":0.5)", R"("top_p":0.9)", R"("n":2)",
           R"("frequency_penalty":1)", R"("presence_penalty":1)",
           R"("repetition_penalty":1.1)",
           R"("logprobs":true)", R"("top_logprobs":1)",
           R"("logit_bias":{"1":1})", R"("top_k":1)",
           R"("response_format":{"type":"json_object"})",
           R"("tool_choice":"required")", R"("parallel_tool_calls":false)",
           R"("temprature":0)"}) {
    Q4T_CHECK(!Accept(std::string("{\"prompt\":\"hello\",") + field + "}"));
  }
  return true;
}

Q4T_TEST(chat_contract_completion_token_budget) {
  for (const auto* fields : {
      R"("max_completion_tokens":1)",
      R"("max_completion_tokens":2147483647)",
      R"("max_completion_tokens":null,"max_tokens":10)",
      R"("max_completion_tokens":10,"max_tokens":null)",
      R"("max_completion_tokens":null,"max_tokens":null)"})
    Q4T_CHECK(Accept(std::string(R"({"prompt":"hello",)") + fields + "}"));
  for (const auto* fields : {
      R"("max_completion_tokens":0)", R"("max_completion_tokens":true)",
      R"("max_completion_tokens":1.5)", R"("max_completion_tokens":"10")",
      R"("max_completion_tokens":2147483648)",
      R"("max_completion_tokens":10,"max_tokens":10)",
      R"("max_completion_tokens":10,"max_tokens":20)"})
    Q4T_CHECK(!Accept(std::string(R"({"prompt":"hello",)") + fields + "}"));
  return true;
}

Q4T_TEST(chat_contract_no_silent_message_semantics) {
  for (const auto* message : {
      R"({"role":"user","content":"hello","name":"alice"})",
      R"({"role":"assistant","content":"x","refusal":"refused"})",
      R"({"role":"user","content":"x","reasoning_content":"lost"})",
      R"({"role":"user","content":"x","tool_calls":[]})",
      R"({"role":"user","content":"x","contnet":"lost"})",
      R"({"role":"user","content":[{"type":"file","text":"lost"}]})",
      R"({"role":"user","content":[{"type":"text","text":"x","extra":1}]})"})
    Q4T_CHECK(!Accept(std::string(R"({"messages":[)") + message + "]}"));
  Q4T_CHECK(Accept(R"({"messages":[{"role":"user","content":"hi"},
      {"role":"assistant","content":"hello","refusal":null}]})"));
  return true;
}

Q4T_TEST(chat_contract_rejects_wrong_types_and_ambiguous_input) {
  for (const char* bad : {
           R"({})", R"({"prompt":"x","messages":[]})",
           R"({"prompt":1})", R"({"messages":[]})",
           R"({"prompt":"x","stream":1})",
           R"({"prompt":"x","max_tokens":0})",
           R"({"prompt":"x","max_tokens":1.5})",
           R"({"prompt":"x","max_tokens":2147483648})",
           R"({"prompt":"x","max_tokens":true})",
           R"({"prompt":"x","temperature":false})",
           R"({"prompt":"x","seed":9007199254740992})",
           R"({"prompt":"x","seed":1.5})",
           R"({"prompt":"x","model":"other"})",
           R"({"prompt":"x","stream_options":{"include_usage":1}})",
           R"({"prompt":"x","stream_options":{"extra":true}})"}) {
    Q4T_CHECK(!Accept(bad));
  }
  return true;
}

Q4T_TEST(chat_contract_template_options_are_explicit) {
  Q4T_CHECK(Accept(R"({"messages":[{"role":"user","content":"x"}],
    "tools":[],"chat_template_kwargs":{"enable_thinking":false,
    "preserve_thinking":true,"reasoning_effort":"low"}})"));
  Q4T_CHECK(!Accept(R"({"prompt":"x","enable_thinking":false})"));
  Q4T_CHECK(!Accept(R"({"messages":[{"role":"user","content":"x"}],
    "chat_template_kwargs":{"enable_thinking":false,
    "reasoning_effort":"invalid"}})"));
  Q4T_CHECK(!Accept(R"({"messages":[{"role":"user","content":"x"}],
    "chat_template_kwargs":{"unknown":true}})"));
  return true;
}

Q4T_TEST(chat_contract_media_rejected_before_decoding) {
  const std::string image = R"({"messages":[{"role":"user","content":[
    {"type":"image_url","image_url":{"url":"not even valid base64"}}]}]})";
  const std::string video = R"({"messages":[{"role":"user","content":[
    {"video_frames":["invalid"]}]}]})";
  Q4T_CHECK(!Accept(image));
  Q4T_CHECK(!Accept(video));
  // Explicit experimental mode only passes this policy gate, not decoding.
  Q4T_CHECK(Accept(image, true));
  Q4T_CHECK(Accept(video, true));
  Q4T_CHECK(Accept(R"({"messages":[{"role":"user","content":[
    {"type":"text","text":"hello"}]}]})"));
  return true;
}

Q4T_TEST(chat_contract_parsed_response_options) {
  Q4T_CHECK(Accept(R"({"messages":[{"role":"user","content":"x"}],
    "parse_response":true,"tool_choice":"auto","parallel_tool_calls":true})"));
  Q4T_CHECK(Accept(R"({"messages":[{"role":"user","content":"x"}],
    "parse_response":false})"));
  Q4T_CHECK(Accept(R"({"prompt":"x","parse_response":false})"));
  for (const char* bad : {
      R"({"prompt":"x","parse_response":true})",
      R"({"messages":[],"parse_response":"false"})",
      R"({"messages":[],"parse_response":false,"tool_choice":"auto"})",
      R"({"messages":[],"tool_choice":"none"})",
      R"({"messages":[],"tool_choice":"required"})",
      R"({"messages":[],"parallel_tool_calls":false})"})
    Q4T_CHECK(!Accept(bad));
  return true;
}

Q4T_TEST(chat_contract_tool_definitions_all_modes) {
  for (const auto* mode : {"", R"(,"parse_response":false)",
                           R"(,"tool_choice":"auto")",
                           R"(,"tool_choice":"none")"}) {
    for (const auto* tool : {
        R"({"type":"function","strict":true,"function":{"name":"f"}})",
        R"({"type":"function","function":{"name":"f","paramaters":{}}})",
        R"({"type":"function","function":{"name":"f","description":null}})",
        R"({"type":"function","function":{"name":"f","parameters":null}})",
        R"({"type":"function","function":{"name":"f","strict":1}})",
        R"({"type":"custom","function":{"name":"f"}})",
        R"({"function":{"name":"f"}})",
        R"({"type":"function","function":{"name":"bad\u0000name"}})",
        R"({"type":"function","function":{"name":"f"},"extra":false})"}) {
      const auto text = std::string(R"({"messages":[{"role":"user",)"
                                   R"("content":"q"}],"tools":[)") +
                        tool + ']' + mode + '}';
      Q4T_CHECK(!Accept(text));
      q4t::io::Json request;
      Q4T_CHECK(q4t::io::ParseJson(text, &request, true).ok());
      std::string prompt = "clear on failure";
      Q4T_CHECK(!q4t::server::RenderChatPrompt(request, {"q"}, &prompt).ok());
      Q4T_CHECK(prompt.empty());
      q4t::server::ChatResponseParser parser;
      Q4T_CHECK(!parser.Init(request,
          "<|im_start|>assistant\n<think>\n\n</think>\n\n", "r", true).ok());
    }
  }
  // An advisory schema can retain ordinary JSON Schema keywords. Only the
  // constrained mode promises validation against the finite schema profile.
  Q4T_CHECK(Accept(R"({"messages":[{"role":"user","content":"q"}],
    "tools":[{"type":"function","function":{"name":"f","strict":null,
    "parameters":{"type":"object","properties":{"id":{"type":"number",
    "default":1.0}}}}}]})"));
  Q4T_CHECK(!Accept(R"({"messages":[{"role":"user","content":"q"}],
    "tools":[{"type":"function","function":{"name":"f"}},
    {"type":"function","function":{"name":"f"}}]})"));
  return true;
}

Q4T_TEST(chat_contract_nullable_defaults_are_field_specific) {
  Q4T_CHECK(Accept(R"({"prompt":"q","temperature":null,"top_p":null,
    "n":null,"frequency_penalty":null,"presence_penalty":null,"stream":null,
    "stream_options":null,"user":null})"));
  for (const auto* key : {"model", "repetition_penalty", "request_id",
                         "cancel_token", "request_timeout_ms",
                         "prefix_cache_write_tokens", "parse_response"})
    Q4T_CHECK(!Accept(std::string(R"({"prompt":"q",")") + key + "\":null}"));
  for (const auto* field : {R"("temperature":"0")", R"("top_p":false)",
                           R"("n":[])" , R"("stream_options":[])"})
    Q4T_CHECK(!Accept(std::string(R"({"prompt":"q",)") + field + '}'));
  return true;
}

Q4T_TEST(chat_contract_exact_numeric_spelling) {
  for (const auto* fields : {
      R"("max_tokens":1.0000000000000001)",
      R"("max_completion_tokens":2147483647.00000001)",
      R"("request_timeout_ms":1.0000000000000001)",
      R"("prefix_cache_write_tokens":1024.00000000000001)",
      R"("seed":9007199254740991.1)", R"("seed":1.0000000000000001)",
      R"("n":1.0000000000000001)", R"("top_p":1.0000000000000001)",
      R"("temperature":1e-400)", R"("top_logprobs":1e-400)"})
    Q4T_CHECK(!Accept(std::string(R"({"prompt":"q",)") + fields + '}'));
  for (const auto* fields : {
      R"("max_tokens":1.0000000000000000)", R"("max_tokens":10e-1)",
      R"("max_completion_tokens":21474836470e-1)",
      R"("request_timeout_ms":1e3)", R"("seed":-9007199254740991)",
      R"("temperature":-0.0)", R"("n":1e0)"})
    Q4T_CHECK(Accept(std::string(R"({"prompt":"q",)") + fields + '}'));
  return true;
}

Q4T_TEST(chat_contract_cancellation_shape_and_credentials) {
  const std::string key(64, 'a');
  const std::string fields = R"("request_id":"task-1","cancel_token":")" +
                             key + '"';
  q4t::io::Json req;
  Q4T_CHECK(q4t::io::ParseJson('{' + fields + '}', &req).ok());
  Q4T_CHECK(q4t::server::ValidateCancelContract(req).ok());
  Q4T_CHECK(Accept(R"({"prompt":"q",)" + fields + '}'));
  for (const auto& text : std::initializer_list<std::string>{
      "{}", '{' + fields + R"(,"dry_run":true})",
      '{' + fields + R"(,"reason":"ignore"})",
      R"({"cancel_token":")" + key + R"("})",
      R"({"request_id":null,"cancel_token":")" + key + R"("})",
      R"({"request_id":"chatcmpl-auto-1","cancel_token":")" + key + R"("})",
      R"({"request_id":"bad/id","cancel_token":")" + key + R"("})",
      R"({"request_id":"x","cancel_token":"A"})",
      R"({"request_id":"x","cancel_token":null})"}) {
    Q4T_CHECK(q4t::io::ParseJson(text, &req).ok());
    Q4T_CHECK(!q4t::server::ValidateCancelContract(req).ok());
  }
  Q4T_CHECK(!Accept(R"({"prompt":"q","request_id":"x"})"));
  Q4T_CHECK(!Accept(R"({"prompt":"q","cancel_token":")" + key + R"("})"));
  for (const auto* number : {"null", "true", "0", "10800001", "1.1"})
    Q4T_CHECK(!Accept(std::string(R"({"prompt":"q","request_timeout_ms":)") +
                      number + '}'));
  return true;
}
