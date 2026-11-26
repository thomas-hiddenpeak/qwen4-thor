#include "q4t/server/responses_python.h"
#include "q4t/server/responses_input.h"

#include <algorithm>
#include <charconv>
#include <deque>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/chat_tool_history.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/local_shell_history.h"
#include "q4t/server/file_input.h"
#include "q4t/server/file_video.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_file_search.h"
#include "q4t/server/responses_web_search.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_mcp_binding.h"
#include "q4t/server/responses_program.h"
#include "q4t/server/responses_tool_search.h"
#include "q4t/trace/sha256.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message) {
  *error = {"invalid_request", std::move(path)};
  return Status::Fail(std::move(message));
}
Status Unresolved(ChatPlanError* error, std::string path) {
  *error = {"responses_input_resolution_required", std::move(path), 503};
  return Status::Fail("Responses input requires client-scoped resolution");
}
Status DirectCaller(const Json& item, const std::string& path,
                    ChatPlanError* error) {
  const auto* caller = item.Find("caller");
  if (!caller || caller->IsNull()) return {};
  if (!caller->IsObject() || caller->GetString("type").empty())
    return Invalid(error, path + ".caller", "invalid tool caller");
  if (caller->GetString("type") != "direct")
    return Unresolved(error, path + ".caller");
  return InputFields(*caller, {"type"}, &error->param, path + ".caller");
}
Status TextField(const Json& value, const char* key, std::string_view path,
                 ChatPlanError* error) {
  const auto* field = value.Find(key);
  if (!field || !field->IsString())
    return Invalid(error, std::string(path) + '.' + key,
                   "expected a text field");
  return {};
}
Status ItemMetadata(const Json& item, std::string_view path,
                    ChatPlanError* error) {
  // These variants have their own status/nullable contract in the SDK graph.
  const auto kind = item.GetString("type");
  if (kind == "apply_patch_call" || kind == "apply_patch_call_output") {
    if (Nonnull(item, "id") &&
        (item.GetString("id").empty() || item.GetString("id").size() > 256))
      return Invalid(error, std::string(path) + ".id", "invalid item ID");
    return ValidateResponsesItemShape(item, ResponsesItemShape::kInput, error,
                                      path);
  }
  if (kind.starts_with("mcp_"))
    return ValidateResponsesItemShape(item, ResponsesItemShape::kInput, error,
                                      path);
  if (const auto* type = item.Find("type"); type && !type->IsString())
    return Invalid(error, std::string(path) + ".type", "invalid item type");
  if (const auto* id = item.Find("id");
      id && (!id->IsString() || id->str.empty() || id->str.size() > 256))
    return Invalid(error, std::string(path) + ".id", "invalid item ID");
  if (kind == "local_shell_call" || kind == "local_shell_call_output")
    return ValidateResponsesItemShape(item, ResponsesItemShape::kInput, error,
                                      path);
  if (const auto* status = item.Find("status");
      status && (!status->IsString() ||
                 (status->str != "in_progress" && status->str != "completed" &&
                  status->str != "incomplete" &&
                  !(item.GetString("type") == "file_search_call" &&
                    (status->str == "searching" || status->str == "failed")))))
    return Invalid(error, std::string(path) + ".status", "invalid item status");
  return {};
}

// Omitted Responses strict attempts compatible normalization. Never overwrite
// an explicit open map or discard an unsupported keyword just to make it pass.
bool StrictDefaults(Json* schema, size_t depth = 0) {
  if (!schema->IsObject() || depth > 64) return false;
  const auto* type = schema->Find("type");
  bool object = schema->Has("properties") ||
                (type && type->IsString() && type->str == "object");
  if (type && type->IsArray())
    for (const auto& item : type->array)
      object = object || (item.IsString() && item.str == "object");
  if (object) {
    const auto* additional = schema->Find("additionalProperties");
    if (additional && (!additional->IsBool() || additional->boolean))
      return false;
    Set(schema, "additionalProperties", Boolean(false));
    if (!schema->Has("properties")) Set(schema, "properties", Object());
    const auto* properties = schema->Find("properties");
    if (!properties->IsObject()) return false;
    Json required = Array();
    for (const auto& [name, ignored] : properties->object) {
      (void)ignored;
      required.array.push_back(String(name));
    }
    Set(schema, "required", std::move(required));
  }
  for (auto& [key, value] : schema->object) {
    if (key == "properties" || key == "$defs") {
      if (!value.IsObject()) return false;
      for (auto& [name, child] : value.object) {
        (void)name;
        if (!StrictDefaults(&child, depth + 1)) return false;
      }
    } else if (key == "items") {
      if (!StrictDefaults(&value, depth + 1)) return false;
    } else if (key == "anyOf" || key == "allOf" || key == "oneOf") {
      if (!value.IsArray()) return false;
      for (auto& child : value.array)
        if (!StrictDefaults(&child, depth + 1)) return false;
    }
  }
  return true;
}
Status Parts(const Json& value, const std::string& role, std::string_view path,
             Json* content, ChatPlanError* error, bool preparing_files,
             bool prepared_file_media) {
  if (value.IsString()) {
    *content = value;
    return {};
  }
  if (!value.IsArray())
    return Invalid(error, std::string(path), "content requires text or parts");
  if (value.array.empty()) {
    *content = String("");
    return {};
  }
  *content = Array();
  for (size_t i = 0; i < value.array.size(); ++i) {
    const auto& part = value.array[i];
    const auto at = std::string(path) + '[' + std::to_string(i) + ']';
    const auto type = part.GetString("type");
    Status status;
    bool cache_boundary = false;
    int cache_ttl = 0;
    status = ParsePromptCacheBreakpoint(
        part.Find("prompt_cache_breakpoint"), at + ".prompt_cache_breakpoint",
        &cache_boundary, &cache_ttl, &error->param);
    if (!status) return status;
    if (part.Has("prompt_cache_breakpoint") && type != "input_text" &&
        type != "input_image" && type != "input_file" &&
        !(prepared_file_media && type == "input_video_frames"))
      return Invalid(error, at + ".prompt_cache_breakpoint",
                     "Cache breakpoints require an input content block");
    Json mapped;
    if (preparing_files && (type == "input_file" || type == "input_image")) {
      FileInputSource source;
      const auto checked =
          type == "input_file"
              ? ParseFileInputSource(part, FileInputProfile::kResponsesStored,
                                     at, &source)
              : ParseImageInputSource(part, FileInputProfile::kResponsesStored,
                                      at, &source);
      if (!checked.ok()) {
        *error = {checked.code, checked.value.GetString("param"),
                  checked.http_status};
        return Status::Fail(checked.message);
      }
      // This is the actual unresolved part, not a surrogate text prompt.
      mapped = part;
    } else if (type == "input_text" || type == "output_text") {
      status = InputFields(part,
                           {"type", "text", "annotations", "logprobs",
                            "prompt_cache_breakpoint"},
                           &error->param, at);
      if (status) status = TextField(part, "text", at, error);
      if (!status) return status;
      if (type == "output_text" && role != "assistant")
        return Invalid(error, at + ".type", "output_text requires assistant");
      if (type == "input_text" &&
          (part.Has("annotations") || part.Has("logprobs")))
        return Invalid(error, at, "input_text has no output metadata");
      for (const auto* field : {"annotations", "logprobs"})
        if (const auto* data = part.Find(field); data && !data->IsArray())
          return Invalid(error, at + '.' + field,
                         "output metadata is an array");
      std::string text = part.GetString("text");
      if (const auto* annotations = part.GetArray("annotations");
          annotations && !annotations->array.empty())
        text += "\n[Response annotations: " + OutputJson(*annotations) + "]";
      mapped = Object({{"type", String("text")}, {"text", String(text)}});
    } else if (type == "refusal") {
      status = InputFields(part, {"type", "refusal"}, &error->param, at);
      if (status) status = TextField(part, "refusal", at, error);
      if (!status) return status;
      if (role != "assistant")
        return Invalid(error, at + ".type", "refusal requires assistant");
      mapped = part;
    } else if (type == "input_image") {
      status = InputFields(
          part,
          {"type", "detail", "image_url", "file_id", "prompt_cache_breakpoint"},
          &error->param, at);
      if (!status) return status;
      if (Nonnull(part, "file_id")) {
        if (!part.Find("file_id")->IsString() ||
            part.GetString("file_id").empty() || Nonnull(part, "image_url"))
          return Invalid(error, at, "image requires one URL or file ID");
        return Unresolved(error, at + ".file_id");
      }
      status = TextField(part, "image_url", at, error);
      if (!status) return status;
      const auto detail = part.GetString("detail", "auto");
      if (const auto* field = part.Find("detail");
          field &&
          (!field->IsString() || (detail != "auto" && detail != "low" &&
                                  detail != "high" && detail != "original")))
        return Invalid(error, at + ".detail", "invalid input image detail");
      // Preserve requested detail; the model/media planner must implement it,
      // never silently rewrite low/high/original to auto.
      mapped = Object({{"type", String("image_url")},
                       {"image_url", Object({{"url", *part.Find("image_url")},
                                             {"detail", String(detail)}})}});
    } else if (type == "input_video_frames" && prepared_file_media) {
      status = MapPreparedVideoContent(part, &mapped, &error->param);
      if (!status) return Invalid(error, at, status.message());
    } else if (type == "input_file") {
      return Unresolved(error, at);
    } else {
      return Invalid(error, at + ".type", "unresolved Responses content type");
    }
    if (cache_boundary)
      Set(&mapped, "prompt_cache_breakpoint",
          *part.Find("prompt_cache_breakpoint"));
    content->array.push_back(std::move(mapped));
  }
  return {};
}

