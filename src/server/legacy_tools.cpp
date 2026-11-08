#include "q4t/server/legacy_tools.h"

#include <algorithm>

#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
io::Json String(std::string value) {
  io::Json out;
  out.type = io::Json::Type::kString;
  out.str = std::move(value);
  return out;
}
io::Json Object(
    std::initializer_list<std::pair<std::string, io::Json>> fields) {
  io::Json out;
  out.type = io::Json::Type::kObject;
  out.object = fields;
  return out;
}
io::Json Array() {
  io::Json out;
  out.type = io::Json::Type::kArray;
  return out;
}
void Erase(io::Json* value, std::string_view field) {
  std::erase_if(value->object,
                [&](const auto& pair) { return pair.first == field; });
}
void Set(io::Json* value, std::string name, io::Json field) {
  Erase(value, name);
  value->object.emplace_back(std::move(name), std::move(field));
}
bool Nonnull(const io::Json& value, const char* field) {
  const auto* item = value.Find(field);
  return item && !item->IsNull();
}
bool FunctionName(const std::string& name) {
  return !name.empty() && name.size() <= 64 &&
         std::all_of(name.begin(), name.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
         });
}
}  // namespace

bool HasLegacyTools(const io::Json& request) {
  if (request.Has("functions") || request.Has("function_call")) return true;
  const auto* messages = request.GetArray("messages");
  return messages &&
         std::any_of(messages->array.begin(), messages->array.end(),
                     [](const auto& message) {
                       return message.Has("function_call") ||
                              message.GetString("role") == "function";
                     });
}

