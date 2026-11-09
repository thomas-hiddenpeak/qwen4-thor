#include "prompt_internal.h"

#include <algorithm>
#include <set>

#include "q4t/server/input_contract.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server::prompt_detail {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_prompt", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
bool Version(std::string_view version) {
  return !version.empty() && version.size() <= 9 && version.front() != '0' &&
         std::all_of(version.begin(), version.end(),
                     [](char c) { return c >= '0' && c <= '9'; });
}
namespace {
bool Name(std::string_view name) {
  return !name.empty() && name.size() <= 64 &&
         std::none_of(name.begin(), name.end(), [](unsigned char c) {
           return c <= ' ' || c == '{' || c == '}' || c == 127;
         });
}
ObjectReply Fields(const Json& object,
                   std::initializer_list<std::string_view> fields,
                   std::string_view path) {
  std::string bad;
  auto status = InputFields(object, fields, &bad, path);
  return status ? ObjectReply() : Invalid(status.message(), bad);
}
Json Text(std::string text) {
  return Object({{"type", String("input_text")}, {"text", String(text)}});
}
ObjectReply Shape(const Json& part, const std::string& path) {
  ChatPlanError error;
  auto message = Object({{"role", String("user")}, {"content", Array({part})}});
  const auto status = ValidateResponsesItemShape(
      message, ResponsesItemShape::kInput, &error, path);
  return status ? ObjectReply() : Invalid(status.message(), path);
}
// Only template text is scanned; substituted values are data and are never
// scanned again. Output expansion is charged before each append.
class Renderer {
 public:
  explicit Renderer(const Json& variables) : variables_(variables) {}
  ObjectReply StringParts(std::string_view text, Json* parts,
                          const std::string& path) {
    *parts = Array();
    const auto append = [&](Json value) -> ObjectReply {
      const size_t size = OutputJson(value).size();
      if (parts->array.size() >= 1024 || size > kExpandedBytes - bytes_)
        return Error(413, "prompt_expansion_limit", "Prompt expansion limit");
      bytes_ += size;
      parts->array.push_back(std::move(value));
      return {};
    };
    while (!text.empty()) {
      const auto start = text.find("{{");
      if (start == text.npos) return append(Text(std::string(text)));
      if (start) {
        auto reply = append(Text(std::string(text.substr(0, start))));
        if (!reply.ok()) return reply;
      }
      text.remove_prefix(start + 2);
      const auto end = text.find("}}");
      if (end == text.npos) return Invalid("Unclosed prompt variable", path);
      auto name = text.substr(0, end);
      while (!name.empty() && name.front() == ' ') name.remove_prefix(1);
      while (!name.empty() && name.back() == ' ') name.remove_suffix(1);
      if (!Name(name)) return Invalid("Invalid prompt variable name", path);
      const auto* value = variables_.Find(std::string(name));
      if (!value)
        return Invalid("Missing declared prompt variable",
                       "prompt.variables." + std::string(name));
      used_.insert(std::string(name));
      auto reply = append(value->IsString() ? Text(value->str) : *value);
      if (!reply.ok()) return reply;
      text.remove_prefix(end + 2);
    }
    // Empty text stays an actual empty text value, not a missing message.
    if (parts->array.empty()) return append(Text(""));
    return {};
  }
  ObjectReply Instructions(Json* value) {
    if (value->IsNull()) return {};
    if (!value->IsString())
      return Invalid("Instructions must be text",
                     "definition.request.instructions");
    Json parts;
    auto result = StringParts(value->str, &parts, "instructions");
    if (!result.ok()) return result;
    std::string text;
    for (const auto& part : parts.array) {
      if (part.GetString("type") != "input_text")
        return Invalid("Media variables require message content",
                       "instructions");
      if (part.Has("prompt_cache_breakpoint"))
        return Invalid("Cache breakpoints require structured message content",
                       "instructions");
      text += part.GetString("text");
    }
    *value = String(std::move(text));
    return {};
  }
  ObjectReply Input(Json* input) {
    if (input->IsNull()) return {};
    if (input->IsString())
      *input = Array({Object({{"role", String("user")}, {"content", *input}})});
    if (!input->IsArray() || input->array.size() > 512)
      return Invalid("Prompt input requires at most 512 items", "input");
    for (size_t i = 0; i < input->array.size(); ++i) {
      auto& item = input->array[i];
      const auto path = "input[" + std::to_string(i) + ']';
      if (item.Has("role") || item.GetString("type") == "message") {
        auto* content = &Field(item, "content");
        if (content->IsString()) {
          Json parts;
          auto result = StringParts(content->str, &parts, path + ".content");
          if (!result.ok()) return result;
          *content = std::move(parts);
        } else if (content->IsArray()) {
          auto parts = Array();
          for (const auto& part : content->array) {
            if (part.GetString("type") == "input_text") {
              const auto* text = part.Find("text");
              if (!text || !text->IsString())
                return Invalid("Input text requires text", path + ".content");
              // Extra fields must not disappear through substitution.
              auto checked = Shape(part, path + ".content");
              if (!checked.ok()) return checked;
              Json expanded;
              auto result =
                  StringParts(text->str, &expanded, path + ".content");
              if (!result.ok()) return result;
              if (const auto* boundary = part.Find("prompt_cache_breakpoint")) {
                auto& last = expanded.array.back();
                if (const auto* variable_boundary =
                        last.Find("prompt_cache_breakpoint");
                    variable_boundary &&
                    (variable_boundary->GetString("mode") !=
                         boundary->GetString("mode") ||
                     variable_boundary->GetString("ttl") !=
                         boundary->GetString("ttl")))
                  return Invalid("Conflicting cache boundaries at one position",
                                 path + ".content");
                Set(&last, "prompt_cache_breakpoint", *boundary);
              }
              parts.array.insert(parts.array.end(), expanded.array.begin(),
                                 expanded.array.end());
            } else {
              parts.array.push_back(part);
            }
            if (parts.array.size() > 1024)
              return Error(413, "prompt_expansion_limit", "Prompt parts limit");
          }
          *content = std::move(parts);
        }
      }
      ChatPlanError error;
      auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                               &error, path);
      if (!status) return Invalid(status.message(), error.param);
    }
    return {};
  }
  const std::set<std::string>& Used() const { return used_; }