Status ToolSelection(const Json& original, Json* choice, std::string_view path,
                     const std::vector<ResponsesToolIdentity>& identities,
                     ChatPlanError* error) {
  if (original.IsString()) {
    if (original.str != "none" && original.str != "auto" &&
        original.str != "required")
      return Invalid(error, std::string(path), "invalid tool choice mode");
    *choice = original;
    return {};
  }
  if (!original.IsObject())
    return Invalid(error, std::string(path),
                   "tool choice requires text or object");
  const auto type = original.GetString("type");
  if (type == "function" || type == "custom") {
    auto status = InputFields(original, {"type", "name"}, &error->param, path);
    if (!status) return status;
    if (!ValidInputName(original.GetString("name")))
      return Invalid(error, std::string(path) + ".name", "invalid tool name");
    auto name = original.GetString("name");
    const ResponsesToolIdentity* selected = nullptr;
    // Exact global names take precedence. Qualified names are an explicit
    // selector spelling; an unqualified duplicate never picks an arbitrary
    // namespace. The SDK selector has no separate namespace field.
    for (int pass = 0; pass < 3 && !selected; ++pass) {
      for (const auto& identity : identities) {
        if (identity.type != type) continue;
        const bool matches =
            pass == 0   ? identity.name_space.empty() && identity.name == name
            : pass == 1 ? !identity.name_space.empty() &&
                              identity.name_space + '.' + identity.name == name
                        : identity.name == name;
        if (!matches) continue;
        if (selected)
          return Invalid(error, std::string(path) + ".name",
                         "ambiguous tool selector; use a qualified name");
        selected = &identity;
      }
    }
    if (selected) name = selected->model_name;
    *choice = Object({{"type", String(type)},
                      {type, Object({{"name", String(std::move(name))}})}});
    return {};
  }
  if (type != "allowed_tools") return Unresolved(error, std::string(path));
  auto status =
      InputFields(original, {"type", "mode", "tools"}, &error->param, path);
  if (!status) return status;
  const auto mode = original.GetString("mode");
  const auto* tools = original.GetArray("tools");
  if ((mode != "auto" && mode != "required") || !tools || tools->array.empty())
    return Invalid(error, std::string(path), "invalid allowed_tools choice");
  auto items = Array();
  for (size_t i = 0; i < tools->array.size(); ++i) {
    const auto& item = tools->array[i];
    if (item.GetString("type") != "function" &&
        item.GetString("type") != "custom")
      return Unresolved(
          error, std::string(path) + ".tools[" + std::to_string(i) + ']');
    Json converted;
    status =
        ToolSelection(item, &converted,
                      std::string(path) + ".tools[" + std::to_string(i) + ']',
                      identities, error);
    if (!status) return status;
    items.array.push_back(std::move(converted));
  }
  *choice =
      Object({{"type", String(type)},
              {"allowed_tools",
               Object({{"mode", String(mode)}, {"tools", std::move(items)}})}});
  return {};
}

Status MapNamespaceChoice(const Json& original,
                          const std::vector<ResponsesToolIdentity>& identities,
                          Json* mapped, ChatPlanError* error,
                          std::string_view path = "tool_choice") {
  *mapped = original;
  const auto type = original.GetString("type");
  if (type == "local_shell" || type == "apply_patch") {
    auto status = InputFields(original, {"type"}, &error->param, path);
    if (!status) return status;
    const auto found = std::ranges::find_if(identities, [&](const auto& tool) {
      return tool.type == type && tool.direct;
    });
    if (found == identities.end())
      return Invalid(error, std::string(path),
                     "Selected client tool is not enabled for direct calls");
    *mapped = Object(
        {{"type", String("function")}, {"name", String(found->model_name)}});
  } else if (type == "function" || type == "custom") {
    Json selected;
    auto status = ToolSelection(original, &selected, path, identities, error);
    if (!status) return status;
    Set(mapped, "name", *selected.Find(type)->Find("name"));
  } else if (type == "allowed_tools") {
    const auto* selected = original.GetArray("tools");
    if (!selected) return {};
    auto items = Array();
    for (size_t i = 0; i < selected->array.size(); ++i) {
      Json item;
      auto status = MapNamespaceChoice(
          selected->array[i], identities, &item, error,
          std::string(path) + ".tools[" + std::to_string(i) + ']');
      if (!status) return status;
      items.array.push_back(std::move(item));
    }
    Set(mapped, "tools", std::move(items));
  }
  return {};
}
}  // namespace

io::Json ResponsesPatchFunction() {
  auto kinds = Array(
      {String("create_file"), String("update_file"), String("delete_file")});
  auto properties = Object({{"type", Object({{"type", String("string")},
                                             {"enum", std::move(kinds)}})},
                            {"path", Object({{"type", String("string")}})},
                            {"diff", Object({{"type", String("string")}})}});
  auto parameters =
      Object({{"type", String("object")},
              {"properties", std::move(properties)},
              {"required", Array({String("type"), String("path")})},
              {"additionalProperties", Boolean(false)}});
  return Object(
      {{"name", String(kApplyPatchToolName)},
       {"description", String("Request a patch in the client workspace. "
                              "Include diff for create and update, and only "
                              "the path for delete. Wait for the result.")},
       {"parameters", std::move(parameters)},
       {"strict", Boolean(false)}});
}

std::string ResponsesToolModelName(std::string_view name_space,
                                   std::string_view name,
                                   std::string_view type) {
  constexpr std::string_view prefix = "q4t_ns_";
  if (name_space.empty() && !name.starts_with(prefix) &&
      !name.starts_with("q4t_builtin_"))
    return std::string(name);
  const auto key =
      OutputJson(Array({String(std::string(name_space)),
                        String(std::string(name)), String(std::string(type))}));
  const auto hash =
      trace::Sha256({reinterpret_cast<const uint8_t*>(key.data()), key.size()});
  return std::string(prefix) + trace::HexDigest(hash).substr(0, 56);
}