Status NormalizeLegacyTools(const io::Json& original, LegacyToolRequest* out,
                            std::string* error_param) {
  *out = {};
  if (error_param) error_param->clear();
  const auto invalid = [&](std::string path, std::string message) {
    if (error_param) *error_param = std::move(path);
    return Status::Fail(std::move(message));
  };
  out->request = original;
  auto& result = out->request;
  out->legacy_output =
      Nonnull(original, "functions") || Nonnull(original, "function_call");
  if (out->legacy_output &&
      (Nonnull(original, "tools") || Nonnull(original, "tool_choice")))
    return invalid(Nonnull(original, "tools") ? "tools" : "tool_choice",
                   "legacy and current tool controls cannot be mixed");
  if (out->legacy_output) {
    if (const auto* choice = result.Find("tool_choice");
        choice && choice->IsNull())
      Erase(&result, "tool_choice");
    if (const auto* parallel = original.Find("parallel_tool_calls");
        parallel && (!parallel->IsBool() || parallel->boolean))
      return invalid("parallel_tool_calls", "legacy functions allow one call");
    io::Json single;
    single.type = io::Json::Type::kBool;
    single.boolean = false;
    Set(&result, "parallel_tool_calls", single);
  }
  if (const auto* functions = original.Find("functions");
      functions && !functions->IsNull()) {
    if (!functions->IsArray()) return invalid("functions", "expected array");
    if (functions->array.empty() || functions->array.size() > 128)
      return invalid("functions", "functions requires 1 to 128 definitions");
    auto tools = Array();
    for (size_t i = 0; i < functions->array.size(); ++i) {
      const auto& function = functions->array[i];
      const auto path = "functions[" + std::to_string(i) + ']';
      const auto status = InputFields(
          function, {"name", "description", "parameters"}, error_param, path);
      if (!status) return status;
      if (!FunctionName(function.GetString("name")))
        return invalid(path + ".name", "invalid function name");
      tools.array.push_back(
          Object({{"type", String("function")}, {"function", function}}));
    }
    Set(&result, "tools", std::move(tools));
  }
  if (const auto* choice = original.Find("function_call");
      choice && !choice->IsNull()) {
    if (choice->IsString()) {
      if (choice->str != "none" && choice->str != "auto")
        return invalid("function_call",
                       "expected none, auto or a named function");
      Set(&result, "tool_choice", *choice);
    } else {
      const auto status =
          InputFields(*choice, {"name"}, error_param, "function_call");
      if (!status) return status;
      if (!FunctionName(choice->GetString("name")))
        return invalid("function_call.name", "invalid function name");
      Set(&result, "tool_choice",
          Object({{"type", String("function")}, {"function", *choice}}));
    }
  }
  Erase(&result, "functions");
  Erase(&result, "function_call");
  std::string pending_id, pending_name;
  for (auto& [field, messages] : result.object) {
    if (field != "messages" || !messages.IsArray()) continue;
    for (size_t i = 0; i < messages.array.size(); ++i) {
      auto& message = messages.array[i];
      const auto path = "messages[" + std::to_string(i) + ']';
      const auto role = message.GetString("role");
      if (role == "function") {
        if (pending_id.empty()) return invalid(path, "orphan function result");
        auto status = InputFields(message, {"role", "name", "content"},
                                  error_param, path);
        if (!status) return status;
        if (message.GetString("name") != pending_name)
          return invalid(path + ".name", "function result does not match call");
        const auto* content = message.Find("content");
        if (!content || (!content->IsString() && !content->IsNull()))
          return invalid(path + ".content",
                         "function result needs text or null");
        const bool empty = content->IsNull();
        Set(&message, "role", String("tool"));
        Set(&message, "tool_call_id", String(pending_id));
        if (empty) Set(&message, "content", String(""));
        pending_id.clear();
        pending_name.clear();
        continue;
      }
      if (!pending_id.empty())
        return invalid(path, "function result must follow its call");
      const auto* function = message.Find("function_call");
      if (!function) continue;
      if (role != "assistant")
        return invalid(path + ".function_call",
                       "function_call needs assistant role");
      if (!function->IsNull()) {
        if (Nonnull(message, "tool_calls"))
          return invalid(path + ".tool_calls",
                         "two call dialects in one message");
        auto status = InputFields(*function, {"name", "arguments"}, error_param,
                                  path + ".function_call");
        if (!status) return status;
        pending_name = function->GetString("name");
        const auto* arguments = function->Find("arguments");
        if (!FunctionName(pending_name))
          return invalid(path + ".function_call.name", "invalid function name");
        if (!arguments || !arguments->IsString())
          return invalid(path + ".function_call.arguments",
                         "expected JSON text");
        pending_id = "q4t_legacy_" + std::to_string(i);
        auto calls = Array();
        calls.array.push_back(Object({{"id", String(pending_id)},
                                      {"type", String("function")},
                                      {"function", *function}}));
        Set(&message, "tool_calls", std::move(calls));
      }
      Erase(&message, "function_call");
    }
  }
  if (!pending_id.empty())
    return invalid("messages", "function result missing");
  return Status();
}

void LegacyToolErrorParam(const io::Json& original, std::string* param) {
  if (!param) return;
  if (Nonnull(original, "functions") && param->starts_with("tools[")) {
    param->replace(0, 5, "functions");
    const auto field = param->find("].function");
    if (field != param->npos) param->erase(field + 1, 9);
  } else if (Nonnull(original, "function_call") &&
             param->starts_with("tool_choice")) {
    if (param->starts_with("tool_choice.function")) param->erase(11, 9);
    param->replace(0, 11, "function_call");
  }
  const auto* messages = original.GetArray("messages");
  if (!messages) return;
  for (size_t i = 0; i < messages->array.size(); ++i) {
    if (!messages->array[i].Has("function_call")) continue;
    const auto prefix = "messages[" + std::to_string(i) + "].tool_calls";
    if (!param->starts_with(prefix)) continue;
    auto suffix = param->substr(prefix.size());
    if (suffix.starts_with("[0].function")) suffix.erase(0, 12);
    *param = "messages[" + std::to_string(i) + "].function_call" + suffix;
    return;
  }
}

}  // namespace q4t::server
