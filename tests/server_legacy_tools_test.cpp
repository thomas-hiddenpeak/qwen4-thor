#include "q4t/server/legacy_tools.h"

#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
constexpr auto kPrefix = "<|im_start|>assistant\n<think>\n\n</think>\n\n";

Json Parse(const std::string& text) {
  Json value;
  q4t::io::ParseJson(text, &value, true);
  return value;
}
Json Request(std::string_view extra = {}) {
  return Parse(
      std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                  R"("enable_thinking":false,"functions":[{)"
                  R"("name":"read","parameters":{"type":"object",)"
                  R"("properties":{"path":{"type":"string"}},)"
                  R"("required":["path"],"additionalProperties":false}}])") +
      std::string(extra) + '}');
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "m";
  config.structured = true;
  return config;
}
std::string Call(std::string_view text = "docs/MODEL.md") {
  return "<tool_call><function=read><parameter=path>\n" + std::string(text) +
         "\n</parameter></function></tool_call>";
}
}  // namespace

Q4T_TEST(legacy_tools_plan_template_and_original_immutable) {
  const auto request = Request(R"(,"function_call":{"name":"read"})");
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(request, Config(), &plan, &error));
  Q4T_CHECK(plan.legacy_output && plan.normalized_request.has_value());
  const auto& canonical = plan.Request(request);
  Q4T_CHECK(request.Has("functions") && !request.Has("tools"));
  Q4T_CHECK(!canonical.Has("functions") && canonical.Has("tools"));
  Q4T_CHECK(plan.constraints.active && !plan.constraints.parallel);
  std::string first, second;
  std::vector<VisionItem> items;
  Q4T_CHECK(PrepareChatPrompt(request, plan.constraints, &first, &items,
                              &error.param));
  Q4T_CHECK(PrepareChatPrompt(canonical, plan.constraints, &second, &items,
                              &error.param));
  Q4T_CHECK(first == second && first.find("read") != first.npos);
  return true;
}

Q4T_TEST(legacy_tools_error_paths_and_ambiguous_controls) {
  for (const auto& [extra, path] :
       std::vector<std::pair<std::string, std::string>>{
           {R"(,"function_call":{"name":"missing"})", "function_call.name"},
           {R"(,"function_call":"required")", "function_call"},
           {R"(,"function_call":{"name":"read","extra":1})",
            "function_call.extra"},
           {R"(,"tools":[])", "tools"},
           {R"(,"tool_choice":"auto")", "tool_choice"},
           {R"(,"parallel_tool_calls":true)", "parallel_tool_calls"}}) {
    ChatRequestPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!PlanChatRequest(Request(extra), Config(), &plan, &error));
    Q4T_CHECK(error.param == path);
  }
  const auto request = Parse(R"({"messages":[{"role":"user","content":"q"}],
    "functions":[{"name":"read","parameters":"bad"}]})");
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanChatRequest(request, Config(), &plan, &error));
  Q4T_CHECK(error.param == "functions[0].parameters");
  return true;
}

Q4T_TEST(legacy_tools_history_roundtrip_and_modern_migration) {
  const auto request = Parse(R"({"messages":[{"role":"user","content":"q"},
    {"role":"assistant","function_call":{"name":"read","arguments":"{}"}},
    {"role":"function","name":"read","content":"真实记录"},
    {"role":"assistant","function_call":{"name":"read","arguments":"{}"}},
    {"role":"function","name":"read","content":null}],
    "tools":[{"type":"function","function":{"name":"read"}}],
    "enable_thinking":false})");
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(request, Config(), &plan, &error));
  Q4T_CHECK(!plan.legacy_output);
  Q4T_CHECK(
      ValidateChatMessageProfile(*request.Find("messages"), &error.param));
  const auto* messages = plan.Request(request).GetArray("messages");
  Q4T_CHECK(messages && messages->array.size() == 5);
  Q4T_CHECK(messages->array[2].GetString("tool_call_id") == "q4t_legacy_1");
  Q4T_CHECK(messages->array[4].GetString("tool_call_id") == "q4t_legacy_3");
  std::string prompt;
  std::vector<VisionItem> items;
  Q4T_CHECK(PrepareChatPrompt(request, plan.constraints, &prompt, &items,
                              &error.param));
  Q4T_CHECK(prompt.find("真实记录") != prompt.npos);
  Q4T_CHECK(prompt.find("q4t_legacy_") == prompt.npos);
  return true;
}

