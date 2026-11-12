#include "q4t/server/responses_input.h"

#include <algorithm>
#include <charconv>
#include <deque>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/chat_tool_history.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/file_input.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_file_search.h"
#include "q4t/server/responses_item_shape.h"
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
  if (const auto* type = item.Find("type"); type && !type->IsString())
    return Invalid(error, std::string(path) + ".type", "invalid item type");
  if (const auto* id = item.Find("id");
      id && (!id->IsString() || id->str.empty() || id->str.size() > 256))
    return Invalid(error, std::string(path) + ".id", "invalid item ID");
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
             Json* content, ChatPlanError* error, bool preparing_files) {
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
        type != "input_image" && type != "input_file")
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
  if (type == "function" || type == "custom") {
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
    identities->push_back(
        {type, name, space, model_name, path, tool.GetBool("async")});
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
                               bool allow_empty, bool preparing_files) {
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
    status = NormalizeActiveResponsesTools(Object({{"tools", initial}}),
                                           discovery.initial, &native, &ignored,
                                           error);
    if (!status) return status;
    remember_formats(native);
  }
  size_t activation = 0;
  std::deque<std::string> server_searches;

  Json assistant =
      Object({{"role", String("assistant")}, {"content", String("")}});
  Json calls = Array();
  std::vector<std::pair<Json, std::string>> results;
  std::unordered_map<std::string, bool> expected;
  std::unordered_map<std::string, std::pair<std::string, std::string>> names;
  std::unordered_set<std::string> returned, call_ids, item_ids, async_ids;
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
    auto status = ItemMetadata(item, path, error);
    if (!status) return status;
    if (item.Has("id") && !item_ids.insert(item.GetString("id")).second)
      return Invalid(error, path + ".id", "duplicate input item ID");
    const auto type = item.GetString("type", "message");
    if (type == "message") {
      status = flush();
      if (!status) return status;
      status = InputFields(item,
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
                     preparing_files);
      if (!status) return status;
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
            Object({{"tools", discovery.activations[activation].definitions}}),
            discovery.initial, &native, &ignored, error);
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
    } else if (type == "function_call" || type == "custom_tool_call") {
      if (results.size() > automatic_returns) {
        if (async_ids.empty())
          return Invalid(error, path,
                         "new call before previous tool results end");
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
      Json payload = Object({{"name", String(model_name)},
                             {custom ? "input" : "arguments",
                              *item.Find(custom ? "input" : "arguments")}});
      if (!custom) {
        Json arguments;
        status = io::ParseJson(item.GetString("arguments"), &arguments, true,
                               kSchemaJsonLimits);
        if (!status || !arguments.IsObject())
          return Invalid(error, path + ".arguments",
                         "expected JSON object text");
      }
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
                     preparing_files);
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
                                       io::Json* effective_tools) {
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
  constexpr size_t top_level = 128;
  std::vector<ResponsesFileSearchBinding> native;
  auto binding_status =
      PlanResponsesFileSearchBindings(request, &native, error);
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
      if (type == "file_search") {
        const auto found = std::find_if(
            native.begin(), native.end(), [&](const auto& binding) {
              return binding.tool_index == entry.outer;
            });
        if (found == native.end())
          return Invalid(error, at, "Missing native tool binding");
        tools->array.push_back(NativeFileSearchDefinition(found->model_name));
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
      if (const auto* callers = definition.Find("allowed_callers");
          callers && !callers->IsNull()) {
        if (!callers->IsArray() || callers->array.size() != 1 ||
            !callers->array[0].IsString() || callers->array[0].str != "direct")
          return Unresolved(error, at + ".allowed_callers");
      }
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
    }
  }
  if (const auto* original = request.Find("tool_choice")) {
    Json selected, mapped;
    // Client selectors are mapped before builtin expansion. A builtin's
    // private model name must never be reinterpreted as a child's bare name.
    auto status = MapNamespaceChoice(*original, identities, &selected, error);
    if (status)
      status = MapResponsesFileSearchChoice(selected, native, &mapped, error);
    if (status)
      status = ToolSelection(mapped, choice, "tool_choice", {}, error);
    if (!status) return status;
  }
  auto status =
      ValidateToolDefinitions(Object({{"tools", *tools}}), &error->param);
  if (!status) {
    const auto end = error->param.find(']');
    size_t index = 0;
    if (error->param.starts_with("tools[") && end != std::string::npos) {
      const auto parsed = std::from_chars(error->param.data() + 6,
                                          error->param.data() + end, index);
      if (parsed.ec == std::errc() && index < entries.size()) {
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
        error->param = entries[index].path + suffix;
      }
    }
  }
  return status;
}

Status NormalizeResponsesTools(const io::Json& request, io::Json* tools,
                               io::Json* choice, ChatPlanError* error,
                               io::Json* effective_tools,
                               io::Json* header_tools, io::Json* active_tools) {
  if (header_tools) *header_tools = {};
  const auto* definitions = request.Find("tools");
  const auto* input = request.GetArray("input");
  const auto empty = Array();
  if (!HasResponsesToolDiscovery(definitions ? *definitions : empty,
                                 input ? *input : empty)) {
    Json effective;
    auto status = NormalizeResponsesCallableTools(request, tools, choice, error,
                                                  &effective);
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
                                         choice, error);
  if (!status) return status;
  if (header_tools) {
    auto initial = history.initial.eager;
    if (!history.initial.search.IsNull())
      initial.array.push_back(history.initial.search);
    Json ignored;
    status = NormalizeActiveResponsesTools(Object({{"tools", initial}}),
                                           history.initial, header_tools,
                                           &ignored, error);
    if (!status) return status;
  }
  if (effective_tools) *effective_tools = history.initial.definitions;
  if (active_tools) *active_tools = std::move(history.active);
  return {};
}
}  // namespace q4t::server
