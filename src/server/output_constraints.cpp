#include "q4t/server/output_constraints.h"

#include <algorithm>
#include <unordered_set>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"

namespace q4t::server {
namespace {
std::string Quote(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}

Status Fields(const io::Json& object,
              std::initializer_list<std::string_view> allowed) {
  if (!object.IsObject()) return Status::Fail("expected constraint object");
  for (const auto& [key, value] : object.object) {
    (void)value;
    if (std::find(allowed.begin(), allowed.end(), key) == allowed.end())
      return Status::Fail("unsupported constraint field: " + key);
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

Status PlanOutputConstraints(const io::Json& request, OutputConstraints* out) {
  *out = {};
  auto definitions = ValidateToolDefinitions(request);
  if (!definitions) return definitions;
  const bool parsed =
      request.Has("messages") && request.GetBool("parse_response", true);
  const auto* choice = request.Find("tool_choice");
  if (choice) {
    if (!parsed) return Status::Fail("tool_choice requires parsed messages");
    if (choice->IsString()) {
      if (choice->str == "none")
        out->choice = OutputConstraints::ToolChoice::kNone;
      else if (choice->str == "required")
        out->choice = OutputConstraints::ToolChoice::kRequired;
      else if (choice->str != "auto")
        return Status::Fail("invalid tool_choice");
    } else {
      auto status = Fields(*choice, {"type", "function"});
      if (!status) return status;
      const auto* fn = choice->Find("function");
      if (choice->GetString("type") != "function" || !fn)
        return Status::Fail("named tool_choice requires a function");
      status = Fields(*fn, {"name"});
      if (!status) return status;
      out->named_tool = fn->GetString("name");
      if (!SafeName(out->named_tool)) return Status::Fail("invalid named tool");
      out->choice = OutputConstraints::ToolChoice::kNamed;
    }
  }
  if (const auto* parallel = request.Find("parallel_tool_calls")) {
    if (!parsed || !parallel->IsBool())
      return Status::Fail(
          "parallel_tool_calls requires parsed messages / bool");
    out->parallel = parallel->boolean;
  }
  const auto* tools = request.GetArray("tools");
  const bool has_tools = tools && !tools->array.empty();
  const bool forced = out->choice == OutputConstraints::ToolChoice::kRequired ||
                      out->choice == OutputConstraints::ToolChoice::kNamed;
  if (forced && !has_tools)
    return Status::Fail("required/named tool_choice requires nonempty tools");
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
            : std::initializer_list<std::string_view>{"type"});
    if (!status) return status;
    if (type != "text" && type != "json_object" && type != "json_schema")
      return Status::Fail("unsupported response_format.type");
    out->json_output = type != "text";
    if (type == "json_schema") {
      const auto* spec = format->Find("json_schema");
      if (!spec) return Status::Fail("response_format requires json_schema");
      status = Fields(*spec, {"name", "description", "strict", "schema"});
      if (!status) return status;
      if (!SafeName(spec->GetString("name")))
        return Status::Fail("invalid response schema name");
      if (const auto* strict = spec->Find("strict")) {
        if (!strict->IsNull() && !strict->IsBool())
          return Status::Fail("schema.strict must be bool or null");
        strict_schema = strict->IsBool() && strict->boolean;
      }
      if (const auto* description = spec->Find("description");
          description && !description->IsString())
        return Status::Fail("schema description must be a string");
      schema = spec->Find("schema");
      if (!schema) return Status::Fail("missing response schema");
      status = ValidateOutputSchema(*schema, strict_schema);
      if (!status) return status;
    }
  }
  out->active = out->json_output || forced || strict_tools ||
                out->choice == OutputConstraints::ToolChoice::kNone ||
                (!out->parallel && has_tools);
  if (!out->active) return Status();
  if (!parsed)
    return Status::Fail("output constraints require parsed messages");
  if (request.Has("session_id") || request.Has("prefix_cache_scope"))
    return Status::Fail(
        "output constraints with session/prefix cache are not "
        "supported in this stage");
  if (out->json_output && has_tools &&
      out->choice != OutputConstraints::ToolChoice::kNone)
    return Status::Fail("response_format plus tools requires tool_choice=none");

  std::string payload;
  if (out->json_output) {
    payload = schema
                  ? SchemaFormat(*schema, false)
                  : R"({"type":"json_schema","json_schema":{"type":"object",)"
                    R"("additionalProperties":true},)"
                    R"("any_order":false,"max_whitespace_cnt":16})";
  } else if (out->choice == OutputConstraints::ToolChoice::kNone ||
             !has_tools) {
    payload = TextFormat();
  } else {
    if (tools->array.size() > 16)
      return Status::Fail("constrained requests allow at most 16 tools");
    std::unordered_set<std::string> names;
    std::string tags;
    for (const auto& tool : tools->array) {
      auto status = Fields(tool, {"type", "function"});
      if (!status) return status;
      const auto* fn = tool.Find("function");
      if (tool.GetString("type") != "function" || !fn)
        return Status::Fail("constraints require function tools");
      status = Fields(*fn, {"name", "description", "parameters", "strict"});
      if (!status) return status;
      const auto name = fn->GetString("name");
      if (!SafeName(name) || !names.insert(name).second)
        return Status::Fail("invalid or duplicate constrained tool name");
      if (const auto* description = fn->Find("description");
          description && !description->IsString())
        return Status::Fail("function description must be a string");
      // Even ignored tool definitions must not contain unsupported strict
      // schemas. None mode intentionally never compiles argument schemas.
      const auto parameters = Parameters(*fn);
      status = ValidateOutputSchema(parameters, fn->GetBool("strict"), true);
      if (!status) return status;
      if (out->choice == OutputConstraints::ToolChoice::kNamed &&
          name != out->named_tool)
        continue;
      if (!tags.empty()) tags += ',';
      tags += R"({"type":"tag","begin":)" +
              Quote("<tool_call>\n<function=" + name + ">\n") +
              R"(,"content":)" + SchemaFormat(parameters, true) +
              R"(,"end":"\n</function>\n</tool_call>"})";
    }
    if (tags.empty()) return Status::Fail("named tool is not declared");
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
    return Status::Fail("compiled request description exceeds 64 KiB");
  return Status();
}

io::Json ConstraintTemplateRequest(const io::Json& request,
                                   const OutputConstraints& plan) {
  io::Json result = request;
  for (auto& [key, value] : result.object) {
    if (key != "tools" || !value.IsArray()) continue;
    if (plan.choice == OutputConstraints::ToolChoice::kNone)
      value.array.clear();
    else if (plan.choice == OutputConstraints::ToolChoice::kNamed)
      std::erase_if(value.array, [&](const io::Json& tool) {
        const auto* fn = tool.Find("function");
        return !fn || fn->GetString("name") != plan.named_tool;
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
