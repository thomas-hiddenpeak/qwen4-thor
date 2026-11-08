#include "q4t/server/output_constraints.h"

#include <algorithm>
#include <unordered_set>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/legacy_tools.h"
#include "q4t/server/output_schema.h"

namespace q4t::server {
namespace {
std::string Quote(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}

Status Fields(const io::Json& object,
              std::initializer_list<std::string_view> allowed,
              std::string* error_param, std::string_view path) {
  if (error_param) error_param->clear();
  if (!object.IsObject()) {
    if (error_param) *error_param = path;
    return Status::Fail("expected constraint object");
  }
  for (const auto& [key, value] : object.object) {
    (void)value;
    if (std::find(allowed.begin(), allowed.end(), key) == allowed.end()) {
      if (error_param) *error_param = InputParamPath(path, key);
      return Status::Fail("unsupported constraint field: " + key);
    }
  }
  return Status();
}

bool SafeName(const std::string& name) {
  return !name.empty() && name.size() <= 64 &&
         std::all_of(name.begin(), name.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
         });
}

io::Json Parameters(const io::Json& function) {
  if (const auto* parameters = function.Find("parameters")) return *parameters;
  io::Json empty;
  io::ParseJson(R"({"type":"object","properties":{},"required":[],)"
                R"("additionalProperties":false})",
                &empty);
  return empty;
}

std::string SchemaFormat(const io::Json& schema, bool xml) {
  return R"({"type":"json_schema","json_schema":)" + OutputJson(schema) +
         R"(,"style":)" + Quote(xml ? "qwen_xml" : "json") +
         R"(,"any_order":false,"max_whitespace_cnt":16)" +
         (xml ? R"(,"excludes":["<tool_call>","</tool_call>",)"
                R"("</parameter>","</function>"])"
              : "") +
         '}';
}

std::string TextFormat() {
  return R"({"type":"any_text","excludes":["<tool_call>",)"
         R"("</tool_call>","<think>","</think>"]})";
}

constexpr auto kWhitespace =
    R"({"type":"grammar","grammar":"root ::= [ \\t\\r\\n]{0,16}"})";

Status ParseDocument(std::string_view input, io::Json* out) {
  if (input.size() > 1024 * 1024)
    return Status::Fail("constrained output exceeds 1 MiB");
  return io::ParseJson(std::string(input), out, true,
                       {32, 65536, 1024 * 1024, true});
}
}  // namespace

