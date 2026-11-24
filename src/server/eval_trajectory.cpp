#include "eval_trajectory.h"

#include <algorithm>
#include <unordered_map>

#include "eval_run_state.h"
#include "q4t/server/legacy_tools.h"

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;

void Content(Json* content) {
  if (content->IsObject()) *content = Array({*content});
  for (auto& part : content->array) {
    const auto type = part.GetString("type");
    if (part.IsString()) {
      part = Object({{"type", String("input_text")}, {"text", part}});
    } else if (type == "text") {
      Set(&part, "type", String("input_text"));
    } else if (type == "image_url") {
      const auto* image = part.Find("image_url");
      if (!image || !image->IsObject() || !image->Find("url")) continue;
      auto mapped = Object({{"type", String("input_image")},
                            {"image_url", *image->Find("url")}});
      if (const auto* detail = image->Find("detail"))
        Set(&mapped, "detail", *detail);
      if (const auto* boundary = part.Find("prompt_cache_breakpoint"))
        Set(&mapped, "prompt_cache_breakpoint", *boundary);
      part = std::move(mapped);
    }
  }
}

ObjectReply ExpandCall(const Json& call, Json* item) {
  const auto type = call.GetString("type");
  const bool custom = type == "custom";
  const auto id = call.GetString("id");
  const auto* payload = call.Find(type);
  const auto key = custom ? "input" : "arguments";
  if ((!custom && type != "function") || id.empty() || !payload ||
      payload->GetString("name").empty() || !payload->Find(key) ||
      !payload->Find(key)->IsString())
    return BadInput("input_messages", "Invalid historical Chat tool call");
  *item =
      Object({{"type", String(custom ? "custom_tool_call" : "function_call")},
              {"call_id", String(id)},
              {"name", *payload->Find("name")},
              {key, *payload->Find(key)}});
  return {};
}
}  // namespace

ObjectReply NormalizeEvalTrajectory(Json* input) {
  if (!input->IsArray()) return {};
  const bool legacy =
      std::any_of(input->array.begin(), input->array.end(), [](const Json& m) {
        return m.Has("function_call") || m.GetString("role") == "function";
      });
  if (legacy) {
    LegacyToolRequest normalized;
    auto status =
        NormalizeLegacyTools(Object({{"messages", *input}}), &normalized);
    if (!status) return BadInput("input_messages", status.message());
    *input = std::move(Field(normalized.request, "messages"));
  }
  auto result = Array();
  std::unordered_map<std::string, bool> custom;
  for (auto& message : input->array) {
    const auto type = message.GetString("type");
    if (type == "function_call" || type == "custom_tool_call")
      custom.emplace(message.GetString("call_id"), type == "custom_tool_call");
    if (message.Has("content")) Content(&Field(message, "content"));
    const auto role = message.GetString("role");
    if (role == "tool") {
      const auto id = message.GetString("tool_call_id");
      const auto found = custom.find(id);
      const auto* output = message.Find("content");
      if (found == custom.end() || !output)
        return BadInput("input_messages",
                        "Historical result has no matching tool call");
      result.array.push_back(
          Object({{"type", String(found->second ? "custom_tool_call_output"
                                                : "function_call_output")},
                  {"call_id", String(id)},
                  {"output", *output}}));
      continue;
    }
    if (role == "assistant") {
      const auto* reasoning = message.Find("reasoning");
      if (!reasoning) reasoning = message.Find("reasoning_content");
      if (reasoning && reasoning->IsString() && !reasoning->str.empty())
        result.array.push_back(Object(
            {{"type", String("reasoning")},
             {"id", String("rs_eval_" + std::to_string(result.array.size()))},
             {"summary", Array()},
             {"content", Array({Object({{"type", String("reasoning_text")},
                                        {"text", *reasoning}})})}}));
      if (const auto* refused = message.Find("refusal");
          refused && refused->IsString() && !refused->str.empty()) {
        const auto* content = message.Find("content");
        auto parts = content && content->IsArray() ? *content : Array();
        if (content && content->IsString() && !content->str.empty())
          parts.array.push_back(
              Object({{"type", String("output_text")}, {"text", *content}}));
        parts.array.push_back(
            Object({{"type", String("refusal")}, {"refusal", *refused}}));
        Set(&message, "content", std::move(parts));
      }
      std::erase_if(message.object, [](const auto& field) {
        return field.first == "reasoning" ||
               field.first == "reasoning_content" || field.first == "refusal";
      });
    }
    const auto* array = message.GetArray("tool_calls");
    if (!array || role != "assistant") {
      result.array.push_back(std::move(message));
      continue;
    }
    auto calls = std::move(Field(message, "tool_calls"));
    std::erase_if(message.object,
                  [](const auto& f) { return f.first == "tool_calls"; });
    const auto* content = message.Find("content");
    if (content && !content->IsNull() &&
        (!content->IsString() || !content->str.empty()))
      result.array.push_back(std::move(message));
    for (const auto& call : calls.array) {
      Json item;
      auto expanded = ExpandCall(call, &item);
      if (!expanded.ok()) return expanded;
      if (!custom
               .emplace(item.GetString("call_id"),
                        item.GetString("type") == "custom_tool_call")
               .second)
        return BadInput("input_messages", "Duplicate historical tool call ID");
      result.array.push_back(std::move(item));
    }
  }
  *input = std::move(result);
  return {};
}

ObjectReply EvalChatTool(const Json& item, Json* tool) {
  const bool custom = item.GetString("type") == "custom_tool_call";
  const auto type = custom ? "custom" : "function";
  const auto key = custom ? "input" : "arguments";
  const auto* value = item.Find(key);
  if ((!custom && item.GetString("type") != "function_call") ||
      item.GetString("call_id").empty() || item.GetString("name").empty() ||
      !value || !value->IsString())
    return InvalidRecord();
  *tool =
      Object({{"type", String(type)},
              {"id", *item.Find("call_id")},
              {type, Object({{"name", *item.Find("name")}, {key, *value}})}});
  return {};
}
}  // namespace q4t::server::eval_detail
