#include "q4t/server/chat_contract.h"
#include "q4t/test.h"

#include <string>

namespace {
bool Accept(const std::string& input, bool media = false) {
  q4t::io::Json req;
  if (!q4t::io::ParseJson(input, &req).ok()) return false;
  return q4t::server::ValidateChatContract(
      req, "qwen3.8-flash-next", media).ok();
}
}  // namespace

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
           R"("repetition_penalty":1.1)", R"("stop":["END"])",
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
      R"({"role":"developer","content":"instruction"})",
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