Status PlanResponsesToolIdentities(
    const Json& request, std::vector<ResponsesToolIdentity>* identities,
    ChatPlanError* error) {
  identities->clear();
  *error = {};
  const auto* tools = request.Find("tools");
  if (!tools) return {};
  if (!tools->IsArray() || tools->array.size() > 128)
    return Invalid(error, "tools", "tools must be an array up to 128 items");
  std::unordered_set<std::string> namespaces, public_names, model_names;
  size_t count = 0;
  const auto add = [&](const Json& tool, const std::string& space,
                       const std::string& path) -> Status {
    if (++count > 128) {
      *error = {"tool_resource_exhausted", path, 503};
      return Status::Fail("expanded tools exceed the 128-callable budget");
    }
    const auto type = tool.GetString("type");
    if (type == "apply_patch") {
      auto status =
          InputFields(tool, {"type", "allowed_callers"}, &error->param, path);
      if (status) status = ValidateResponsesAllowedCallers(tool, error, path);
      if (!status) return status;
      if (!space.empty() || !model_names.insert(kApplyPatchToolName).second)
        return Invalid(error, path, "Invalid or duplicate patch tool");
      identities->push_back({type, type, "", kApplyPatchToolName, path, false,
                             ResponsesToolAllows(tool, "direct"),
                             ResponsesToolAllows(tool, "programmatic")});
      return {};
    }
    if (type == "local_shell") {
      auto status = InputFields(tool, {"type"}, &error->param, path);
      if (!status) return status;
      if (!model_names.insert(kLocalShellToolName).second)
        return Invalid(error, path, "Duplicate local shell tool");
      identities->push_back(
          {type, type, "", kLocalShellToolName, path, false, true, false});
      return {};
    }
    if (type != "function" && type != "custom") return {};
    const auto name = tool.GetString("name");
    if (!ValidInputName(name))
      return Invalid(error, path + ".name", "invalid tool name");
    const auto key = OutputJson(Array({String(space), String(name)}));
    if (!public_names.insert(key).second)
      return Invalid(error, path + ".name", "duplicate tool in namespace");
    const auto model_name = ResponsesToolModelName(space, name, type);
    if (!model_names.insert(model_name).second)
      return Invalid(error, path + ".name", "model tool identity collision");
    const auto* async = tool.Find("async");
    if (async && !async->IsBool())
      return Invalid(error, path + ".async", "async must be boolean");
    auto callers = ValidateResponsesAllowedCallers(tool, error, path);
    if (!callers) return callers;
    identities->push_back({type, name, space, model_name, path,
                           tool.GetBool("async"),
                           ResponsesToolAllows(tool, "direct"),
                           ResponsesToolAllows(tool, "programmatic")});
    return {};
  };
  for (size_t i = 0; i < tools->array.size(); ++i) {
    const auto& tool = tools->array[i];
    const auto path = "tools[" + std::to_string(i) + ']';
    if (tool.GetString("type") != "namespace") {
      auto status = add(tool, "", path);
      if (!status) return status;
      continue;
    }
    auto status = InputFields(tool, {"type", "name", "description", "tools"},
                              &error->param, path);
    if (!status) return status;
    const auto space = tool.GetString("name");
    if (!ValidInputName(space) || !namespaces.insert(space).second)
      return Invalid(error, path + ".name", "invalid or duplicate namespace");
    if (!tool.Find("description") || !tool.Find("description")->IsString())
      return Invalid(error, path + ".description",
                     "namespace description required");
    const auto* children = tool.GetArray("tools");
    if (!children || children->array.empty())
      return Invalid(error, path + ".tools", "namespace requires tools");
    for (size_t j = 0; j < children->array.size(); ++j) {
      const auto& child = children->array[j];
      const auto at = path + ".tools[" + std::to_string(j) + ']';
      if (child.GetString("type") != "function" &&
          child.GetString("type") != "custom")
        return Invalid(error, at + ".type",
                       "namespace children require function or custom tools");
      status = add(child, space, at);
      if (!status) return status;
    }
  }
  return {};
}

