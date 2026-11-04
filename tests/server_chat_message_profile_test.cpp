#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_template.h"
#include "q4t/test.h"

#include <string>
#include <vector>

namespace {
using q4t::io::Json;
using q4t::server::RenderChatPrompt;
using q4t::server::ValidateChatContract;
using q4t::server::ValidateChatMessageProfile;

bool Read(const std::string& messages, Json* request) {
  return q4t::io::ParseJson("{\"messages\":" + messages +
                               ",\"enable_thinking\":false}",
                           request, true).ok();
}
}  // namespace

Q4T_TEST(chat_message_profile_developer_system_equivalence) {
  // Fixed native bytes, independent of the role-mapping implementation.
  const std::string expected =
      "<|im_start|>system\n只回复确认<|im_end|>\n"
      "<|im_start|>user\nhello<|im_end|>\n"
      "<|im_start|>assistant\n<think>\n\n</think>\n\n";
  for (const auto* role : {"system", "developer"}) {
    Json request;
    Q4T_CHECK(Read(std::string("[{\"role\":\"") + role +
                       R"(","content":[{"type":"text","text":"只回复确认"}]},)"
                       R"({"role":"user","content":"hello","name":null}])",
                   &request));
    const auto before = request.Find("messages")->array[0].GetString("role");
    std::string param = "stale", prompt;
    Q4T_CHECK(ValidateChatContract(request, "m", false, &param));
    Q4T_CHECK(param.empty());
    Q4T_CHECK(RenderChatPrompt(request, {" 只回复确认\u3000", "hello"},
                              &prompt));
    Q4T_CHECK(prompt == expected);
    Q4T_CHECK(request.Find("messages")->array[0].GetString("role") == before);
  }
  return true;
}

Q4T_TEST(chat_message_profile_rejects_unrepresentable_semantics) {
  const std::pair<const char*, const char*> cases[] = {
      {R"([{"role":"user"},{"role":"developer"}])", "messages[1].role"},
      {R"([{"role":"system"},{"role":"developer"}])", "messages[1].role"},
      {R"([{"role":"developer"},{"role":"system"}])", "messages[1].role"},
      {R"([{"role":"developer"},{"role":"developer"}])", "messages[1].role"},
      {R"([{"role":"system"},{"role":"system"}])", "messages[1].role"},
      {R"([{"role":"user","name":""}])", "messages[0].name"},
      {R"([{"role":"assistant","name":"alice"}])", "messages[0].name"},
      {R"([{"role":"tool","name":"lookup"}])", "messages[0].name"},
      {R"([{"role":"developer","name":42}])", "messages[0].name"},
      {R"([{"role":"function"}])", "messages[0].role"},
      {R"([{"role":null}])", "messages[0].role"},
      {R"([{}])", "messages[0].role"},
      {R"([null])", "messages[0]"}};
  for (const auto& [wire, expected] : cases) {
    Json request;
    Q4T_CHECK(Read(wire, &request));
    std::string param, rendered = "stale";
    Q4T_CHECK(!ValidateChatContract(request, "m", false, &param));
    Q4T_CHECK(param == expected);
    const auto& messages = *request.Find("messages");
    Q4T_CHECK(!ValidateChatMessageProfile(messages, &param));
    Q4T_CHECK(param == expected);
    Q4T_CHECK(!RenderChatPrompt(
        request, std::vector<std::string>(messages.array.size(), "x"),
        &rendered));
    Q4T_CHECK(rendered.empty());
  }
  return true;
}

Q4T_TEST(chat_message_profile_instruction_content_before_media) {
  for (const auto* role : {"system", "developer"}) {
    for (const auto* part : {
        R"({"type":"image_url","image_url":{"url":"invalid-base64"}})",
        R"({"video_frames":["invalid-base64"]})",
        R"({"type":"file","text":"lost"})",
        R"({"text":"x","image_url":"bad"})",
        R"({"type":"text","text":"x","unknown":true})", R"(null)"}) {
      Json request;
      Q4T_CHECK(Read(std::string("[{\"role\":\"") + role +
                         "\",\"content\":[" + part +
                         R"(]},{"role":"user","content":"x"}])",
                     &request));
      for (bool allow_media : {false, true}) {
        std::string param;
        Q4T_CHECK(!ValidateChatContract(request, "m", allow_media, &param));
        Q4T_CHECK(param == "messages[0].content[0]");
      }
    }
  }
  return true;
}