Q4T_TEST(legacy_tools_history_rejects_missing_or_wrong_result) {
  for (
      const auto& history :
      {R"([{"role":"function","name":"read","content":"x"}])",
       R"([{"role":"assistant","function_call":{"name":"read","arguments":"{}"}}])",
       R"([{"role":"assistant","function_call":{"name":"read","arguments":"{}"}},
               {"role":"function","name":"wrong","content":"x"}])",
       R"([{"role":"assistant","function_call":{"name":"read","arguments":"{}"}},
               {"role":"user","content":"interrupted"}])",
       R"([{"role":"user","content":"q","function_call":null}])"}) {
    LegacyToolRequest normalized;
    std::string error;
    Q4T_CHECK(!NormalizeLegacyTools(
        Parse(std::string("{\"messages\":") + history + '}'), &normalized,
        &error));
    Q4T_CHECK(error.starts_with("messages"));
  }
  return true;
}

Q4T_TEST(legacy_tools_incremental_response_every_split) {
  const auto request = Request(R"(,"function_call":{"name":"read"})");
  const auto call = Call("中国\"\\path");
  for (size_t split = 0; split <= call.size(); ++split) {
    ChatResponseParser parser;
    detail::Utf8Stream utf8;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, kPrefix, "legacy", true));
    Q4T_CHECK(parser.Append(utf8.Append(call.substr(0, split)), &deltas));
    Q4T_CHECK(parser.Append(utf8.Append(call.substr(split)), &deltas));
    Q4T_CHECK(parser.Append(utf8.Finish(), &deltas));
    Q4T_CHECK(parser.Finish(false, &deltas));
    Q4T_CHECK(std::string_view(parser.ToolFinishReason()) == "function_call");
    const auto message = Parse(parser.MessageJson());
    Q4T_CHECK(!message.Has("tool_calls"));
    const auto* expected = message.Find("function_call");
    Q4T_CHECK(expected && expected->GetString("name") == "read");
    std::string arguments, name;
    for (const auto& delta : deltas) {
      const auto value = Parse(delta.ToJson());
      Q4T_CHECK(!value.Has("tool_calls"));
      if (const auto* fn = value.Find("function_call")) {
        Q4T_CHECK(!fn->Has("id") && !fn->Has("index"));
        name += fn->GetString("name");
        arguments += fn->GetString("arguments");
      }
    }
    Q4T_CHECK(name == "read" && arguments == expected->GetString("arguments"));
    Q4T_CHECK(Parse(arguments).GetString("path") == "中国\"\\path");
  }
  return true;
}

Q4T_TEST(legacy_tools_truncation_and_second_call_are_not_success) {
  const auto request = Request(R"(,"function_call":{"name":"read"})");
  ChatResponseParser parser;
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Init(request, kPrefix, "legacy", true));
  Q4T_CHECK(
      parser.Append("<tool_call><function=read><parameter=path>abc", &deltas));
  Q4T_CHECK(!deltas.empty() && !parser.HasToolCalls());
  Q4T_CHECK(!parser.Finish(true, &deltas));
  ChatResponseParser multiple;
  Q4T_CHECK(multiple.Init(request, kPrefix, "legacy", true));
  Q4T_CHECK(!multiple.Append(Call() + Call(), &deltas));
  return true;
}

Q4T_TEST(legacy_tools_null_controls_and_new_output_remain_modern) {
  const auto request = Parse(R"({"messages":[{"role":"user","content":"q"}],
    "functions":null,"function_call":null,"enable_thinking":false})");
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(request, Config(), &plan, &error));
  Q4T_CHECK(!plan.legacy_output && !plan.constraints.active);
  Q4T_CHECK(PlanChatRequest(Request(R"(,"tool_choice":null)"), Config(), &plan,
                            &error));
  Q4T_CHECK(plan.legacy_output);
  return true;
}

Q4T_TEST(legacy_tools_choice_wire_finish_and_usage) {
  ChatChoices choices(1, false);
  Q4T_CHECK(choices.Add(0, R"({"role":"assistant","content":null,
    "function_call":{"name":"read","arguments":"{}"}})",
                        "function_call", "null", 20, 8, 0, false, -1, 0, 0));
  std::string output;
  Q4T_CHECK(choices.Json("legacy", "m", 42, &output));
  const auto value = Parse(output);
  Q4T_CHECK(value.Find("choices")->array[0].GetString("finish_reason") ==
            "function_call");
  Q4T_CHECK(value.Find("usage")->GetInt("total_tokens") == 28);
  return true;
}
