#include "q4t/server/chat_tool_history.h"

#include <algorithm>
#include <numeric>
#include <string>
#include <unordered_map>
#include <utility>

#include "q4t/server/input_contract.h"
#include "q4t/server/custom_tools.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
constexpr size_t kMaxCalls = 128;
constexpr size_t kMaxIdBytes = 256;

bool ValidId(const io::Json* id) {
  return id && id->IsString() && !id->str.empty() &&
         id->str.size() <= kMaxIdBytes;
}

Status ValidateCall(const io::Json& call, bool identified) {
  if (call.GetString("type") == "custom") {
    auto status = InputFields(call, {"id", "type", "custom"});
    if (!status) return status;
    const auto* custom = call.Find("custom");
    if (!ValidId(call.Find("id")) || !custom)
      return Status::Fail("custom history requires an ID and custom object");
    status = InputFields(*custom, {"name", "input"});
    if (!status) return status;
    const auto* input = custom->Find("input");
    if (!SafeToolName(custom->GetString("name")) || !input ||
        !input->IsString())
      return Status::Fail("custom history requires a name and input string");
    return Status();
  }
  const auto* fn = call.Find("function");
  auto status = fn ? InputFields(call, {"id", "type", "function"})
                   : InputFields(call, {"type", "name", "arguments"});
  if (!status) return status;
  if ((identified && (!fn || !ValidId(call.Find("id")) ||
                      call.GetString("type") != "function")) ||
      (call.Has("type") && call.GetString("type") != "function"))
    return Status::Fail("tool calls require function type and valid batch IDs");
  if (fn) {
    status = InputFields(*fn, {"name", "arguments"});
    if (!status) return status;
  } else {
    fn = &call;  // Explicit native legacy flat shape, without an ID.
  }
  if (!ValidInputName(fn->GetString("name")))
    return Status::Fail("invalid historical function name");
  const auto* args = fn->Find("arguments");
  io::Json decoded;
  if (args && args->IsString()) {
    status = io::ParseJson(args->str, &decoded, true, kSchemaJsonLimits);
    if (!status) return Status::Fail("invalid historical arguments JSON");
    args = &decoded;
  }
  if (!args || !args->IsObject())
    return Status::Fail("tool arguments require an object or JSON object text");
  // JSON property names are data. The model renderer encodes unsafe native
  // XML keys in one JSON object instead of rejecting valid API arguments.
  return Status();
}

bool TextResult(const io::Json& message) {
  const auto* content = message.Find("content");
  if (!content || content->IsNull() || content->IsString()) return true;
  if (!content->IsArray()) return false;
  for (const auto& part : content->array) {
    const auto* text = part.Find("text");
    const auto type = part.GetString("type");
    if (!part.IsObject() || !text || !text->IsString() ||
        part.Has("image_url") || part.Has("video_frames") ||
        (!type.empty() && type != "text"))
      return false;
  }
  return true;
}
}  // namespace

Status PlanToolResultOrder(const io::Json& messages,
                           std::vector<size_t>* render_order) {
  if (render_order) render_order->clear();
  if (!messages.IsArray()) return Status::Fail("messages must be an array");
  const auto& ms = messages.array;
  for (const auto& message : ms) {
    if (!message.IsObject()) return Status::Fail("message must be an object");
    if (message.Has("tool_call_id") && message.GetString("role") != "tool")
      return Status::Fail("tool_call_id is only valid on tool results");
    if (message.GetString("role") == "tool") {
      const auto* content = message.Find("content");
      if (!content || !(content->IsString() || content->IsArray()))
        return Status::Fail("tool result requires string or array content");
    }
  }
  std::vector<size_t> order;
  if (render_order) {
    order.resize(ms.size());
    std::iota(order.begin(), order.end(), size_t{0});
  }
  for (size_t i = 0; i < ms.size();) {
    const auto role = ms[i].GetString("role");
    if (role == "tool" && ms[i].Has("tool_call_id"))
      return Status::Fail("orphan tool_call_id result");
    const auto* calls =
        role == "assistant" ? ms[i].Find("tool_calls") : nullptr;
    if (!calls || calls->IsNull()) {
      ++i;
      continue;
    }
    if (!calls->IsArray()) return Status::Fail("tool_calls must be an array");
    if (calls->array.empty()) {
      ++i;
      continue;
    }
    const bool identified =
        std::any_of(calls->array.begin(), calls->array.end(),
                    [](const io::Json& call) { return call.Has("id"); });
    if (calls->array.size() > kMaxCalls)
      return Status::Fail("tool batch exceeds 128 calls");
    for (const auto& call : calls->array) {
      auto status = ValidateCall(call, identified);
      if (!status) return status;
    }
    size_t end = i + 1;
    while (end < ms.size() && ms[end].GetString("role") == "tool") ++end;
    if (!identified) {
      // Preserve the frozen native-template path, including legacy fixtures
      // with multiple result messages for one unlabelled function call.
      for (size_t j = i + 1; j < end; ++j)
        if (ms[j].Has("tool_call_id"))
          return Status::Fail("tool result IDs require IDs on all calls");
      i = end;
      continue;
    }
    std::unordered_map<std::string, size_t> slots;
    for (size_t k = 0; k < calls->array.size(); ++k) {
      const auto& call = calls->array[k];
      const auto* id = call.Find("id");
      if (!slots.emplace(id->str, k).second)
        return Status::Fail("duplicate tool call id within assistant batch");
    }
    std::vector<size_t> results(calls->array.size(), ms.size());
    for (size_t j = i + 1; j < end; ++j) {
      const auto* id = ms[j].Find("tool_call_id");
      if (!ValidId(id))
        return Status::Fail("tool result requires tool_call_id");
      const auto slot = slots.find(id->str);
      if (slot == slots.end())
        return Status::Fail("tool_call_id does not match the preceding batch");
      if (results[slot->second] != ms.size())
        return Status::Fail("duplicate result for tool_call_id");
      results[slot->second] = j;
    }
    if (std::find(results.begin(), results.end(), ms.size()) != results.end())
      return Status::Fail("all tool results are required before continuing");
    bool reordered = false;
    for (size_t k = 0; k < results.size(); ++k)
      reordered = reordered || results[k] != i + 1 + k;
    if (reordered) {
      // RenderContent gathers vision features in original message order.
      // Moving media markers independently would misassociate those features.
      for (size_t j = i + 1; j < end; ++j)
        if (!TextResult(ms[j]))
          return Status::Fail("media tool results must follow call order");
    }
    if (render_order)
      for (size_t k = 0; k < results.size(); ++k) order[i + 1 + k] = results[k];
    i = end;
  }
  if (render_order) *render_order = std::move(order);
  return Status();
}

}  // namespace q4t::server
