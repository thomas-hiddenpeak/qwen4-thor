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

Q4T_TEST(responses_input_malformed_tool_type_is_not_unresolved_capability) {
  for (const auto* definition : {"{}", "null", "42", R"({"type":null})",
                                 R"({"type":true})", R"({"type":""})"}) {
    Json tools, choice;
    ChatPlanError error;
    Q4T_CHECK(!NormalizeResponsesTools(
        Read(std::string("{\"tools\":[") + definition + "]}"), &tools, &choice,
        &error));
    Q4T_CHECK(error.http_status == 400 && error.code == "invalid_request" &&
              error.param.starts_with("tools[0]"));
  }
  return true;
}

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

Q4T_TEST(
    responses_namespace_definitions_preserve_public_identity_and_defaults) {
  auto request = Read(R"({"tools":[
    {"type":"namespace","name":"crm","description":"Customer records",
      "tools":[{"type":"function","name":"read"}]},
    {"type":"namespace","name":"support","description":"Ticket records",
      "tools":[{"type":"function","name":"read"},
               {"type":"custom","name":"query"}]},
    {"type":"function","name":"q4t_ns_public"}]})");
  Json tools, choice, effective;
  ChatPlanError error;
  Q4T_CHECK(
      NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
  Q4T_CHECK(tools.array.size() == 4 && effective.array.size() == 3);
  const auto& customer = *tools.array[0].Find("function");
  const auto& support = *tools.array[1].Find("function");
  Q4T_CHECK(customer.GetString("name") != support.GetString("name"));
  Q4T_CHECK(customer.GetString("description").find("Customer records") !=
            std::string::npos);
  Q4T_CHECK(customer.GetString("description").find("crm") != std::string::npos);
  const auto& original = effective.array[0].GetArray("tools")->array[0];
  Q4T_CHECK(original.GetString("name") == "read" && original.GetBool("strict"));
  Q4T_CHECK(original.Find("parameters") && !original.Has("namespace"));
  Q4T_CHECK(tools.array[2].Find("custom")->GetString("name") ==
            ResponsesToolModelName("support", "query", "custom"));
  Q4T_CHECK(tools.array[3].Find("function")->GetString("name") !=
            "q4t_ns_public");
  return true;
}

Q4T_TEST(responses_namespace_selection_is_explicit_and_never_arbitrary) {
  for (const bool allowed : {false, true}) {
    for (const auto* name : {"crm.read", "support.read", "read"}) {
      auto request = Read(R"({"tools":[
        {"type":"namespace","name":"crm","description":"customers",
         "tools":[{"type":"function","name":"read"}]},
        {"type":"namespace","name":"support","description":"tickets",
         "tools":[{"type":"function","name":"read"}]}]})");
      auto selector =
          Read(std::string(R"({"type":"function","name":")") + name + R"("})");
      if (allowed)
        selector =
            Read(R"({"type":"allowed_tools","mode":"required","tools":[)" +
                 OutputJson(selector) + "]}");
      request.object.emplace_back("tool_choice", selector);
      Json tools, choice;
      ChatPlanError error;
      auto status = NormalizeResponsesTools(request, &tools, &choice, &error);
      if (std::string(name) == "read") {
        Q4T_CHECK(!status && error.http_status == 400 &&
                  error.param == (allowed ? "tool_choice.tools[0].name"
                                          : "tool_choice.name"));
      } else {
        Q4T_CHECK(status);
        const auto& picked =
            allowed ? choice.Find("allowed_tools")->GetArray("tools")->array[0]
                    : choice;
        const auto space =
            std::string(name).substr(0, std::string(name).find('.'));
        Q4T_CHECK(picked.Find("function")->GetString("name") ==
                  ResponsesToolModelName(space, "read", "function"));
      }
    }
  }
  return true;
}