 private:
  const Json& variables_;
  size_t bytes_ = 0;
  std::set<std::string> used_;
};
}  // namespace

ObjectReply Render(const Json& definition, const Json& variables) {
  const auto* types = definition.Find("variables");
  const auto* base = definition.Find("request");
  if (!types || !types->IsObject() || !base || !base->IsObject() ||
      types->object.size() > 64 || !variables.IsObject() ||
      variables.object.size() != types->object.size() ||
      OutputJson(variables).size() > kExpandedBytes)
    return Invalid("Prompt variables must match the declared names",
                   "prompt.variables");
  for (const auto& [name, type] : types->object) {
    const auto* value = variables.Find(name);
    if (!Name(name) || !type.IsString() || !value)
      return Invalid("Invalid or missing prompt variable", "prompt.variables");
    const auto path = "prompt.variables." + name;
    if (type.str == "string") {
      if (!value->IsString()) return Invalid("Expected string variable", path);
    } else {
      if ((type.str != "input_text" && type.str != "input_image" &&
           type.str != "input_file") ||
          value->GetString("type") != type.str)
        return Invalid("Prompt variable type mismatch", path);
      auto result = Shape(*value, path);
      if (!result.ok()) return result;
    }
  }
  Renderer render(variables);
  ObjectReply result;
  result.value = *base;
  if (result.value.Has("instructions")) {
    auto checked = render.Instructions(&Field(result.value, "instructions"));
    if (!checked.ok()) return checked;
  }
  if (result.value.Has("input")) {
    auto checked = render.Input(&Field(result.value, "input"));
    if (!checked.ok()) return checked;
  }
  if (render.Used().size() != types->object.size())
    return Invalid("Declared variable is unused in instructions or input",
                   "definition.variables");
  if (OutputJson(result.value).size() > kExpandedBytes)
    return Error(413, "prompt_expansion_limit", "Expanded prompt is too large");
  return result;
}

ObjectReply ValidateDefinition(const Json& definition,
                               const ChatPlanConfig& config) {
  auto result = Fields(definition, {"request", "variables"}, "definition");
  if (!result.ok()) return result;
  if (OutputJson(definition).size() > kDefinitionBytes)
    return Error(413, "prompt_definition_limit",
                 "Prompt definition is too large");
  const auto* request = definition.Find("request");
  const auto* types = definition.Find("variables");
  if (!request || !types || !types->IsObject() || types->object.size() > 64)
    return Invalid("Prompt requires request and variable types", "definition");
  result = Fields(*request,
                  {"model",
                   "instructions",
                   "input",
                   "reasoning",
                   "text",
                   "tools",
                   "tool_choice",
                   "parallel_tool_calls",
                   "temperature",
                   "top_p",
                   "top_logprobs",
                   "max_output_tokens",
                   "max_tool_calls",
                   "truncation",
                   "include",
                   "context_management",
                   "prompt_cache_key",
                   "prompt_cache_retention",
                   "prompt_cache_options",
                   "moderation",
                   "access_programs"},
                  "definition.request");
  if (!result.ok()) return result;
  auto dummy = Object();
  for (const auto& [name, type] : types->object) {
    if (!Name(name) || !type.IsString())
      return Invalid("Invalid variable declaration", "definition.variables");
    if (type.str == "string")
      Set(&dummy, name, String("value"));
    else if (type.str == "input_text")
      Set(&dummy, name, Text("value"));
    else if (type.str == "input_image")
      Set(&dummy, name,
          Object({{"type", type},
                  {"detail", String("auto")},
                  {"image_url", String("data:image/png;base64,AA==")}}));
    else if (type.str == "input_file")
      Set(&dummy, name,
          Object({{"type", type}, {"file_id", String("file_shape")}}));
    else
      return Invalid("Unknown variable type", "definition.variables." + name);
  }
  auto rendered = Render(definition, dummy);
  if (!rendered.ok()) return rendered;
  ResponsesEnvelope envelope;
  ChatPlanError error;
  auto status =
      ParseResponsesEnvelope(rendered.value, config, &envelope, &error);
  if (!status)
    return Invalid(status.message(), "definition.request." + error.param);
  Json tools, choice;
  status = NormalizeResponsesTools(rendered.value, &tools, &choice, &error);
  return status
             ? ObjectReply()
             : Invalid(status.message(), "definition.request." + error.param);
}
}  // namespace q4t::server::prompt_detail
