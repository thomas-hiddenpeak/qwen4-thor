#include "q4t/server/responses_input.h"

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/chat_tool_history.h"
#include "q4t/server/output_constraints.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
Json Read(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true, kSchemaJsonLimits);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
bool Input(const std::string& text, ResponsesInputPlan* plan,
           ChatPlanError* error) {
  return NormalizeResponsesInput(Read(text), plan, error).ok();
}
}  // namespace

Q4T_TEST(responses_input_instructions_roles_and_original_metadata) {
  const auto request = Read(R"({"instructions":"总规则",
    "input":[{"role":"developer","content":"补充规则"},
      {"role":"user","content":[{"type":"input_text","text":"用户内容"}]},
      {"type":"message","id":"msg_old","role":"assistant",
       "status":"completed","phase":"commentary",
       "content":[{"type":"output_text","text":"说明",
         "annotations":[],"logprobs":[]}]}]})");
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesInput(request, &plan, &error));
  Q4T_CHECK(plan.messages.array.size() == 4);
  Q4T_CHECK(plan.messages.array[0].GetString("role") == "developer");
  Q4T_CHECK(plan.messages.array[0].GetString("content") == "总规则");
  Q4T_CHECK(plan.items.array[2].GetString("id") == "msg_old");
  Q4T_CHECK(plan.items.array[2].GetString("phase") == "commentary");
  Q4T_CHECK(plan.messages.array[3].GetArray("content")->array[0].GetString(
                "text") == "[Response phase: commentary]\n");
  Q4T_CHECK(ValidateChatMessageProfile(plan.messages));
  std::string param = "messages[0].content";
  ResponsesInputErrorParam(plan, &param);
  Q4T_CHECK(param == "instructions");
  param = "messages[2].content[0].text";
  ResponsesInputErrorParam(plan, &param);
  Q4T_CHECK(param == "input[1].content[0].text");
  return true;
}

Q4T_TEST(responses_input_plain_text_empty_parts_and_refusal_order) {
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Input(R"({"input":"中文\n🙂"})", &plan, &error));
  Q4T_CHECK(plan.messages.array[0].GetString("content") == "中文\n🙂");
  Q4T_CHECK(Input(R"({"input":[{"role":"user","content":[]},
    {"role":"assistant","content":[
       {"type":"output_text","text":"之前"},
       {"type":"refusal","refusal":"拒绝部分"},
       {"type":"output_text","text":"之后"}]}]})",
                  &plan, &error));
  Q4T_CHECK(plan.messages.array.size() == 4);
  Q4T_CHECK(plan.messages.array[0].GetString("content") == "");
  Q4T_CHECK(plan.messages.array[2].GetArray("content")->array[0].GetString(
                "refusal") == "拒绝部分");
  Q4T_CHECK(ValidateChatMessageProfile(plan.messages));
  return true;
}

Q4T_TEST(responses_input_reasoning_parallel_calls_and_reverse_results) {
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Input(R"({"input":[{"role":"user","content":"资料查询"},
    {"type":"reasoning","id":"rs_a","summary":[],
     "content":[{"type":"reasoning_text","text":"需要两份资料"}]},
    {"type":"function_call","call_id":"a","name":"read","arguments":
     "{\"limit\":9007199254740993,\"path\":\"A\"}"},
    {"type":"function_call","call_id":"b","name":"read","arguments":
     "{\"path\":\"B\"}"},
    {"type":"function_call_output","call_id":"b","name":"read","output":"乙"},
    {"type":"function_call_output","call_id":"a","output":
     [{"type":"input_text","text":"甲"}]}]})",
                  &plan, &error));
  Q4T_CHECK(plan.messages.array.size() == 4);
  const auto& assistant = plan.messages.array[1];
  Q4T_CHECK(assistant.GetString("reasoning") == "需要两份资料");
  Q4T_CHECK(assistant.GetArray("tool_calls")
                ->array[0]
                .Find("function")
                ->GetString("arguments")
                .find("9007199254740993") != std::string::npos);
  std::vector<size_t> order;
  Q4T_CHECK(PlanToolResultOrder(plan.messages, &order));
  Q4T_CHECK(order == std::vector<size_t>({0, 1, 3, 2}));
  Q4T_CHECK(plan.items.array[4].GetString("call_id") == "b");
  return true;
}