Q4T_TEST(chat_message_profile_preserves_history_rules) {
  Json request;
  Q4T_CHECK(Read(R"([{"role":"developer","content":"read"},
      {"role":"user","content":"query"},
      {"role":"assistant","reasoning_content":"why","tool_calls":[
        {"id":"a","type":"function","function":
          {"name":"lookup","arguments":"{}"}}]},
      {"role":"tool","tool_call_id":"a","content":"result","name":null}])",
                &request));
  Q4T_CHECK(ValidateChatContract(request, "m", false));
  std::string prompt;
  Q4T_CHECK(RenderChatPrompt(request, {"read", "query", "", "result"},
                            &prompt));
  Q4T_CHECK(prompt.find("<think>\nwhy\n</think>") != std::string::npos);
  Q4T_CHECK(prompt.find("<tool_response>\nresult\n</tool_response>") !=
            std::string::npos);
  // Developer does not bypass batch completeness or the real user requirement.
  for (auto& [key, value] : request.object)
    if (key == "messages") value.array.pop_back();
  Q4T_CHECK(!ValidateChatContract(request, "m", false));
  Q4T_CHECK(Read(R"([{"role":"developer","content":"read"}])", &request));
  Q4T_CHECK(!RenderChatPrompt(request, {"read"}, &prompt));
  return true;
}

Q4T_TEST(chat_reasoning_history_alias_contract) {
  const std::string prefix = R"([{"role":"user","content":"q"},)";
  std::string reference;
  for (const auto* fields : {
           R"("reasoning_content":"  why✅  ")",
           R"("reasoning":"  why✅  ")",
           R"("reasoning":"  why✅  ","reasoning_content":null)",
           R"("reasoning":null,"reasoning_content":"  why✅  ")",
           R"("reasoning":"  why✅  ","reasoning_content":"  why✅  ")"}) {
    Json request;
    Q4T_CHECK(Read(prefix + "{\"role\":\"assistant\",\"content\":\"a\"," +
                       fields + "}]", &request));
    std::string param, prompt;
    Q4T_CHECK(ValidateChatContract(request, "m", false, &param));
    Q4T_CHECK(param.empty());
    Q4T_CHECK(RenderChatPrompt(request, {"q", "a"}, &prompt));
    if (reference.empty()) reference = prompt;
    Q4T_CHECK(prompt == reference);
    Q4T_CHECK(prompt.find("<think>\nwhy✅\n</think>\n\na") != prompt.npos);
  }
  // Missing, null and empty all render the same single native reasoning slot.
  for (const auto* fields : {"", R"(,"reasoning":null)",
                             R"(,"reasoning_content":null)",
                             R"(,"reasoning":"","reasoning_content":null)"}) {
    Json request;
    Q4T_CHECK(Read(prefix + R"({"role":"assistant","content":"a")" +
                       fields + "}]", &request));
    Q4T_CHECK(ValidateChatContract(request, "m", false));
    std::string prompt;
    Q4T_CHECK(RenderChatPrompt(request, {"q", "a"}, &prompt));
    Q4T_CHECK(prompt.find("<think>\n\n</think>\n\na") != prompt.npos);
  }
  return true;
}

Q4T_TEST(chat_reasoning_history_conflicts_rejected_before_render) {
  const std::pair<const char*, const char*> cases[] = {
      {R"({"role":"assistant","reasoning":"a","reasoning_content":"b"})",
       "reasoning"},
      {R"({"role":"assistant","reasoning":1})", "reasoning"},
      {R"({"role":"assistant","reasoning_content":false})", "reasoning_content"},
      {R"({"role":"user","reasoning":null})", "reasoning"},
      {R"({"role":"tool","reasoning_content":"x"})", "reasoning_content"}};
  for (const auto& [message, field] : cases) {
    Json request;
    Q4T_CHECK(Read(std::string(R"([{"role":"user","content":"q"},)") +
                       message + ']', &request));
    std::string param, prompt;
    Q4T_CHECK(!ValidateChatContract(request, "m", false, &param));
    Q4T_CHECK(param == "messages[1]." + std::string(field));
    Q4T_CHECK(!RenderChatPrompt(request, {"q", "a"}, &prompt));
    Q4T_CHECK(prompt.empty());
  }
  return true;
}

Q4T_TEST(chat_reasoning_controls_require_parsed_messages) {
  for (const auto* field : {"reasoning_field", "include_reasoning"}) {
    const auto valid = std::string(field) == "reasoning_field"
                           ? "\"reasoning\"" : "false";
    for (const auto* body : {
             R"("messages":[{"role":"user","content":"q"}])",
             R"("messages":[{"role":"user","content":"q"}],"parse_response":false)",
             R"("prompt":"raw")"}) {
      for (const auto* value : {valid, "null", "17", "\"unknown\""}) {
        Json request;
        Q4T_CHECK(q4t::io::ParseJson(std::string("{") + body + ",\"" +
                                       field + "\":" + value + '}', &request));
        std::string param;
        const bool accepted = value == valid &&
            !request.Has("prompt") && request.GetBool("parse_response", true);
        Q4T_CHECK(ValidateChatContract(request, "m", false, &param).ok() ==
                  accepted);
        Q4T_CHECK(accepted ? param.empty() : param == field);
      }
    }
  }
  return true;
}