Q4T_TEST(responses_namespace_paths_duplicates_and_callable_budget) {
  for (
      const auto& [body, path] :
      std::vector<std::pair<std::string, std::string>>{
          {R"({"type":"namespace","name":"crm","description":"x","tools":[]})",
           "tools[0].tools"},
          {R"({"type":"namespace","name":"crm","description":null,"tools":[{"type":"function","name":"x"}]})",
           "tools[0].description"},
          {R"({"type":"namespace","name":"crm","description":"x","tools":[{"type":"file_search"}]})",
           "tools[0].tools[0].type"},
          {R"({"type":"namespace","name":"crm","description":"x","tools":[{"type":"function","name":"x"},{"type":"custom","name":"x"}]})",
           "tools[0].tools[1].name"},
          {R"({"type":"namespace","name":"crm","description":"x","tools":[{"type":"function","name":"x","description":42}]})",
           "tools[0].tools[0].description"}}) {
    Json tools, choice;
    ChatPlanError error;
    Q4T_CHECK(!NormalizeResponsesTools(Read("{\"tools\":[" + body + "]}"),
                                       &tools, &choice, &error));
    Q4T_CHECK(error.param == path && error.http_status == 400);
  }
  auto request = Read(R"({"tools":[{"type":"namespace","name":"crm",
    "description":"bounded","tools":[]}]})");
  auto& children = request.object[0].second.array[0].object[3].second.array;
  for (size_t i = 0; i < 129; ++i) {
    children.push_back(
        Read(R"({"type":"function","name":"f)" + std::to_string(i) + R"("})"));
    if (i < 127) continue;
    Json tools, choice;
    ChatPlanError error;
    const auto result =
        NormalizeResponsesTools(request, &tools, &choice, &error);
    Q4T_CHECK(i == 127 ? result.ok()
                       : !result && error.http_status == 503 &&
                             error.code == "tool_resource_exhausted");
  }
  return true;
}

Q4T_TEST(responses_namespace_history_maps_without_current_declarations) {
  auto request = Read(R"({"input":[
    {"type":"function_call","namespace":"crm","name":"read",
     "call_id":"a","arguments":"{}"},
    {"type":"custom_tool_call","namespace":"support","name":"read",
     "call_id":"b","input":"raw\n中文"},
    {"type":"custom_tool_call_output","call_id":"b","output":"ticket"},
    {"type":"function_call_output","call_id":"a","output":"customer"}]})");
  ResponsesInputPlan plan;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesInput(request, &plan, &error));
  const auto& calls = plan.messages.array[0].GetArray("tool_calls")->array;
  Q4T_CHECK(calls[0].Find("function")->GetString("name") ==
            ResponsesToolModelName("crm", "read", "function"));
  Q4T_CHECK(calls[1].Find("custom")->GetString("name") ==
            ResponsesToolModelName("support", "read", "custom"));
  Q4T_CHECK(plan.items.array[0].GetString("namespace") == "crm");
  Q4T_CHECK(PlanToolResultOrder(plan.messages));
  auto& result = request.object[0].second.array[3];
  result.object.emplace_back("namespace", Read(R"("support")"));
  Q4T_CHECK(!NormalizeResponsesInput(request, &plan, &error));
  Q4T_CHECK(error.param == "input[3].namespace");
  return true;
}

Q4T_TEST(responses_namespace_native_search_preserves_original_binding_index) {
  auto request = Read(R"({"tools":[
    {"type":"namespace","name":"crm","description":"records","tools":[
      {"type":"function","name":"read"},
      {"type":"function","name":"q4t_file_search_1"}]},
    {"type":"file_search","vector_store_ids":["vs_project"]}],
    "tool_choice":{"type":"allowed_tools","mode":"required","tools":[
      {"type":"function","name":"crm.read"},{"type":"file_search"}]}})");
  Json tools, choice, effective;
  ChatPlanError error;
  Q4T_CHECK(
      NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
  Q4T_CHECK(tools.array.size() == 3 && effective.array.size() == 2);
  Q4T_CHECK(tools.array[2].Find("function")->GetString("name") ==
            "q4t_file_search_1");
  const auto& selected = choice.Find("allowed_tools")->GetArray("tools")->array;
  Q4T_CHECK(selected.size() == 2);
  Q4T_CHECK(selected[1].Find("function")->GetString("name") ==
            "q4t_file_search_1");
  OutputConstraints constraints;
  auto chat = Read(R"({"tools":[],"tool_choice":null,"messages":[]})");
  chat.object[0].second = tools;
  chat.object[1].second = choice;
  Q4T_CHECK(PlanOutputConstraints(chat, &constraints));
  return true;
}
