#include "q4t/server/responses_input.h"

#include <algorithm>
#include <charconv>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/chat_tool_history.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/file_input.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_file_search.h"
#include "q4t/server/responses_item_shape.h"
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
    *choice = Object({{"type", String(type)},
                      {type, Object({{"name", *original.Find("name")}})}});
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
    status = ToolSelection(
        item, &converted,
        std::string(path) + ".tools[" + std::to_string(i) + ']', error);
    if (!status) return status;
    items.array.push_back(std::move(converted));
  }
  *choice =
      Object({{"type", String(type)},
              {"allowed_tools",
               Object({{"mode", String(mode)}, {"tools", std::move(items)}})}});
  return {};
}
}  // namespace

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

  Json assistant =
      Object({{"role", String("assistant")}, {"content", String("")}});
  Json calls = Array();
  std::vector<std::pair<Json, std::string>> results;
  std::unordered_map<std::string, bool> expected;
  std::unordered_set<std::string> returned, call_ids, item_ids;
  std::string reasoning, assistant_path;
  size_t automatic_returns = 0;
  const auto flush = [&]() -> Status {
    if (!calls.array.empty() && returned.size() != expected.size())
      return Invalid(error, assistant_path, "missing output for a tool call");
    if (!reasoning.empty() || !calls.array.empty()) {
      if (!reasoning.empty()) Set(&assistant, "reasoning", String(reasoning));
      if (!calls.array.empty()) Set(&assistant, "tool_calls", calls);
      append(assistant, assistant_path);
      for (auto& [message, path] : results)
        append(std::move(message), std::move(path));
    }
    assistant =
        Object({{"role", String("assistant")}, {"content", String("")}});
    calls = Array();
    results.clear();
    expected.clear();
    returned.clear();
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
      if (!returned.empty()) {
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
      if (returned.size() > automatic_returns)
        return Invalid(error, path,
                       "new call before previous tool results end");
      const bool custom = type == "custom_tool_call";
      status = InputFields(item,
                           {"id", "type", "call_id", "name", "arguments",
                            "input", "status", "async", "caller", "namespace"},
                           &error->param, path);
      if (!status) return status;
      for (const auto* key : {"caller", "namespace"})
        if (Nonnull(item, key)) return Unresolved(error, path + '.' + key);
      if (const auto* async = item.Find("async")) {
        if (!async->IsBool())
          return Invalid(error, path + ".async", "async must be boolean");
        if (async->boolean) return Unresolved(error, path + ".async");
      }
      const auto id = item.GetString("call_id");
      if (id.empty() || id.size() > 256 || !call_ids.insert(id).second)
        return Invalid(error, path + ".call_id",
                       "invalid or duplicate call ID");
      if (!ValidInputName(item.GetString("name")))
        return Invalid(error, path + ".name", "invalid tool name");
      status = TextField(item, custom ? "input" : "arguments", path, error);
      if (!status) return status;
      if (item.Has(custom ? "arguments" : "input"))
        return Invalid(error, path, "tool payload does not match its type");
      Json payload = Object({{"name", *item.Find("name")},
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
      for (const auto* key : {"namespace", "caller"})
        if (Nonnull(item, key)) return Unresolved(error, path + '.' + key);
      const auto id = item.GetString("call_id");
      const auto match = expected.find(id);
      if (match == expected.end() || !returned.insert(id).second ||
          match->second != (type == "custom_tool_call_output"))
        return Invalid(error, path + ".call_id",
                       "orphan, duplicate or mismatched output");
      if (const auto* name = item.Find("name"); name && !name->IsNull()) {
        const auto call = std::find_if(
            calls.array.begin(), calls.array.end(),
            [&](const Json& value) { return value.GetString("id") == id; });
        const auto* definition =
            call->Find(match->second ? "custom" : "function");
        if (!name->IsString() || name->str != definition->GetString("name"))
          return Invalid(error, path + ".name", "tool output name mismatch");
      }
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

Status NormalizeResponsesTools(const io::Json& request, io::Json* tools,
                               io::Json* choice, ChatPlanError* error,
                               io::Json* effective_tools) {
  *tools = Array();
  *choice = String("auto");
  *error = {};
  if (effective_tools) *effective_tools = Array();
  std::vector<ResponsesFileSearchBinding> native;
  auto binding_status =
      PlanResponsesFileSearchBindings(request, &native, error);
  if (!binding_status) return binding_status;
  if (const auto* definitions = request.Find("tools")) {
    if (!definitions->IsArray() || definitions->array.size() > 128)
      return Invalid(error, "tools", "tools must be an array up to 128 items");
    for (size_t i = 0; i < definitions->array.size(); ++i) {
      const auto& definition = definitions->array[i];
      const auto at = "tools[" + std::to_string(i) + ']';
      if (!definition.IsObject())
        return Invalid(error, at, "tool definition must be an object");
      const auto* kind = definition.Find("type");
      if (!kind || !kind->IsString() || kind->str.empty())
        return Invalid(error, at + ".type",
                       "tool type must be a nonempty string");
      const auto type = definition.GetString("type");
      if (type == "file_search") {
        const auto found = std::find_if(
            native.begin(), native.end(),
            [&](const auto& binding) { return binding.tool_index == i; });
        if (found == native.end())
          return Invalid(error, at, "Missing native tool binding");
        tools->array.push_back(NativeFileSearchDefinition(found->model_name));
        if (effective_tools) effective_tools->array.push_back(definition);
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
      for (const auto* flag : {"async", "defer_loading"}) {
        const auto* value = definition.Find(flag);
        if (value && !value->IsBool())
          return Invalid(error, at + '.' + flag, "tool flag must be boolean");
        if (value && value->boolean) return Unresolved(error, at + '.' + flag);
      }
      if (Nonnull(definition, "output_schema"))
        return Unresolved(error, at + ".output_schema");
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
        effective_tools->array.push_back(std::move(effective));
      }
      tools->array.push_back(
          Object({{"type", String(type)}, {type, std::move(body)}}));
    }
  }
  if (const auto* original = request.Find("tool_choice")) {
    Json mapped;
    auto status =
        MapResponsesFileSearchChoice(*original, native, &mapped, error);
    if (status) status = ToolSelection(mapped, choice, "tool_choice", error);
    if (!status) return status;
  }
  auto status =
      ValidateToolDefinitions(Object({{"tools", *tools}}), &error->param);
  if (!status) {
    const auto end = error->param.find(']');
    if (end != std::string::npos)
      for (const auto* part : {".function", ".custom"}) {
        const auto offset = end + 1;
        if (error->param.compare(offset, std::char_traits<char>::length(part),
                                 part) == 0)
          error->param.erase(offset, std::char_traits<char>::length(part));
      }
  }
  return status;
}
}  // namespace q4t::server
