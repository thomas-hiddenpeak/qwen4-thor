#include "q4t/server/live_responses_settings.h"

#include <algorithm>
#include <unordered_set>

#include "q4t/server/input_contract.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/responses_file_search.h"
#include "q4t/server/responses_web_search.h"
#include "q4t/server/responses_mcp.h"
#include "q4t/server/responses_plan.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Json* Member(Json* value, const char* field) {
  return value && value->Has(field) ? &Field(*value, field) : nullptr;
}
bool OneOf(std::string_view value,
           std::initializer_list<std::string_view> values) {
  return std::ranges::find(values, value) != values.end();
}
Json* Settings(Json* session) {
  auto* owner = Member(session, "delegation");
  if (!owner || owner->GetString("type") != "responses") return nullptr;
  return Member(owner, "responses");
}
bool ExtendedTool(std::string_view type) {
  return OneOf(type, {"mcp", "tool_search", "file_search", "code_interpreter",
                      "shell", "local_shell", "apply_patch", "custom",
                      "computer", "computer_use_preview", "image_generation",
                      "web_search", "namespace", "programmatic_tool_calling"});
}
Status Invalid(ChatPlanError* error, std::string path, std::string message) {
  if (error) *error = {"invalid_request", std::move(path), 400};
  return Status::Fail(std::move(message));
}
bool Strip(Json* value, std::string_view name) {
  return value && std::erase_if(value->object, [&](const auto& field) {
                    return field.first == name;
                  }) != 0;
}
// ValidateLiveShape has already bounded depth, nodes and storage before the
// compatibility projection inspects a tool or format.
bool UniqueKeys(const Json& value) {
  std::unordered_set<std::string_view> seen;
  for (const auto& [name, child] : value.object)
    if (!seen.insert(name).second || !UniqueKeys(child)) return false;
  return std::ranges::all_of(value.array, UniqueKeys);
}
bool RedactTools(Json* tools) {
  if (!tools || !tools->IsArray()) return false;
  bool redacted = false;
  for (auto& tool : tools->array) {
    if (tool.GetString("type") == "mcp") {
      redacted |= Strip(&tool, "authorization");
      redacted |= Strip(&tool, "headers");
    } else if (tool.GetString("type") == "shell") {
      redacted |= Strip(Member(Member(&tool, "environment"), "network_policy"),
                        "domain_secrets");
    } else if (tool.GetString("type") == "namespace") {
      redacted |= RedactTools(Member(&tool, "tools"));
    }
  }
  return redacted;
}
}  // namespace
Status ValidateLiveWireShape(const Json& value, LiveShape shape,
                             ChatPlanError* error, std::string_view path) {
  auto status = ValidateLiveShape(value, shape, error, path);
  if (status || !error || error->http_status != 400) return status;
  const bool event =
      shape == LiveShape::kClientEvent || shape == LiveShape::kServerEvent ||
      shape == LiveShape::kSidebandClient ||
      shape == LiveShape::kSidebandServer || shape == LiveShape::kForkClient ||
      shape == LiveShape::kForkServer;
  if (event && !OneOf(value.GetString("type"),
                      {"session.start", "session.update", "session.started",
                       "session.updated", "session.closed"}))
    return status;
  if (!event && shape != LiveShape::kMediaCreate &&
      shape != LiveShape::kSessionFork && shape != LiveShape::kSessionStart &&
      shape != LiveShape::kSessionUpdate && shape != LiveShape::kSession)
    return status;
  auto projection = value;
  auto* settings = Settings(event || shape == LiveShape::kMediaCreate ||
                                    shape == LiveShape::kSessionFork
                                ? Member(&projection, "session")
                                : &projection);
  if (!settings) return status;
  bool changed = false;
  if (auto* tools = Member(settings, "tools"); tools && tools->IsArray()) {
    for (auto& tool : tools->array) {
      if (!ExtendedTool(tool.GetString("type"))) continue;
      if (!UniqueKeys(tool)) return status;
      // Keep array indexes stable and validate function declarations in place.
      tool = Object({{"type", String("web_search")}});
      changed = true;
    }
  }
  if (auto* text = Member(settings, "text"); text && text->Has("format")) {
    if (!UniqueKeys(*text)) return status;
    Strip(text, "format");
    changed = true;
  }
  if (!changed) return status;
  return ValidateLiveShape(projection, shape, error, path);
}
bool RedactLiveResponseCredentials(Json* session) {
  return RedactTools(Member(Settings(session), "tools"));
}
Status MakeLiveResponsesRequest(const Json& settings, Json input, Json* request,
                                ChatPlanError* error) {
  if (!request || !error) return Status::Fail("Missing Live Responses owner");
  *error = {};
  auto status = InputFields(
      settings,
      {"model", "instructions", "tools", "tool_choice", "max_output_tokens",
       "reasoning", "service_tier", "parallel_tool_calls", "text"},
      &error->param, "delegation.responses");
  if (!status) return status;
  if (settings.GetString("model").empty() || !input.IsArray())
    return Invalid(error, "model",
                   "A backend model and input array are required");
  if (const auto* cap = settings.Find("max_output_tokens");
      cap && !cap->IsNull() && (!IsInputInteger(*cap) || cap->number < 16))
    return Invalid(error, "max_output_tokens",
                   "Live requires at least 16 output tokens");
  if (const auto* tools = settings.GetArray("tools")) {
    for (size_t i = 0; i < tools->array.size(); ++i) {
      const auto& tool = tools->array[i];
      const auto* env = tool.Find("environment");
      if (tool.GetString("type") != "shell" || !env) continue;
      if (const auto* skills = env->GetArray("skills")) {
        for (size_t j = 0; j < skills->array.size(); ++j) {
          const auto& skill = skills->array[j];
          const auto* version = skill.Find("version");
          if (skill.GetString("type") == "skill_reference" && version &&
              !version->IsString())
            return Invalid(error,
                           "tools[" + std::to_string(i) +
                               "].environment.skills[" + std::to_string(j) +
                               "].version",
                           "Skill version must be a string");
        }
      }
      const auto* policy = env->Find("network_policy");
      if (policy && policy->Has("domain_secrets"))
        return Invalid(error,
                       "tools[" + std::to_string(i) +
                           "].environment.network_policy.domain_secrets",
                       "Live shell tools do not accept domain secrets");
    }
  }
  auto next = settings;
  // Live update null resets optional settings to the backend's defaults.
  std::erase_if(next.object,
                [](const auto& field) { return field.second.IsNull(); });
  Set(&next, "input", std::move(input));
  Set(&next, "store", Boolean(false));
  Set(&next, "stream", Boolean(true));
  Set(&next, "truncation", String("auto"));
  *request = std::move(next);
  return {};
}
Status CheckLiveResponsesSettings(
    const Json& settings, const ChatPlanConfig& config, std::string_view scope,
    ChatPlanError* error,
    const std::function<ObjectReply(const std::string&)>& check_store) {
  Json request;
  auto status = MakeLiveResponsesRequest(settings, Array(), &request, error);
  ResponsesEnvelope envelope;
  if (status)
    status = ParseResponsesEnvelope(request, config, &envelope, error);
  if (!status) return status;
  std::vector<ResponsesMcpDefinition> credentials;
  status = CaptureResponsesMcpCredentials(&request, &credentials, error);
  if (!status) {
    // A malformed header can produce an error path containing its private key.
    if (const auto pos = error->param.find(".headers");
        pos != std::string::npos)
      error->param.resize(pos + sizeof(".headers") - 1);
    return status;
  }
  Json tools, choice;
  status = NormalizeResponsesTools(request, &tools, &choice, error, nullptr,
                                   nullptr, nullptr, true);
  if (!status) return status;
  const auto* declarations = request.GetArray("tools");
  if (!declarations) return {};
  std::unordered_set<std::string> checked_stores;
  for (size_t i = 0; i < declarations->array.size(); ++i) {
    const auto& tool = declarations->array[i];
    const auto type = tool.GetString("type");
    const auto path = "tools[" + std::to_string(i) + "]";
    if (type == "mcp") {
      // The credential capture above contains only declarations with secrets.
      // Route and scope apply to every connector, including public ones.
      ResponsesMcpDefinition definition;
      status = PlanResponsesMcpDefinition(tool, &definition, error, path);
      if (!status) return status;
      const auto resolved = ResolveMcpDeploymentTarget(config.mcp_policy.get(),
                                                       scope, &definition);
      if (!resolved.ok()) {
        *error = {resolved.code, path, resolved.http_status};
        return Status::Fail(resolved.message);
      }
    } else if (type == "file_search") {
      ResponsesFileSearchTool search;
      status = PlanResponsesFileSearchTool(tool, &search, error, path);
      if (!status) return status;
      for (size_t j = 0; j < search.store_ids.size(); ++j) {
        const auto& id = search.store_ids[j];
        if (!checked_stores.insert(id).second) continue;
        const auto param =
            path + ".vector_store_ids[" + std::to_string(j) + "]";
        if (!check_store) {
          *error = {"vector_store_unavailable", param, 503};
          return Status::Fail("Vector store lookup is unavailable");
        }
        const auto reply = check_store(id);
        if (!reply.ok() || reply.commit_unknown || reply.http_status != 200) {
          *error = {"vector_store_unavailable", param, reply.http_status};
          return Status::Fail("Vector store is inaccessible to this session");
        }
      }
    } else if (IsWebSearchType(type)) {
      if (!config.web_policy ||
          std::ranges::find(config.web_policy->scopes, scope) ==
              config.web_policy->scopes.end()) {
        *error = {"web_search_unavailable", path,
                  config.web_policy ? 403 : 503};
        return Status::Fail("Web search source unavailable to this session");
      }
    }
  }
  return {};
}
}  // namespace q4t::server