Status PlanOutputConstraints(const io::Json& request, OutputConstraints* out,
                             std::string* error_param) {
  *out = {};
  if (error_param) error_param->clear();
  if (HasLegacyTools(request)) {
    LegacyToolRequest normalized;
    auto status = NormalizeLegacyTools(request, &normalized, error_param);
    if (!status) return status;
    status = PlanOutputConstraints(normalized.request, out, error_param);
    if (!status) LegacyToolErrorParam(request, error_param);
    return status;
  }
  const auto invalid = [&](std::string_view path, std::string message) {
    if (error_param) *error_param = path;
    return Status::Fail(std::move(message));
  };
  auto definitions = ValidateToolDefinitions(request, error_param);
  if (!definitions) return definitions;
  const bool parsed =
      request.Has("messages") && request.GetBool("parse_response", true);
  const auto* choice = request.Find("tool_choice");
  if (choice) {
    if (!parsed)
      return invalid("tool_choice", "tool_choice requires parsed messages");
    if (choice->IsString()) {
      if (choice->str == "none")
        out->choice = OutputConstraints::ToolChoice::kNone;
      else if (choice->str == "required")
        out->choice = OutputConstraints::ToolChoice::kRequired;
      else if (choice->str != "auto")
        return invalid("tool_choice", "invalid tool_choice");
    } else if (choice->GetString("type") == "allowed_tools") {
      auto status = Fields(*choice, {"type", "allowed_tools"}, error_param,
                           "tool_choice");
      if (!status) return status;
      const auto* allowed = choice->Find("allowed_tools");
      if (!allowed)
        return invalid("tool_choice.allowed_tools", "missing allowed tools");
      status = Fields(*allowed, {"mode", "tools"}, error_param,
                      "tool_choice.allowed_tools");
      if (!status) return status;
      const auto mode = allowed->GetString("mode");
      if (mode != "auto" && mode != "required")
        return invalid("tool_choice.allowed_tools.mode", "invalid mode");
      out->choice = mode == "required"
                        ? OutputConstraints::ToolChoice::kRequired
                        : OutputConstraints::ToolChoice::kAuto;
      const auto* list = allowed->GetArray("tools");
      if (!list)
        return invalid("tool_choice.allowed_tools.tools", "expected array");
      out->restrict_tools = true;
      std::unordered_set<std::string> seen;
      for (size_t i = 0; i < list->array.size(); ++i) {
        const auto& tool = list->array[i];
        const auto path =
            "tool_choice.allowed_tools.tools[" + std::to_string(i) + ']';
        status = Fields(tool, {"type", "function"}, error_param, path);
        if (!status) return status;
        if (tool.GetString("type") != "function")
          return invalid(path + ".type", "expected function reference");
        const auto* function = tool.Find("function");
        if (!function) return invalid(path + ".function", "missing reference");
        status = Fields(*function, {"name"}, error_param, path + ".function");
        if (!status) return status;
        const auto name = function->GetString("name");
        if (!SafeName(name) || !seen.insert(name).second)
          return invalid(path + ".function.name", "invalid or duplicate name");
        const auto* definitions = request.GetArray("tools");
        if (!definitions ||
            std::none_of(definitions->array.begin(), definitions->array.end(),
                         [&](const io::Json& item) {
                           const auto* fn = item.Find("function");
                           return fn && fn->GetString("name") == name;
                         }))
          return invalid(path + ".function.name", "tool is not declared");
        out->allowed_tools.push_back(name);
      }
      if (mode == "required" && out->allowed_tools.empty())
        return invalid("tool_choice.allowed_tools.tools",
                       "required mode needs an allowed tool");
    } else {
      auto status =
          Fields(*choice, {"type", "function"}, error_param, "tool_choice");
      if (!status) return status;
      const auto* fn = choice->Find("function");
      if (choice->GetString("type") != "function" || !fn)
        return invalid(choice->GetString("type") != "function"
                           ? "tool_choice.type"
                           : "tool_choice.function",
                       "named tool_choice requires a function");
      status = Fields(*fn, {"name"}, error_param, "tool_choice.function");
      if (!status) return status;
      out->named_tool = fn->GetString("name");
      if (!SafeName(out->named_tool))
        return invalid("tool_choice.function.name", "invalid named tool");
      out->choice = OutputConstraints::ToolChoice::kNamed;
    }
  }
  if (const auto* parallel = request.Find("parallel_tool_calls")) {
    if (!parsed || !parallel->IsBool())
      return invalid("parallel_tool_calls",
                     "parallel_tool_calls requires parsed messages / bool");
    out->parallel = parallel->boolean;
  }
  const auto* tools = request.GetArray("tools");
  const bool has_tools = tools && !tools->array.empty();
  const bool forced = out->choice == OutputConstraints::ToolChoice::kRequired ||
                      out->choice == OutputConstraints::ToolChoice::kNamed;
  if (forced && !has_tools)
    return invalid("tools",
                   "required/named tool_choice requires nonempty tools");
  bool strict_tools = false;
  if (tools) {
    for (const auto& tool : tools->array) {
      const auto* fn = tool.Find("function");
      const auto* strict = fn ? fn->Find("strict") : nullptr;
      strict_tools |= strict && strict->IsBool() && strict->boolean;
    }
  }
  const auto* format = request.Find("response_format");
  const io::Json* schema = nullptr;
  bool strict_schema = false;
  if (format && !format->IsNull()) {
    const auto type = format->GetString("type");
    auto status = Fields(
        *format,
        type == "json_schema"
            ? std::initializer_list<std::string_view>{"type", "json_schema"}
            : std::initializer_list<std::string_view>{"type"},
        error_param, "response_format");
    if (!status) return status;
    if (type != "text" && type != "json_object" && type != "json_schema")
      return invalid("response_format.type",
                     "unsupported response_format.type");
    out->json_output = type != "text";
    if (type == "json_schema") {
      const auto* spec = format->Find("json_schema");
      if (!spec)
        return invalid("response_format.json_schema",
                       "response_format requires json_schema");
      status = Fields(*spec, {"name", "description", "strict", "schema"},
                      error_param, "response_format.json_schema");
      if (!status) return status;
      if (!SafeName(spec->GetString("name")))
        return invalid("response_format.json_schema.name",
                       "invalid response schema name");
      if (const auto* strict = spec->Find("strict")) {
        if (!strict->IsNull() && !strict->IsBool())
          return invalid("response_format.json_schema.strict",
                         "schema.strict must be bool or null");
        strict_schema = strict->IsBool() && strict->boolean;
      }
      if (const auto* description = spec->Find("description");
          description && !description->IsString())
        return invalid("response_format.json_schema.description",
                       "schema description must be a string");
      schema = spec->Find("schema");
      if (!schema)
        return invalid("response_format.json_schema.schema",
                       "missing response schema");
      status = ValidateOutputSchema(*schema, strict_schema, false, error_param,
                                    "response_format.json_schema.schema");
      if (!status) return status;
    }
  }
  out->active = out->json_output || forced || strict_tools ||
                out->restrict_tools ||
                out->choice == OutputConstraints::ToolChoice::kNone ||
                (!out->parallel && has_tools);
  if (!out->active) return Status();
  if (!parsed)
    return invalid(request.Has("messages") ? "parse_response" : "messages",
                   "output constraints require parsed messages");
  if (request.Has("session_id") || request.Has("prefix_cache_scope"))
    return invalid(
        request.Has("session_id") ? "session_id" : "prefix_cache_scope",
        "output constraints with session/prefix cache are not "
        "supported in this stage");
  if (out->json_output && has_tools &&
      out->choice != OutputConstraints::ToolChoice::kNone)
    return invalid("tool_choice",
                   "response_format plus tools requires tool_choice=none");

  std::string payload;
  if (out->json_output) {
    payload = schema
                  ? SchemaFormat(*schema, false)
                  : R"({"type":"json_schema","json_schema":{"type":"object",)"
                    R"("additionalProperties":true},)"
                    R"("any_order":false,"max_whitespace_cnt":16})";
  } else if (out->choice == OutputConstraints::ToolChoice::kNone ||
             (out->restrict_tools && out->allowed_tools.empty()) ||
             !has_tools) {
    payload = TextFormat();
  } else {
    if (tools->array.size() > 16)
      return invalid("tools", "constrained requests allow at most 16 tools");
    std::unordered_set<std::string> names;
    std::string tags;
    for (size_t index = 0; index < tools->array.size(); ++index) {
      const auto& tool = tools->array[index];
      const auto path = "tools[" + std::to_string(index) + "]";
      const auto fn_path = path + ".function";
      auto status = Fields(tool, {"type", "function"}, error_param, path);
      if (!status) return status;
      const auto* fn = tool.Find("function");
      if (tool.GetString("type") != "function" || !fn)
        return invalid(
            tool.GetString("type") != "function" ? path + ".type" : fn_path,
            "constraints require function tools");
      status = Fields(*fn, {"name", "description", "parameters", "strict"},
                      error_param, fn_path);
      if (!status) return status;
      const auto name = fn->GetString("name");
      if (!SafeName(name) || !names.insert(name).second)
        return invalid(fn_path + ".name",
                       "invalid or duplicate constrained tool name");
      if (const auto* description = fn->Find("description");
          description && !description->IsString())
        return invalid(fn_path + ".description",
                       "function description must be a string");
      // Even ignored tool definitions must not contain unsupported strict
      // schemas. None mode intentionally never compiles argument schemas.
      const auto parameters = Parameters(*fn);
      status = ValidateOutputSchema(parameters, fn->GetBool("strict"), true,
                                    error_param, fn_path + ".parameters");
      if (!status) return status;
      if (!out->AllowsTool(name)) continue;
      if (!tags.empty()) tags += ',';
      tags += R"({"type":"tag","begin":)" +
              Quote("<tool_call>\n<function=" + name + ">\n") +
              R"(,"content":)" + SchemaFormat(parameters, true) +
              R"(,"end":"\n</function>\n</tool_call>"})";
    }
    if (tags.empty())
      return invalid("tool_choice.function.name", "named tool is not declared");
    const bool single =
        !out->parallel || out->choice == OutputConstraints::ToolChoice::kNamed;
    if (forced) {
      payload = R"({"type":"tags_with_separator","tags":[)" + tags +
                R"(],"separator":"\n","at_least_one":true,)"
                R"("stop_after_first":)" +
                (single ? "true}" : "false}");
    } else {
      payload = R"({"type":"triggered_tags","triggers":["<tool_call>"],)"
                R"("tags":[)" +
                tags +
                R"(],"at_least_one":false,)"
                R"("excludes":["<think>","</think>","</tool_call>"],)"
                R"("stop_after_first":)" +
                (single ? "true}" : "false}");
    }
  }
  bool thinking = request.GetBool("enable_thinking", true);
  if (const auto* options = request.Find("chat_template_kwargs"))
    thinking = options->GetBool("enable_thinking", thinking);
  std::string prefix;
  if (thinking) {
    prefix = R"({"type":"tag","begin":"","content":{"type":"any_text",)"
             R"("excludes":["</think>"]},"end":"</think>"},)";
  }
  out->structural_tag = R"({"type":"structural_tag","format":)"
                        R"({"type":"sequence","elements":[)" +
                        prefix + kWhitespace + ',' + payload + "]}}";
  if (out->structural_tag.size() > 65536)
    return invalid(out->json_output ? "response_format" : "tools",
                   "compiled request description exceeds 64 KiB");
  return Status();
}