Q4T_TEST(responses_input_custom_history_keeps_unstructured_bytes) {
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Input(R"({"input":[{"type":"custom_tool_call","call_id":"c",
    "name":"query","input":"not JSON\n中文 <think>"},
    {"type":"custom_tool_call_output","call_id":"c","output":"result"}]})",
                  &plan, &error));
  Q4T_CHECK(plan.messages.array[0]
                .GetArray("tool_calls")
                ->array[0]
                .Find("custom")
                ->GetString("input") == "not JSON\n中文 <think>");
  Q4T_CHECK(PlanToolResultOrder(plan.messages));
  return true;
}

Q4T_TEST(responses_input_invalid_tools_never_fabricate_results) {
  for (
      const auto* input :
      {R"([{"type":"function_call_output","call_id":"missing","output":"x"}])",
       R"([{"type":"function_call","call_id":"c","name":"read","arguments":"{}"}])",
       R"([{"type":"function_call","call_id":"c","name":"read","arguments":"{"}])",
       R"([{"type":"function_call","call_id":"c","name":"read","arguments":"{}"},
          {"type":"custom_tool_call_output","call_id":"c","output":"x"}])",
       R"([{"type":"function_call","call_id":"c","name":"read","arguments":"{}"},
          {"type":"function_call_output","call_id":"c","name":"wrong","output":"x"}])",
       R"([{"type":"function_call","call_id":"c","name":"read","arguments":"{}"},
          {"type":"function_call_output","call_id":"c","output":"x"},
          {"type":"function_call_output","call_id":"c","output":"x"}])",
       R"([{"type":"function_call","call_id":"c","name":"read","arguments":"{}","async":"yes"}])"}) {
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!Input("{\"input\":" + std::string(input) + '}', &plan, &error));
    Q4T_CHECK(error.http_status == 400 && error.param.starts_with("input["));
    Q4T_CHECK(plan.messages.IsNull());
  }
  return true;
}

Q4T_TEST(responses_input_image_detail_and_tool_media_not_dropped) {
  for (const auto* detail : {"auto", "low", "high", "original"}) {
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(
        Input("{\"input\":[{\"role\":\"user\",\"content\":["
              "{\"type\":\"input_image\",\"image_url\":\"data:image/"
              "png;base64,AA==\","
              "\"detail\":\"" +
                  std::string(detail) + "\"}]}]}",
              &plan, &error));
    const auto& part = plan.messages.array[0].GetArray("content")->array[0];
    Q4T_CHECK(part.Find("image_url")->GetString("detail") == detail);
  }
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Input(R"({"input":[{"type":"function_call","name":"view",
    "call_id":"c","arguments":"{}"},{"type":"function_call_output",
    "call_id":"c","output":[{"type":"input_image","image_url":
    "data:image/png;base64,AA==","detail":"high"}]}]})",
                  &plan, &error));
  Q4T_CHECK(plan.messages.array[1]
                .GetArray("content")
                ->array[0]
                .Find("image_url")
                ->GetString("detail") == "high");
  // This checks lossless adapter output, not media execution. A10 must consume
  // the actual detail and tool role; its backend validation is still required.
  return true;
}

Q4T_TEST(responses_input_unresolved_state_explicit_and_malformed_distinct) {
  for (
      const auto* input :
      {R"([{"type":"item_reference","id":"item_a"}])",
       R"([{"type":"reasoning","id":"rs_a","summary":[],"encrypted_content":"blob"}])",
       R"([{"role":"user","content":[{"type":"input_image","file_id":"file_a"}]}])"}) {
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!Input("{\"input\":" + std::string(input) + '}', &plan, &error));
    Q4T_CHECK(error.code == "responses_input_resolution_required");
    Q4T_CHECK(plan.messages.IsNull());
  }
  for (
      const auto* input :
      {"null", "42", R"([{"role":"user","type":null,"content":"x"}])",
       R"([{"role":"user","content":[{"type":"refusal","refusal":"x"}]}])",
       R"([{"role":"assistant","phase":"wrong","content":"x"}])",
       R"([{"role":"user","id":"same","content":"a"},{"role":"user","id":"same","content":"b"}])"}) {
    ResponsesInputPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!Input("{\"input\":" + std::string(input) + '}', &plan, &error));
    Q4T_CHECK(error.http_status == 400);
  }
  return true;
}