Status NormalizeResponsesInput(const io::Json& request,
                               ResponsesInputPlan* result, ChatPlanError* error,
                               bool allow_empty, bool preparing_files,
                               bool preparing_mcp, const Json* mcp_content,
                               bool prepared_file_media,
                               const Json* web_content, bool multi_agent) {
  *result = {};
  *error = {};
  ResponsesInputPlan plan;
  plan.items = Array();
  plan.messages = Array();
  const auto append = [&](Json message, std::string path) {
    // Carry annotations through phase insertion/refusal splitting/tool order,
    // then keep them separate from the model's closed Chat request shape.
    if (const auto* value = message.Find("content");
        value && value->IsArray()) {
      auto& content = Field(message, "content");
      for (size_t i = 0; i < content.array.size(); ++i) {
        auto& part = content.array[i];
        if (const auto* boundary = part.Find("prompt_cache_breakpoint")) {
          const auto ttl = boundary->GetString("ttl");
          plan.cache_boundaries.push_back(
              {plan.messages.array.size(), i,
               ttl.empty() ? 0 : (ttl == "12h" ? 43200 : 1800)});
          std::erase_if(part.object, [](const auto& field) {
            return field.first == "prompt_cache_breakpoint";
          });
        }
      }
    }
    plan.messages.array.push_back(std::move(message));
    plan.message_paths.push_back(std::move(path));
  };
  if (const auto* instructions = request.Find("instructions");
      instructions && !instructions->IsNull()) {
    if (!instructions->IsString())
      return Invalid(error, "instructions",
                     "instructions requires text or null");
    append(Object({{"role", String("developer")}, {"content", *instructions}}),
           "instructions");
  }
  const auto* input = request.Find("input");
  if (input && input->IsString()) {
    plan.items.array.push_back(Object({{"type", String("message")},
                                       {"role", String("user")},
                                       {"content", *input}}));
  } else if (input && input->IsArray()) {
    plan.items = *input;
  } else if (input) {
    return Invalid(error, "input", "input requires text or an array of items");
  }
  if (plan.items.array.size() > 4096)
    return Invalid(error, "input", "input exceeds 4096 items");

  ResponsesToolHistory discovery;
  const auto* declared = request.Find("tools");
  const bool dynamic =
      HasResponsesToolDiscovery(declared ? *declared : Json(), plan.items);
  std::unordered_map<std::string, bool> call_formats;
  const auto remember_formats = [&](const Json& tools) {
    for (const auto& tool : tools.array)
      if (const auto* function = tool.Find("function"))
        call_formats[function->GetString("name")] =
            NeedsJsonToolArguments(*function);
  };
  if (dynamic) {
    auto status = ResolveResponsesToolHistory(declared ? *declared : Array(),
                                              plan.items, &discovery, error);
    if (!status) return status;
    auto initial = discovery.initial.eager;
    if (!discovery.initial.search.IsNull())
      initial.array.push_back(discovery.initial.search);
    Json native, ignored;
    status = NormalizeActiveResponsesTools(
        Object({{"tools", initial}, {"input", plan.items}}), discovery.initial,
        &native, &ignored, error, preparing_mcp);
    if (!status) return status;
    remember_formats(native);
  }
  size_t activation = 0;
  std::deque<std::string> server_searches;
  LocalShellHistory local_shell;
  std::unordered_set<std::string> patch_calls;

  Json assistant =
      Object({{"role", String("assistant")}, {"content", String("")}});
  Json calls = Array();
  std::vector<std::pair<Json, std::string>> results;
  std::unordered_map<std::string, bool> expected;
  std::unordered_map<std::string, std::pair<std::string, std::string>> names;
  std::unordered_set<std::string> returned, call_ids, item_ids, async_ids;
  std::vector<size_t> async_function_inputs;
  std::unordered_set<std::string> program_ids, program_results;
  std::unordered_map<std::string, std::pair<std::string, Json>> agent_actions;
  struct ProgramChild {
    std::string parent;
    std::string output_type;
  };
  std::unordered_map<std::string, ProgramChild> program_children;
  std::unordered_set<std::string> program_child_results;
  std::string reasoning, assistant_path;
  size_t automatic_returns = 0;
  const auto flush = [&]() -> Status {
    for (const auto& call : calls.array)
      if (!returned.contains(call.GetString("id")) &&
          !async_ids.contains(call.GetString("id")))
        return Invalid(error, assistant_path, "missing output for a tool call");
    if (!reasoning.empty() || !calls.array.empty() ||
        !assistant.GetString("content").empty()) {
      if (!reasoning.empty()) Set(&assistant, "reasoning", String(reasoning));
      if (!calls.array.empty()) Set(&assistant, "tool_calls", calls);
      append(assistant, assistant_path);
    }
    for (auto& [message, path] : results)
      append(std::move(message), std::move(path));
    assistant =
        Object({{"role", String("assistant")}, {"content", String("")}});
    calls = Array();
    results.clear();
    automatic_returns = 0;
    reasoning.clear();
    assistant_path.clear();
    return {};
  };

  for (size_t i = 0; i < plan.items.array.size(); ++i) {
    const auto& item = plan.items.array[i];
    const auto path = input && input->IsString()
                          ? "input"
                          : "input[" + std::to_string(i) + ']';
    if (!item.IsObject())
      return Invalid(error, path, "input item must be object");
    const auto type = item.GetString("type", "message");
    const bool agent_call = type == "multi_agent_call";
    const bool agent_item =
        multi_agent && (agent_call || type == "multi_agent_call_output");
    auto status = agent_item
        ? ValidateMultiAgentShape(item, agent_call ? MultiAgentShape::kInputCall
                                                   : MultiAgentShape::kInputResult,
                                  error, path)
        : ItemMetadata(item, path, error);
    if (!status) return status;
    if (Nonnull(item, "id") && !item_ids.insert(item.GetString("id")).second)
      return Invalid(error, path + ".id", "duplicate input item ID");
    if (agent_item) {
      const auto id = item.GetString("call_id");
      const auto action = item.GetString("action");
      if (Nonnull(item, "id") &&
          (item.GetString("id").empty() || item.GetString("id").size() > 256))
        return Invalid(error, path + ".id", "Invalid input item ID");
      if (id.empty() || id.size() > 256)
        return Invalid(error, path + ".call_id", "Invalid multi-agent call ID");
      if (agent_call) {
        if (!results.empty() && calls.array.size() == results.size()) {
          status = flush();
          if (!status) return status;
        }
        Json arguments;
        if (!io::ParseJson(item.GetString("arguments"), &arguments, true,
                           kSchemaJsonLimits) || !arguments.IsObject())
          return Invalid(error, path + ".arguments", "Invalid action arguments");
        if (!call_ids.insert(id).second)
          return Invalid(error, path + ".call_id", "Duplicate multi-agent call");
        const auto* owner = item.Find("agent");
        agent_actions.try_emplace(id, action, owner ? *owner : Json());
        if (assistant_path.empty()) assistant_path = path;
        calls.array.push_back(Object({
            {"id", String(id)}, {"type", String("function")},
            {"function", Object({{"name", String("multi_agent_" + action)},
                                 {"arguments", *item.Find("arguments")}})}}));
      } else {
        const auto match = agent_actions.find(id);
        if (match == agent_actions.end() || match->second.first != action ||
            !returned.insert(id).second)
          return Invalid(error, path + ".call_id",
                         "Unmatched or duplicate multi-agent result");
        // Storage assigns fresh item IDs to independent count/create calls.
        // Only the public action, participants and result enter the prompt.
        auto record = Object({{"action", String(action)},
                              {"output", *item.Find("output")}});
        if (const auto* agent = item.Find("agent"))
          Set(&record, "agent", *agent);
        if (!match->second.second.IsNull())
          Set(&record, "call_agent", match->second.second);
        results.emplace_back(Object({{"role", String("tool")},
                                     {"tool_call_id", String(id)},
                                     {"content", String(OutputJson(record))}}),
                             path);
        ++automatic_returns;
      }
    } else if (type == "mcp_list_tools") {
      // The request owner authenticates this catalog. The actual definitions
      // are rendered by the model tool adapter, not as a privileged message.
      // Preserve listing failures as data instead of claiming an empty list.
      if (Nonnull(item, "error")) {
        if (assistant_path.empty()) assistant_path = path;
        Set(&assistant, "content",
            String(assistant.GetString("content") +
                   "\n[MCP tool listing failed]\n" + OutputJson(item)));
      }
    } else if (type == "mcp_approval_request" ||
               type == "mcp_approval_response") {
      // Permission metadata is not a completed tool execution. Keep the
      // actual decision/arguments in their participant role, never synthesize
      // a successful tool result or dispatch from this pure projection.
      status = flush();
      if (!status) return status;
      append(
          Object({{"role", String(type == "mcp_approval_request" ? "assistant"
                                                                 : "user")},
                  {"content", String("[MCP approval]\n" + OutputJson(item))}}),
          path);
    } else if (type == "mcp_call") {
      const auto terminal = item.GetString("status");
      if (terminal != "completed" && terminal != "failed") {
        if (assistant_path.empty()) assistant_path = path;
        Set(&assistant, "content",
            String(assistant.GetString("content") +
                   "\n[Unfinished MCP call; no result]\n" + OutputJson(item)));
        continue;
      }
      if (results.size() > automatic_returns) {
        status = flush();
        if (!status) return status;
      }
      const auto id = item.GetString("id");
      if (!call_ids.insert(id).second)
        return Invalid(error, path + ".id", "Duplicate MCP call identity");
      Json arguments;
      if (!io::ParseJson(item.GetString("arguments"), &arguments, true,
                         kSchemaJsonLimits) ||
          !arguments.IsObject())
        return Invalid(error, path + ".arguments", "Invalid MCP arguments");
      const auto model_name = ResponsesMcpModelName(
          item.GetString("server_label"), item.GetString("name"));
      if (assistant_path.empty()) assistant_path = path;
      calls.array.push_back(Object(
          {{"id", String(id)},
           {"type", String("function")},
           {"function", Object({{"name", String(model_name)},
                                {"arguments", *item.Find("arguments")}})}}));
      // A result remains source data. The private invocation owner, not this
      // projection, validates original input/output schemas and execution.
      auto result = Object({{"status", String(terminal)},
                            {"output", Json()},
                            {"error", Json()}});
      for (const auto* key : {"output", "error"})
        if (const auto* value = item.Find(key)) Set(&result, key, *value);
      Json content = String(OutputJson(result));
      if (mcp_content) {
        const auto* prepared = mcp_content->Find(id);
        if (!prepared || !prepared->IsArray())
          return Unresolved(error, path + ".output");
        status = Parts(*prepared, "tool", path + ".output", &content, error,
                       preparing_files, true);
        if (!status) return status;
      }
      results.emplace_back(Object({{"role", String("tool")},
                                   {"tool_call_id", String(id)},
                                   {"content", std::move(content)}}),
                           path);
      expected.emplace(id, false);
      returned.insert(id);
      ++automatic_returns;
    } else if (type == "program" || type == "program_output") {
      status = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                          error, path);
      if (!status) return status;
      const auto id = item.GetString("call_id");
      if (id.empty() || id.size() > 256)
        return Invalid(error, path + ".call_id", "Invalid program call ID");
      if (type == "program") {
        if (!results.empty()) {
          status = flush();
          if (!status) return status;
        }
        if (!call_ids.insert(id).second || !program_ids.insert(id).second)
          return Invalid(error, path, "Duplicate program call");
        // The execution owner authenticates the fingerprint before any
        // dispatch. Template history contains code/result, never sealed state
        // or intermediate client outputs intended only for program execution.
        if (assistant_path.empty()) assistant_path = path;
        calls.array.push_back(Object(
            {{"id", String(id)},
             {"type", String("function")},
             {"function", Object({{"name", String(kResponsesProgramModelName)},
                                  {"arguments",
                                   String(OutputJson(Object(
                                       {{"code", *item.Find("code")}})))}})}}));
        expected.emplace(id, false);
        async_ids.insert(id);
        plan.async_tool_call_ids.push_back(id);
      } else {
        if (!program_ids.contains(id) || !program_results.insert(id).second)
          return Invalid(error, path, "Orphan or duplicate program output");
        returned.insert(id);
        Json content, mapped;
        status =
            DecodeResponsesProgramOutput(item.GetString("result"), &content);
        if (status)
          status = Parts(content, "tool", path + ".result", &mapped, error,
                         preparing_files, true);
        if (!status) return status;
        results.emplace_back(Object({{"role", String("tool")},
                                     {"tool_call_id", String(id)},
                                     {"content", std::move(mapped)}}),
                             path);
        ++automatic_returns;
      }
    } else if (const auto* caller = item.Find("caller");
               caller && caller->GetString("type") == "program") {
      status = InputFields(*caller, {"type", "caller_id"}, &error->param,
                           path + ".caller");
      if (!status) return status;
      const auto parent = caller->GetString("caller_id");
      const auto id = item.GetString("call_id");
      if (!program_ids.contains(parent) || program_results.contains(parent) ||
          id.empty() || id.size() > 256)
        return Invalid(error, path, "Invalid program child relationship");
      const bool invocation =
          type == "function_call" || type == "custom_tool_call" ||
          type == "apply_patch_call";
      const bool result =
          type == "function_call_output" || type == "custom_tool_call_output" ||
          type == "apply_patch_call_output";
      if (!invocation && !result)
        return Invalid(error, path, "Invalid program caller item");
      auto public_item = item;
      if (type == "custom_tool_call" || type == "custom_tool_call_output")
        std::erase_if(public_item.object, [](const auto& field) {
          return field.first == "status" || field.first == "created_by";
        });
      status = ValidateResponsesItemShape(
          public_item, ResponsesItemShape::kInput, error, path);
      if (!status) return status;
      if (invocation) {
        if (!call_ids.insert(id).second ||
            !program_children.emplace(id, ProgramChild{parent, type + "_output"})
                 .second ||
            item.GetString("status", "completed") != "completed")
          return Invalid(error, path, "Invalid program child call");
      } else if (const auto child = program_children.find(id);
                 child == program_children.end() ||
                 child->second.parent != parent ||
                 child->second.output_type != type ||
                 !program_child_results.insert(id).second) {
        return Invalid(error, path, "Orphan or duplicate program child result");
      }
    } else if (program_children.contains(item.GetString("call_id"))) {
      return Invalid(error, path + ".caller",
                     "Program child requires its caller");
    } else if (type == "message") {
      status = flush();
      if (!status) return status;
      const auto* source = multi_agent ? item.Find("agent") : nullptr;
      status = source
          ? ValidateMultiAgentShape(item, MultiAgentShape::kOutputMessage,
                                    error, path)
          : InputFields(item,
                           {"id", "type", "role", "content", "status", "phase"},
                           &error->param, path);
      if (!status) return status;
      const auto role = item.GetString("role");
      if (role != "user" && role != "assistant" && role != "system" &&
          role != "developer")
        return Invalid(error, path + ".role", "invalid Responses message role");
      const auto* value = item.Find("content");
      if (!value) return Invalid(error, path + ".content", "content required");
      Json content;
      status = Parts(*value, role, path + ".content", &content, error,
                     preparing_files, prepared_file_media);
      if (!status) return status;
      if (source && source->IsObject() && source->GetString("agent_name") != "root") {
        auto record = Object({{"agent", *source}, {"content", std::move(content)}});
        append(Object({{"role", String("user")},
                       {"content", String("[Agent message]\n" + OutputJson(record))}}), path);
        continue;
      }
      if (const auto* phase = item.Find("phase"); phase && !phase->IsNull()) {
        if (role != "assistant" || !phase->IsString() ||
            (phase->str != "commentary" && phase->str != "final_answer"))
          return Invalid(error, path + ".phase", "invalid assistant phase");
        // Qwen has no phase token. Preserve the semantic tag as assistant
        // content, without promoting it to an instruction/participant role.
        if (content.IsString())
          content =
              Array({Object({{"type", String("text")}, {"text", content}})});
        content.array.insert(
            content.array.begin(),
            Object(
                {{"type", String("text")},
                 {"text", String("[Response phase: " + phase->str + "]\n")}}));
      }
      const bool mixed_refusal =
          content.IsArray() && content.array.size() > 1 &&
          std::any_of(content.array.begin(), content.array.end(),
                      [](const Json& part) {
                        return part.GetString("type") == "refusal";
                      });
      if (mixed_refusal) {
        for (auto& part : content.array)
          append(Object({{"role", String(role)},
                         {"content", Array({std::move(part)})}}),
                 path);
      } else {
        append(
            Object({{"role", String(role)}, {"content", std::move(content)}}),
            path);
      }
    } else if (type == "reasoning") {
      if (!results.empty()) {
        status = flush();
        if (!status) return status;
      }
      status = InputFields(
          item,
          {"id", "type", "summary", "content", "encrypted_content", "status"},
          &error->param, path);
      if (!status) return status;
      if (!item.Has("id") || !item.GetArray("summary"))
        return Invalid(error, path,
                       "reasoning item requires ID and summary array");
      if (Nonnull(item, "encrypted_content"))
        return Unresolved(error, path + ".encrypted_content");
      if (assistant_path.empty()) assistant_path = path;
      for (const char* key : {"content", "summary"}) {
        const auto* values = item.Find(key);
        if (!values) continue;
        if (!values->IsArray())
          return Invalid(error, path + '.' + key,
                         "reasoning field is an array");
        for (size_t j = 0; j < values->array.size(); ++j) {
          const auto& part = values->array[j];
          const auto at = path + '.' + key + '[' + std::to_string(j) + ']';
          status = InputFields(part, {"type", "text"}, &error->param, at);
          if (status) status = TextField(part, "text", at, error);
          if (!status) return status;
          if (part.GetString("type") != (std::string_view(key) == "content"
                                             ? "reasoning_text"
                                             : "summary_text"))
            return Invalid(error, at + ".type", "invalid reasoning part type");
          if (std::string_view(key) == "summary")
            reasoning += "\n[Reasoning summary]\n";
          reasoning += part.GetString("text");
        }
      }
    } else if (type == "tool_search_call") {
      if (!results.empty()) {
        status = flush();
        if (!status) return status;
      }
      if (item.GetString("status", "completed") != "completed") {
        // A skipped/incomplete parallel call may precede a sibling's result.
        // Keep the explicit record as assistant data without closing that
        // sibling early or inventing a result for the unfinished call.
        if (assistant_path.empty()) assistant_path = path;
        Set(&assistant, "content",
            String(assistant.GetString("content") +
                   "\n[Unfinished tool discovery]\n" + OutputJson(item)));
        continue;
      }
      const bool server = item.GetString("execution", "client") == "server";
      auto id = item.GetString("call_id");
      if (server) {
        id = "history_tool_search_" + std::to_string(i);
        while (call_ids.contains(id) ||
               std::any_of(plan.items.array.begin(), plan.items.array.end(),
                           [&](const auto& value) {
                             return value.GetString("call_id") == id;
                           }))
          id += '_';
        server_searches.push_back(id);
      }
      if (!call_ids.insert(id).second)
        return Invalid(error, path + ".call_id", "Duplicate tool call ID");
      if (assistant_path.empty()) assistant_path = path;
      const auto name = std::string(kResponsesToolSearchModelName);
      calls.array.push_back(Object(
          {{"id", String(id)},
           {"type", String("function")},
           {"function",
            Object({{"name", String(name)},
                    {"arguments",
                     String(OutputJson(Object(
                         {{"arguments_json", String(OutputJson(*item.Find(
                                                 "arguments")))}})))}})}}));
      plan.tool_call_formats.push_back({id, call_formats[name]});
      expected.emplace(id, false);
    } else if (type == "tool_search_output" || type == "additional_tools") {
      if (type == "tool_search_output" &&
          item.GetString("status", "completed") == "in_progress")
        continue;
      std::string definitions;
      if (activation < discovery.activations.size() &&
          discovery.activations[activation].item_index == i) {
        Json native, ignored;
        status = NormalizeActiveResponsesTools(
            Object({{"tools", discovery.activations[activation].definitions},
                    {"input", plan.items}}),
            discovery.initial, &native, &ignored, error, preparing_mcp);
        if (status) status = RenderChatToolDefinitions(native, &definitions);
        if (!status) return status;
        remember_formats(native);
        ++activation;
      }
      if (type == "additional_tools") {
        status = flush();
        if (!status) return status;
        append(Object({{"role", String("developer")},
                       {"content", String("Additional available tools:\n" +
                                          definitions)}}),
               path);
        continue;
      }
      auto id = item.GetString("call_id");
      if (item.GetString("execution", "client") == "server") {
        if (server_searches.empty())
          return Invalid(error, path, "Orphan server discovery result");
        id = server_searches.front();
        server_searches.pop_front();
      }
      if (!expected.contains(id) || !returned.insert(id).second)
        return Invalid(error, path + ".call_id", "Invalid discovery result");
      results.emplace_back(
          Object({{"role", String("tool")},
                  {"tool_call_id", String(id)},
                  {"content", String("Tool discovery status: " +
                                     item.GetString("status", "completed") +
                                     "\n" + definitions)}}),
          path);
      // Definitions take effect at this result, before the next model call.
      // Keep simultaneous search results together until every call closes.
      const bool closed = std::all_of(
          calls.array.begin(), calls.array.end(), [&](const auto& call) {
            const auto key = call.GetString("id");
            return returned.contains(key) || async_ids.contains(key);
          });
      if (closed) {
        status = flush();
        if (!status) return status;
      }
    } else if (type == "code_interpreter_call") {
      status = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                          error, path);
      if (!status) return status;
      const auto prefix = "history_python_" + std::to_string(i);
      auto id = prefix;
      for (size_t n = 0; call_ids.contains(id) || expected.contains(id); ++n)
        id = prefix + "_" + std::to_string(n);
      auto args = Object({{"code", String(item.GetString("code"))}});
      calls.array.push_back(Object(
          {{"id", String(id)},
           {"type", String("function")},
           {"function", Object({{"name", String(kPythonToolName)},
                                {"arguments", String(OutputJson(args))}})}}));
      auto data =
          Object({{"status", String(item.GetString("status"))},
                  {"container_id", String(item.GetString("container_id"))}});
      if (const auto* outputs = item.Find("outputs"))
        Set(&data, "outputs", *outputs);
      results.emplace_back(Object({{"role", String("tool")},
                                   {"tool_call_id", String(id)},
                                   {"content", String(OutputJson(data))}}),
                           path);
      expected.emplace(id, false);
      returned.insert(id);
      ++automatic_returns;
    } else if (type == "web_search_call") {
      status = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                          error, path);
      if (!status) return status;
      std::string id = "history_web_search_" + std::to_string(i);
      const auto base = id;
      for (size_t suffix = 0;
           call_ids.contains(id) || std::any_of(plan.items.array.begin(), plan.items.array.end(),
             [&](const auto& value) { return value.GetString("call_id") == id; }); ++suffix)
        id = base + '_' + std::to_string(suffix);
      calls.array.push_back(Object({{"id", String(id)}, {"type", String("function")},
        {"function", Object({{"name", String("web_search")},
          {"arguments", String(OutputJson(*item.Find("action")))}})}}));
      const auto* context = web_content ? FindWebContext(*web_content, item) : nullptr;
      const auto result = Object({{"status", *item.Find("status")},
        {"results_available", Boolean(context != nullptr)},
        {"retrieved_sources", context ? *context : Json()}});
      results.emplace_back(Object({{"role", String("tool")}, {"tool_call_id", String(id)},
        {"content", String(OutputJson(result))}}), path);
      expected.emplace(id, false);
      returned.insert(id);
      ++automatic_returns;
    } else if (type == "file_search_call") {
      status = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                          error, path);
      if (!status) return status;
      // This is historical evidence, not a request to execute a search. Keep
      // missing results explicitly unknown, never re-fetch them or treat a
      // searching/failed item as a successfully completed empty search.
      std::string id = "history_file_search_" + std::to_string(i);
      const auto collides = [&](const std::string& candidate) {
        return call_ids.contains(candidate) ||
               std::any_of(plan.items.array.begin(), plan.items.array.end(),
                           [&](const Json& value) {
                             return value.GetString("call_id") == candidate;
                           });
      };
      while (collides(id)) id += '_';
      call_ids.insert(id);
      if (assistant_path.empty()) assistant_path = path;
      calls.array.push_back(
          Object({{"id", String(id)},
                  {"type", String("function")},
                  {"function",
                   Object({{"name", String("file_search")},
                           {"arguments",
                            String(OutputJson(Object(
                                {{"queries", *item.Find("queries")}})))}})}}));
      const auto* data = item.GetArray("results");
      auto passages = data ? *data : Json();
      for (auto& passage : passages.array) {
        const auto file = passage.GetString("file_id");
        if (!file.empty())
          Set(&passage, "citation_marker", String("[file:" + file + ']'));
      }
      const auto result =
          Object({{"status", *item.Find("status")},
                  {"results_available", Boolean(data != nullptr)},
                  {"results", std::move(passages)}});
      results.emplace_back(Object({{"role", String("tool")},
                                   {"tool_call_id", String(id)},
                                   {"content", String(OutputJson(result))}}),
                           path);
      expected.emplace(id, false);
      returned.insert(id);
      ++automatic_returns;
    } else if (type == "apply_patch_call" || type == "apply_patch_call_output") {
      const auto id = item.GetString("call_id");
      if (id.empty() || id.size() > 256)
        return Invalid(error, path + ".call_id", "Invalid patch call ID");
      status = DirectCaller(item, path, error);
      if (!status) return status;
      if (type == "apply_patch_call") {
        if (item.GetString("status") != "completed")
          return Invalid(error, path + ".status", "Patch call is unfinished");
        if (results.size() > automatic_returns) {
          status = flush();
          if (!status) return status;
        }
        if (!call_ids.insert(id).second)
          return Invalid(error, path + ".call_id", "Duplicate patch call ID");
        patch_calls.insert(id);
        if (assistant_path.empty()) assistant_path = path;
        auto function = Object({
            {"name", String(kApplyPatchToolName)},
            {"arguments", String(OutputJson(*item.Find("operation")))}});
        calls.array.push_back(Object({{"id", String(id)},
                                      {"type", String("function")},
                                      {"function", std::move(function)}}));
      } else {
        if (!patch_calls.contains(id) || !returned.insert(id).second)
          return Invalid(error, path + ".call_id",
                         "Orphan or duplicate patch result");
        auto outcome = Object({{"status", *item.Find("status")}});
        if (const auto* text = item.Find("output"))
          Set(&outcome, "output", *text);
        results.emplace_back(Object({{"role", String("tool")},
                                     {"tool_call_id", String(id)},
                                     {"content", String(OutputJson(outcome))}}),
                             path);
      }
    } else if (type == "local_shell_call" ||
               type == "local_shell_call_output") {
      if (item.GetString("status", "completed") != "completed")
        return Invalid(error, path + ".status",
                       "Expected a completed shell item");
      if (type == "local_shell_call") {
        if (results.size() > automatic_returns) {
          status = flush();
          if (!status) return status;
        }
        const auto id = item.GetString("call_id");
        if (id.size() > 256 || !call_ids.insert(id).second ||
            !local_shell.Register(item))
          return Invalid(error, path + ".call_id",
                         "Invalid local shell call ID");
        if (assistant_path.empty()) assistant_path = path;
        calls.array.push_back(Object(
            {{"id", String(id)},
             {"type", String("function")},
             {"function", Object({{"name", String(kLocalShellToolName)},
                                  {"arguments", String(OutputJson(*item.Find(
                                                    "action")))}})}}));
      } else {
        const auto id = local_shell.Target(item);
        if (id.empty() || !returned.insert(id).second)
          return Invalid(error,
                         path + (item.Has("call_id") ? ".call_id" : ".id"),
                         "Orphan, ambiguous or duplicate local shell result");
        results.emplace_back(Object({{"role", String("tool")},
                                     {"tool_call_id", String(id)},
                                     {"content", *item.Find("output")}}),
                             path);
      }
    } else if (type == "function_call" || type == "custom_tool_call") {
      if (results.size() > automatic_returns) {
        // A result followed by a new call starts another assistant step.
        // flush() verifies every synchronous call in the prior group.
        status = flush();
        if (!status) return status;
      }
      const bool custom = type == "custom_tool_call";
      status = InputFields(item,
                           {"id", "type", "call_id", "name", "arguments",
                            "input", "status", "async", "caller", "namespace"},
                           &error->param, path);
      if (!status) return status;
      status = DirectCaller(item, path, error);
      if (!status) return status;
      if (Nonnull(item, "namespace") &&
          !ValidInputName(item.GetString("namespace")))
        return Invalid(error, path + ".namespace", "invalid tool namespace");
      if (const auto* async = item.Find("async")) {
        if (!async->IsBool())
          return Invalid(error, path + ".async", "async must be boolean");
      }
      const auto id = item.GetString("call_id");
      if (id.empty() || id.size() > 256 || !call_ids.insert(id).second)
        return Invalid(error, path + ".call_id",
                       "invalid or duplicate call ID");
      if (item.GetBool("async")) {
        async_ids.insert(id);
        plan.async_tool_call_ids.push_back(id);
      }
      if (!ValidInputName(item.GetString("name")))
        return Invalid(error, path + ".name", "invalid tool name");
      status = TextField(item, custom ? "input" : "arguments", path, error);
      if (!status) return status;
      if (item.Has(custom ? "arguments" : "input"))
        return Invalid(error, path, "tool payload does not match its type");
      const auto name = item.GetString("name");
      const auto space = item.GetString("namespace");
      const auto model_name =
          ResponsesToolModelName(space, name, custom ? "custom" : "function");
      if (dynamic && !custom)
        plan.tool_call_formats.push_back({id, call_formats[model_name]});
      names.emplace(id, std::make_pair(name, space));
      const char* field = custom ? "input" : "arguments";
      const auto& raw = *item.Find(field);
      if (!custom) {
        if (raw.str.size() > kSchemaJsonLimits.max_string_bytes)
          return Invalid(error, path + ".arguments",
                         "argument text exceeds the request budget");
        if (item.GetBool("async")) async_function_inputs.push_back(i);
      }
      Json payload = Object({{"name", String(model_name)}, {field, raw}});
      if (assistant_path.empty()) assistant_path = path;
      calls.array.push_back(
          Object({{"id", String(id)},
                  {"type", String(custom ? "custom" : "function")},
                  {custom ? "custom" : "function", std::move(payload)}}));
      expected.emplace(id, custom);
    } else if (type == "function_call_output" ||
               type == "custom_tool_call_output") {
      status = InputFields(item,
                           {"id", "type", "call_id", "output", "status", "name",
                            "namespace", "caller"},
                           &error->param, path);
      if (!status) return status;
      status = DirectCaller(item, path, error);
      if (!status) return status;
      const auto id = item.GetString("call_id");
      const auto match = expected.find(id);
      if (match == expected.end() || !returned.insert(id).second ||
          match->second != (type == "custom_tool_call_output"))
        return Invalid(error, path + ".call_id",
                       "orphan, duplicate or mismatched output");
      if (const auto* name = item.Find("name"); name && !name->IsNull()) {
        if (!name->IsString() || name->str != names.at(id).first)
          return Invalid(error, path + ".name", "tool output name mismatch");
      }
      if (const auto* space = item.Find("namespace"); space && !space->IsNull())
        if (!space->IsString() || space->str != names.at(id).second)
          return Invalid(error, path + ".namespace",
                         "tool output namespace mismatch");
      const auto* value = item.Find("output");
      if (!value)
        return Invalid(error, path + ".output", "tool output required");
      Json content;
      status = Parts(*value, "tool", path + ".output", &content, error,
                     preparing_files, prepared_file_media);
      if (!status) return status;
      results.emplace_back(Object({{"role", String("tool")},
                                   {"tool_call_id", String(id)},
                                   {"content", std::move(content)}}),
                           path);
    } else {
      return Unresolved(error, path);
    }
  }
  auto status = flush();
  if (!status) return status;
  // Completed calls are historical model output, including text rejected by
  // the client. Only still-pending async functions need executable arguments.
  for (const auto index : async_function_inputs) {
    const auto& item = plan.items.array[index];
    if (returned.contains(item.GetString("call_id"))) continue;
    Json arguments;
    if (!io::ParseJson(item.Find("arguments")->str, &arguments, true,
                       kSchemaJsonLimits) ||
        !arguments.IsObject())
      return Invalid(error, "input[" + std::to_string(index) + "].arguments",
                     "pending function call requires JSON object text");
  }
  if (!allow_empty && plan.messages.array.empty())
    return Invalid(error, "input", "input or instructions required");
  *result = std::move(plan);
  return {};
}