io::Json ConstraintTemplateRequest(const io::Json& request,
                                   const OutputConstraints& plan) {
  io::Json result = request;
  for (auto& [key, value] : result.object) {
    if (key != "tools" || !value.IsArray()) continue;
    if (plan.choice == OutputConstraints::ToolChoice::kNone)
      value.array.clear();
    else if (plan.choice == OutputConstraints::ToolChoice::kNamed ||
             plan.restrict_tools)
      std::erase_if(value.array, [&](const io::Json& tool) {
        const auto* fn = tool.Find("function");
        return !fn || !plan.AllowsTool(fn->GetString("name"));
      });
  }
  return result;
}

Status ValidateConstrainedArguments(const io::Json& function,
                                    std::string_view arguments) {
  io::Json value;
  auto status = ParseDocument(arguments, &value);
  return status ? ValidateOutputValue(Parameters(function), value) : status;
}

Status ValidateConstrainedContent(const io::Json& request,
                                  std::string_view content) {
  io::Json value;
  auto status = ParseDocument(content, &value);
  if (!status) return status;
  if (!value.IsObject()) return Status::Fail("JSON output must be an object");
  const auto* format = request.Find("response_format");
  const auto* spec = format ? format->Find("json_schema") : nullptr;
  const auto* schema = spec ? spec->Find("schema") : nullptr;
  return schema ? ValidateOutputValue(*schema, value) : Status();
}

}  // namespace q4t::server