Q4T_TEST(responses_input_tools_resolve_strict_defaults_and_effective_schema) {
  const auto request = Read(R"({"tools":[{"type":"function","name":"read",
    "parameters":{"type":"object","properties":{"config":{"type":"object",
      "properties":{"value":{"type":["string","null"]}}}}}}]})");
  Json tools, choice, effective;
  ChatPlanError error;
  Q4T_CHECK(
      NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
  const auto& tool = effective.array[0];
  Q4T_CHECK(tool.GetBool("strict"));
  const auto& schema = *tool.Find("parameters");
  Q4T_CHECK(schema.GetArray("required")->array[0].str == "config");
  Q4T_CHECK(!schema.GetBool("additionalProperties", true));
  Q4T_CHECK(schema.Find("properties")
                ->Find("config")
                ->GetArray("required")
                ->array[0]
                .str == "value");
  Q4T_CHECK(ValidateOutputSchema(schema, true));
  Q4T_CHECK(!request.GetArray("tools")->array[0].Has("strict"));
  Q4T_CHECK(tools.array[0].Find("function")->GetBool("strict"));
  return true;
}

Q4T_TEST(responses_input_tools_strict_opt_out_fallback_and_explicit_failure) {
  Json tools, choice, effective;
  ChatPlanError error;
  for (const auto* strict : {"", ",\"strict\":false"}) {
    const auto request = Read(
        "{\"tools\":[{\"type\":\"function\",\"name\":\"map\","
        "\"parameters\":{\"type\":\"object\",\"additionalProperties\":true}" +
        std::string(strict) + "}]}");
    Q4T_CHECK(
        NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
    Q4T_CHECK(!effective.array[0].GetBool("strict", true));
    Q4T_CHECK(
        effective.array[0].Find("parameters")->GetBool("additionalProperties"));
  }
  Q4T_CHECK(!NormalizeResponsesTools(Read(R"({"tools":[{"type":"function",
    "name":"read","strict":true,"parameters":{"type":"object",
    "properties":{"x":{"type":"string"}}}}]})"),
                                     &tools, &choice, &error));
  Q4T_CHECK(error.param.starts_with("tools[0].parameters"));
  Q4T_CHECK(NormalizeResponsesTools(Read(R"({"tools":[{"type":"function",
    "name":"noop","parameters":null,"strict":null}]})"),
                                    &tools, &choice, &error, &effective));
  Q4T_CHECK(effective.array[0].GetBool("strict"));
  Q4T_CHECK(effective.array[0]
                .Find("parameters")
                ->GetArray("required")
                ->array.empty());
  return true;
}

Q4T_TEST(responses_input_tools_allowed_custom_and_source_errors) {
  auto request =
      Read(R"({"tools":[{"type":"function","name":"read","strict":false},
    {"type":"custom","name":"query","format":{"type":"text"}}],
    "tool_choice":{"type":"allowed_tools","mode":"required","tools":[
      {"type":"function","name":"read"},{"type":"custom","name":"query"}]}})");
  Json tools, choice;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error));
  auto chat = Read(R"({"messages":[{"role":"user","content":"lookup"}]})");
  chat.object.emplace_back("tools", tools);
  chat.object.emplace_back("tool_choice", choice);
  OutputConstraints constraints;
  Q4T_CHECK(PlanOutputConstraints(chat, &constraints));
  Q4T_CHECK(constraints.AllowsTool("read") && constraints.AllowsTool("query"));
  Q4T_CHECK(!constraints.AllowsTool("other"));
  Q4T_CHECK(!NormalizeResponsesTools(Read(R"({"tools":[{"type":"function",
      "name":"read","description":42}]})"),
                                     &tools, &choice, &error));
  Q4T_CHECK(error.param == "tools[0].description");
  Q4T_CHECK(!NormalizeResponsesTools(Read(R"({"tool_choice":null})"), &tools,
                                     &choice, &error));
  Q4T_CHECK(error.http_status == 400 && error.param == "tool_choice");
  return true;
}