void ResponsesInputErrorParam(const ResponsesInputPlan& plan,
                              std::string* param) {
  constexpr std::string_view prefix = "messages[";
  if (!param->starts_with(prefix)) return;
  const auto end = param->find(']', prefix.size());
  if (end == param->npos) return;
  size_t index = 0;
  const auto parsed = std::from_chars(param->data() + prefix.size(),
                                      param->data() + end, index);
  if (parsed.ec != std::errc() || index >= plan.message_paths.size()) return;
  const auto& path = plan.message_paths[index];
  const auto suffix = param->substr(end + 1);
  *param = path == "instructions" || path == "input" ? path : path + suffix;
}

Status NormalizeResponsesCallableTools(const io::Json& request, io::Json* tools,
                                       io::Json* choice, ChatPlanError* error,
                                       io::Json* effective_tools,
                                       bool definitions_only) {
  *tools = Array();
  *choice = String("auto");
  *error = {};
  if (effective_tools) *effective_tools = Array();
  std::vector<ResponsesToolIdentity> identities;
  auto identity_status =
      PlanResponsesToolIdentities(request, &identities, error);
  if (!identity_status) return identity_status;
  struct Entry {
    const Json* definition;
    size_t outer;
    size_t child;
    std::string path, name_space, description;
  };
  std::vector<Entry> entries;
  bool python_enabled = false;
  bool program_enabled = false;
  std::string program_path;
  std::vector<std::string> model_paths;
  std::unordered_set<std::string> program_only;
  constexpr size_t top_level = 128;
  std::vector<ResponsesFileSearchBinding> native;
  auto binding_status =
      PlanResponsesFileSearchBindings(request, &native, error);
  if (!binding_status) return binding_status;
  std::vector<ResponsesWebBinding> web;
  binding_status = PlanResponsesWebBindings(request, &web, error);
  if (!binding_status) return binding_status;
  std::vector<ResponsesMcpBinding> mcp;
  binding_status =
      PlanResponsesMcpRequestBindings(request, &mcp, error, definitions_only);
  if (!binding_status) return binding_status;
  if (const auto* definitions = request.Find("tools")) {
    if (!definitions->IsArray() || definitions->array.size() > 128)
      return Invalid(error, "tools", "tools must be an array up to 128 items");
    if (effective_tools) *effective_tools = *definitions;
    for (size_t i = 0; i < definitions->array.size(); ++i) {
      const auto& definition = definitions->array[i];
      const auto at = "tools[" + std::to_string(i) + ']';
      if (definition.GetString("type") == "namespace") {
        const auto& children = definition.GetArray("tools")->array;
        for (size_t j = 0; j < children.size(); ++j)
          entries.push_back({&children[j], i, j,
                             at + ".tools[" + std::to_string(j) + ']',
                             definition.GetString("name"),
                             definition.GetString("description")});
      } else {
        entries.push_back({&definition, i, top_level, at, "", ""});
      }
    }
    for (const auto& entry : entries) {
      const auto& definition = *entry.definition;
      const auto& at = entry.path;
      if (!definition.IsObject())
        return Invalid(error, at, "tool definition must be an object");
      const auto* kind = definition.Find("type");
      if (!kind || !kind->IsString() || kind->str.empty())
        return Invalid(error, at + ".type",
                       "tool type must be a nonempty string");
      const auto type = definition.GetString("type");
      if (type == "mcp") {
        ResponsesMcpDefinition checked;
        auto status =
            PlanResponsesMcpDefinition(definition, &checked, error, at);
        if (!status) return status;
        if (effective_tools)
          effective_tools->array[entry.outer] = checked.declaration;
        for (const auto& binding : mcp) {
          if (binding.server_label != definition.GetString("server_label"))
            continue;
          auto body = Object();
          for (const auto* key : {"description", "parameters", "strict"})
            Set(&body, key, *binding.function.Find(key));
          Set(&body, "name", String(binding.model_name));
          tools->array.push_back(Object(
              {{"type", String("function")}, {"function", std::move(body)}}));
          model_paths.push_back(at);
          if (!binding.direct) program_only.insert(binding.model_name);
        }
        continue;
      }
      if (type == "code_interpreter") {
        if (python_enabled)
          return Invalid(error, at, "Duplicate code interpreter");
        auto valid = ValidatePythonTool(definition, error, at);
        if (!valid) return valid;
        python_enabled = true;
        tools->array.push_back(PythonToolFunction());
        if (!ResponsesToolAllows(definition, "direct"))
          program_only.insert(kPythonToolName);
        model_paths.push_back(at);
        continue;
      }
      if (type == "apply_patch") {
        auto body = ResponsesPatchFunction();
        tools->array.push_back(Object(
            {{"type", String("function")}, {"function", std::move(body)}}));
        model_paths.push_back(at);
        if (!ResponsesToolAllows(definition, "direct"))
          program_only.insert(kApplyPatchToolName);
        continue;
      }
      if (type == "local_shell") {
        const auto text = Object({{"type", String("string")}});
        auto properties =
            Object({{"command", Object({{"type", String("array")},
                                        {"minItems", Number(1)},
                                        {"items", text}})},
                    {"env", Object({{"type", String("object")},
                                    {"additionalProperties", text}})},
                    {"timeout_ms", Object({{"type", String("integer")}})},
                    {"user", text},
                    {"working_directory", text}});
        auto parameters = Object({{"type", String("object")},
                                  {"properties", std::move(properties)},
                                  {"required", Array({String("command")})},
                                  {"additionalProperties", Boolean(false)}});
        auto function = Object(
            {{"name", String(kLocalShellToolName)},
             {"description",
              String("Request a command in the client's local environment. "
                     "Pass its argv as command and wait for the client's "
                     "output.")},
             {"parameters", std::move(parameters)},
             {"strict", Boolean(false)}});
        tools->array.push_back(Object(
            {{"type", String("function")}, {"function", std::move(function)}}));
        model_paths.push_back(at);
        continue;
      }
      if (type == "programmatic_tool_calling") {
        auto status = InputFields(definition, {"type"}, &error->param, at);
        if (!status) return status;
        if (program_enabled)
          return Invalid(error, at, "Duplicate program tool");
        program_enabled = true;
        program_path = at;
        continue;
      }
      if (IsWebSearchType(type)) {
        const auto found = std::find_if(web.begin(), web.end(), [&](const auto& binding) {
          return binding.tool_index == entry.outer;
        });
        if (found == web.end()) return Invalid(error, at, "Missing native web binding");
        tools->array.push_back(NativeWebSearchDefinition(found->model_name));
        model_paths.push_back(at);
        continue;
      }
      if (type == "file_search") {
        const auto found = std::find_if(
            native.begin(), native.end(), [&](const auto& binding) {
              return binding.tool_index == entry.outer;
            });
        if (found == native.end())
          return Invalid(error, at, "Missing native tool binding");
        tools->array.push_back(NativeFileSearchDefinition(found->model_name));
        model_paths.push_back(at);
        continue;
      }
      if (type == "tool_search") {
        ResponsesToolCatalog catalog;
        catalog.search = definition;
        if (!catalog.search.Has("execution"))
          Set(&catalog.search, "execution", String("server"));
        catalog.summaries = Array();
        Json native_search;
        auto status =
            NativeResponsesToolSearchDefinition(catalog, &native_search, error);
        if (!status) return status;
        tools->array.push_back(std::move(native_search));
        model_paths.push_back(at);
        continue;
      }
      if (type != "function" && type != "custom") return Unresolved(error, at);
      auto status =
          type == "function"
              ? InputFields(definition,
                            {"type", "name", "description", "parameters",
                             "strict", "allowed_callers", "async",
                             "defer_loading", "output_schema"},
                            &error->param, at)
              : InputFields(definition,
                            {"type", "name", "description", "format",
                             "allowed_callers", "async", "defer_loading"},
                            &error->param, at);
      if (!status) return status;
      if (const auto* description = definition.Find("description");
          description && !description->IsNull() && !description->IsString())
        return Invalid(error, at + ".description",
                       "tool description must be text or null");
      for (const auto* flag : {"async", "defer_loading"}) {
        const auto* value = definition.Find(flag);
        if (value && !value->IsBool())
          return Invalid(error, at + '.' + flag, "tool flag must be boolean");
      }
      if (const auto* schema = definition.Find("output_schema");
          schema && !schema->IsNull()) {
        if (!schema->IsObject())
          return Invalid(error, at + ".output_schema",
                         "output_schema requires an object");
        OutputSchemaGraph graph;
        status =
            graph.Init(*schema, false, &error->param, at + ".output_schema",
                       OutputSchemaPurpose::kToolResult);
        if (!status) {
          error->code =
              status.message().starts_with("schema_resource_exhausted:")
                  ? "tool_result_validation_capacity"
                  : "invalid_tool_output_schema";
          error->http_status =
              error->code == "tool_result_validation_capacity" ? 503 : 400;
          return status;
        }
      }
      status = ValidateResponsesAllowedCallers(definition, error, at);
      if (!status) return status;
      Json body = Object();
      for (const char* key :
           {"name", "description", "parameters", "strict", "format"})
        if (const auto* value = definition.Find(key); value && !value->IsNull())
          Set(&body, key, *value);
      if (type == "custom") {
        if (const auto* format = body.Find("format")) {
          const bool grammar = format->GetString("type") == "grammar";
          status = grammar
                       ? InputFields(*format, {"type", "syntax", "definition"},
                                     &error->param, at + ".format")
                       : InputFields(*format, {"type"}, &error->param,
                                     at + ".format");
          if (!status) return status;
          if (grammar) {
            // Responses uses a flat format; Chat's internal custom format
            // nests the same syntax/definition under grammar.
            auto converted = Object();
            for (const auto* key : {"syntax", "definition"})
              if (const auto* value = format->Find(key))
                Set(&converted, key, *value);
            Set(&body, "format",
                Object({{"type", String("grammar")},
                        {"grammar", std::move(converted)}}));
          }
        }
      }
      if (type == "function") {
        if (!body.Has("parameters"))
          Set(&body, "parameters",
              Object({{"type", String("object")},
                      {"properties", Object()},
                      {"additionalProperties", Boolean(false)}}));
        if (!body.Has("strict")) {
          auto normalized = *body.Find("parameters");
          bool strict = StrictDefaults(&normalized);
          if (strict) {
            status = ValidateOutputSchema(normalized, true);
            if (!status &&
                status.message().starts_with("schema_resource_exhausted:")) {
              *error = {"constraint_resource_exhausted", at + ".parameters",
                        503};
              return status;
            }
            strict = status.ok();
          }
          Set(&body, "strict", Boolean(strict));
          if (strict) Set(&body, "parameters", std::move(normalized));
        }
        if (body.GetBool("strict")) {
          status = ValidateOutputSchema(*body.Find("parameters"), true, false,
                                        &error->param, at + ".parameters");
          if (!status) return status;
        }
      }
      if (effective_tools) {
        Json effective = definition;
        for (const char* key : {"parameters", "strict"})
          if (const auto* value = body.Find(key)) Set(&effective, key, *value);
        auto* target = &effective_tools->array[entry.outer];
        if (entry.child != top_level)
          target = &Field(*target, "tools").array[entry.child];
        *target = std::move(effective);
      }
      const auto model_name = ResponsesToolModelName(
          entry.name_space, definition.GetString("name"), type);
      if (!ResponsesToolAllows(definition, "direct"))
        program_only.insert(model_name);
      Set(&body, "name", String(model_name));
      if (model_name != definition.GetString("name")) {
        const auto label = Object({{"namespace", String(entry.name_space)},
                                   {"name", *definition.Find("name")}});
        Set(&body, "description",
            String("Tool identity: " + OutputJson(label) + "\n" +
                   entry.description + "\n" + body.GetString("description")));
      }
      tools->array.push_back(
          Object({{"type", String(type)}, {type, std::move(body)}}));
      model_paths.push_back(at);
    }
  }
  if (program_enabled) {
    tools->array.push_back(NativeResponsesProgramDefinition(
        effective_tools ? *effective_tools : *request.Find("tools"),
        request.GetBool("parallel_tool_calls", true), &mcp));
    model_paths.push_back(program_path);
  }
  if (const auto* original = request.Find("tool_choice")) {
    Json selected, native_choice, mcp_choice, web_choice, mapped;
    // Client selectors are mapped before builtin expansion. A builtin's
    // private model name must never be reinterpreted as a child's bare name.
    auto status = MapNamespaceChoice(*original, identities, &selected, error);
    if (status)
      status = MapResponsesProgramChoice(selected, program_enabled,
                                         &native_choice, error, python_enabled);
    if (status)
      status = MapResponsesMcpChoice(
          native_choice, mcp, &mcp_choice, error,
          definitions_only ? request.GetArray("tools") : nullptr);
    if (status)
      status = MapResponsesWebChoice(mcp_choice, web, &web_choice, error);
    if (status)
      status = MapResponsesFileSearchChoice(web_choice, native, &mapped, error);
    if (status)
      status = ToolSelection(mapped, choice, "tool_choice", {}, error);
    if (!status) return status;
  }
  if (tools->array.size() > 128) {
    *error = {"tool_resource_exhausted", "tools", 503};
    return Status::Fail("Expanded callable tools exceed request capacity");
  }
  auto status =
      ValidateToolDefinitions(Object({{"tools", *tools}}), &error->param);
  if (!status) {
    const auto end = error->param.find(']');
    size_t index = 0;
    if (error->param.starts_with("tools[") && end != std::string::npos) {
      const auto parsed = std::from_chars(error->param.data() + 6,
                                          error->param.data() + end, index);
      if (parsed.ec == std::errc() && index < model_paths.size()) {
        auto suffix = error->param.substr(end + 1);
        const bool custom = suffix.starts_with(".custom");
        for (const auto* part : {".function", ".custom"})
          if (suffix.starts_with(part)) {
            suffix.erase(0, std::char_traits<char>::length(part));
            break;
          }
        if (custom && suffix.starts_with(".format.grammar"))
          suffix.erase(std::string_view(".format").size(),
                       std::string_view(".grammar").size());
        error->param = model_paths[index] + suffix;
      }
    }
  }
  if (!status) return status;
  const auto prohibited = [&](const Json& tool) {
    const auto* value = tool.Find(tool.GetString("type"));
    return value && program_only.contains(value->GetString("name"));
  };
  if (prohibited(*choice))
    return Invalid(error, "tool_choice",
                   "Selected tool requires a program caller");
  if (const auto* selected = choice->Find("allowed_tools"))
    if (const auto* items = selected->GetArray("tools"))
      for (const auto& item : items->array)
        if (prohibited(item))
          return Invalid(error, "tool_choice.tools",
                         "Selected tool requires a program caller");
  // Validate every declaration before removing the direct route. Program-only
  // schemas and custom formats must not escape validation by being hidden.
  std::erase_if(tools->array, prohibited);
  return {};
}

Status NormalizeResponsesTools(const io::Json& request, io::Json* tools,
                               io::Json* choice, ChatPlanError* error,
                               io::Json* effective_tools,
                               io::Json* header_tools, io::Json* active_tools,
                               bool definitions_only) {
  if (header_tools) *header_tools = {};
  const auto* definitions = request.Find("tools");
  const auto* input = request.GetArray("input");
  const auto empty = Array();
  if (!HasResponsesToolDiscovery(definitions ? *definitions : empty,
                                 input ? *input : empty)) {
    Json effective;
    auto status = NormalizeResponsesCallableTools(request, tools, choice, error,
                                                  &effective, definitions_only);
    if (effective_tools) *effective_tools = effective;
    if (active_tools) *active_tools = std::move(effective);
    return status;
  }
  ResponsesToolHistory history;
  auto status =
      ResolveResponsesToolHistory(definitions ? *definitions : empty,
                                  input ? *input : empty, &history, error);
  if (!status) return status;
  auto current = request;
  Set(&current, "tools", history.active);
  status = NormalizeActiveResponsesTools(current, history.initial, tools,
                                         choice, error, definitions_only);
  if (!status) return status;
  if (header_tools) {
    auto initial = history.initial.eager;
    if (!history.initial.search.IsNull())
      initial.array.push_back(history.initial.search);
    Json ignored;
    auto initial_request = request;
    Set(&initial_request, "tools", initial);
    std::erase_if(initial_request.object,
                  [](const auto& pair) { return pair.first == "tool_choice"; });
    status = NormalizeActiveResponsesTools(initial_request, history.initial,
                                           header_tools, &ignored, error,
                                           definitions_only);
    if (!status) return status;
  }
  if (effective_tools) *effective_tools = history.initial.definitions;
  if (active_tools) *active_tools = std::move(history.active);
  return {};
}
}  // namespace q4t::server
