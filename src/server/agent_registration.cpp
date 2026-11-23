#include "q4t/server/agent_registration.h"

#include "q4t/server/agent_session.h"

#include <algorithm>
#include <chrono>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr std::string_view kRoot = "/v1/agents";

ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}

void Default(Json& value, std::string_view key, Json fallback) {
  if (!Nonnull(value, std::string(key))) Set(&value, key, std::move(fallback));
}

ObjectReply NormalizeTools(Json& tools) {
  for (size_t i = 0; i < tools.array.size(); ++i) {
    auto& tool = tools.array[i];
    const auto type = tool.GetString("type");
    if (type == "function") Default(tool, "defer_loading", Boolean(false));
    if (type == "programmatic_tool_calling")
      Default(tool, "enabled", Boolean(true));
    if (type == "computer_use")
      Default(tool, "include_screenshots", Boolean(false));
    if (type == "web_search") {
      Default(tool, "allowed_domains", {});
      Default(tool, "mode", String("live"));
      Default(tool, "context_size", String("medium"));
      auto& location = Field(tool, "location");
      if (location.IsObject()) {
        for (auto key : {"city", "country", "region", "timezone"})
          Default(location, key, {});
      }
    }
    if (type != "mcp") continue;
    Default(tool, "allowed_tools", {});
    Default(tool, "connection_origin", String("service"));
    Default(tool, "credential_id", {});
    Default(tool, "request_metadata", Object());
    Default(tool, "required", Boolean(false));
    auto& transport = Field(tool, "transport");
    if (transport.GetString("type") == "stdio") {
      Default(transport, "args", Array());
      Default(transport, "env_vars", Array());
      continue;
    }
    Default(transport, "headers", Object());
    const auto path = "tools[" + std::to_string(i) + "].transport";
    const auto url = transport.GetString("server_url");
    const auto scheme = url.find("://");
    const auto authority = scheme == url.npos
                               ? std::string_view{}
                               : std::string_view(url).substr(scheme + 3);
    const bool invalid_url_byte =
        std::any_of(url.begin(), url.end(),
                    [](unsigned char c) { return c <= 32 || c == 127; });
    if (invalid_url_byte ||
        !(url.starts_with("https://") || url.starts_with("http://")) ||
        authority.substr(0, authority.find_first_of("/?#")).empty() ||
        authority.substr(0, authority.find_first_of("/?#")).find('@') !=
            url.npos)
      return Invalid("A credential-free HTTP URL is required",
                     path + ".server_url");
    for (const auto& [name, val] : Field(transport, "headers").object) {
      const auto lower = http::Lower(name);
      const bool invalid_value = std::any_of(
          val.str.begin(), val.str.end(),
          [](unsigned char c) { return (c < 32 && c != '\t') || c == 127; });
      if (lower == "authorization" || lower == "proxy-authorization" ||
          lower == "cookie" || lower == "x-api-key" || name.empty() ||
          !std::all_of(name.begin(), name.end(), http::IsToken) ||
          invalid_value)
        return Invalid("Only non-secret HTTP headers may be stored",
                       path + ".headers");
    }
  }
  return {};
}

ObjectReply Resolve(Json& row, const Json& input,
                    const ChatPlanConfig& config) {
  if (const auto* tools = input.Find("tools");
      tools && tools->IsArray() && OutputJson(*tools).size() > 3 * 1024 * 1024)
    return Invalid("Agent tools exceed 3 MiB", "tools");
  for (const auto& [key, value] : input.object) {
    if (key == "experimental" && row.Has(key) && value.IsObject()) {
      for (const auto& [k, v] : value.object) Set(&Field(row, key), k, v);
    } else {
      Set(&row, key, value);
    }
  }
  const auto model = row.GetString("model");
  if (!IsServedModel(model, config.model, config.model_aliases)) {
    auto out = Error(404, "model_not_found", "Unknown agent model");
    out.value = Object({{"param", String("model")}});
    return out;
  }
  auto meta = NormalizeConversationMetadata(Field(row, "metadata"));
  if (!meta.ok()) return meta;
  Set(&row, "metadata", std::move(meta.value));
  Default(row, "instructions", {});
  Default(row, "name", {});
  Default(row, "service_tier", String("auto"));
  Default(row, "reasoning", Object());
  auto& reasoning = Field(row, "reasoning");
  Default(reasoning, "effort", String("medium"));
  Default(reasoning, "summary", {});
  Default(row, "text", Object());
  auto& text = Field(row, "text");
  Default(text, "verbosity", String("medium"));
  Default(text, "format", Object({{"type", String("text")}}));
  Default(row, "multi_agent", Object());
  auto& multi = Field(row, "multi_agent");
  Default(multi, "enabled", Boolean(false));
  if (const auto* n = multi.Find("max_concurrent_subagents");
      n && !n->IsNull() && (!IsInputInteger(*n) || n->number < 1))
    return Invalid("Positive integer required",
                   "multi_agent.max_concurrent_subagents");
  if (multi.GetBool("enabled"))
    Default(multi, "max_concurrent_subagents", Number(6));
  else
    Set(&multi, "max_concurrent_subagents", {});
  Default(row, "experimental", Object());
  Default(Field(row, "experimental"), "time_awareness", Boolean(false));
  Default(row, "compaction", Object());
  Default(Field(row, "compaction"), "async", Boolean(true));
  Default(row, "tools", Array());
  return NormalizeTools(Field(row, "tools"));
}

ObjectReply Validate(const Json& value, AgentRegistrationShape shape) {
  ChatPlanError error;
  auto check = ValidateAgentRegistration(value, shape, &error);
  if (check) return {};
  if (shape == AgentRegistrationShape::kResource)
    return Error(500, "agent_record_invalid",
                 "Stored agent configuration is invalid");
  return Invalid(check.message(), error.param);
}
}  // namespace

ObjectReply ResolveSessionAgent(ObjectStore& objects, const Json& request,
                                const ChatPlanConfig& config,
                                const StateAccess& access, Json* private_http) {
  const auto* agent_id = request.Find("agent_id");
  Json settings = Object();
  if (agent_id) {
    const auto route = std::string(kRoot) + "/" + agent_id->str;
    if (!IsAgentRegistrationTarget(route) || !ValidApplicationId(agent_id->str))
      return Error(404, "object_not_found", "Agent not found");
    auto saved =
        HandleAgentRegistration(objects, "GET", route, "", config, access);
    if (!saved.ok()) return saved;
    settings = std::move(saved.value);
  } else {
    const auto id = NewObjectId(ObjectKind::kAgent);
    if (id.empty()) return Error(503, "agent_id_unavailable", "No agent ID");
    Set(&settings, "id", String(id));
  }
  const auto* overrides = request.Find("agent");
  if (!agent_id && (!overrides || !overrides->Has("model")))
    return Invalid("An inline agent needs a model", "agent.model");
  return NormalizeSessionAgent(std::move(settings), overrides ? *overrides : Object(), config, private_http);
}

ObjectReply NormalizeSessionAgent(Json settings, const Json& overrides,
                                  const ChatPlanConfig& config, Json* private_http) {
  auto result = Resolve(settings, overrides, config);
  if (!result.ok()) return result;

  // Session resources expose the public HTTP endpoint or stdio launch shape.
  // Header values, supplied authorization and literal environment values stay
  // out of SDK responses and public history.
  for (auto& tool : Field(settings, "tools").array) {
    if (tool.GetString("type") != "mcp") continue;
    auto& transport = Field(tool, "transport");
    if (private_http && transport.GetString("type") == "http" &&
        (Nonnull(transport, "authorization") ||
         (transport.Find("headers") && !transport.Find("headers")->object.empty()))) {
      if (!private_http->IsObject()) *private_http = Object();
      Set(private_http, tool.GetString("server_label"), transport);
    }
    std::erase_if(transport.object, [](const auto& field) {
      return field.first == "authorization" || field.first == "headers" ||
             field.first == "env";
    });
  }
  std::erase_if(settings.object, [](const auto& field) {
    return field.first == "object" || field.first == "metadata" ||
           field.first == "created_at" || field.first == "updated_at";
  });
  result.value = std::move(settings);
  return result;
}

ObjectReply HandleAgentRegistration(ObjectStore& objects,
                                    std::string_view method,
                                    std::string_view target,
                                    const std::string& body,
                                    const ChatPlanConfig& config,
                                    const StateAccess& access) {
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  const bool collection = path == kRoot;
  if (!AgentRegistrationMethodAllowed(method, target)) {
    if (!IsAgentRegistrationTarget(target))
      return Error(404, "route_not_found", "Agent route not found");
    auto out = Error(405, "method_not_allowed", "Method not allowed");
    out.value = Object(
        {{"allow", String(collection ? "GET, POST" : "GET, POST, DELETE")}});
    return out;
  }
  if (method != "POST" && !body.empty())
    return Invalid("Unexpected body", "body");
  ObjectRequest request;
  request.kind = ObjectKind::kAgent;
  request.scope = access.scope;
  const auto execute = [&] {
    return objects.Execute(request, access.deadline, access.cancelled);
  };
  if (collection && method == "GET") {
    auto query = ParseApplicationQuery(
        split == target.npos ? "" : target.substr(split + 1));
    if (!query.ok()) return query;
    if (query.value.Has("include"))
      return Invalid("Unknown list option", "include");
    ItemViewOptions page;
    ChatPlanError error;
    auto valid = ParseItemViewOptions(query.value, &page, &error, true);
    if (!valid) return Invalid(valid.message(), error.param);
    request.operation = ObjectOperation::kList;
    request.limit = page.limit;
    request.after = page.after;
    request.descending = page.descending;
    auto result = execute();
    if (!result.ok()) return result;
    for (auto& record : Field(result.value, "data").array) {
      Json agent = Field(record, "data");
      auto checked = Validate(agent, AgentRegistrationShape::kResource);
      if (!checked.ok()) return checked;
      record = std::move(agent);
    }
    return result;
  }
  if (split != target.npos) return Invalid("Unexpected query", "query");
  Json input;
  if (method == "POST") {
    auto parsed = io::ParseJson(body, &input, true, kSchemaJsonLimits);
    if (!parsed) return Invalid(parsed.message(), "body");
    auto checked =
        Validate(input, collection ? AgentRegistrationShape::kCreate
                                   : AgentRegistrationShape::kUpdate);
    if (!checked.ok()) return checked;
  }
  const auto now = std::chrono::duration_cast<std::chrono::seconds>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();
  Json agent;
  if (collection) {
    request.id = NewObjectId(request.kind);
    if (request.id.empty())
      return Error(503, "agent_id_unavailable", "No agent ID");
    request.expected_revision = 0;
    agent = Object({{"id", String(request.id)},
                    {"object", String("agent")},
                    {"created_at", Number(now)}});
  } else {
    request.id = path.substr(kRoot.size() + 1);
    auto old = execute();
    if (!old.ok()) return old;
    agent = Field(old.value, "data");
    auto checked = Validate(agent, AgentRegistrationShape::kResource);
    if (!checked.ok()) return checked;
    request.expected_revision = old.value.GetInt("revision");
    if (method == "GET") {
      old.value = std::move(agent);
      return old;
    }
  }
  if (method == "DELETE") {
    request.operation = ObjectOperation::kErase;
  } else {
    auto resolved = Resolve(agent, input, config);
    if (!resolved.ok()) return resolved;
    Set(&agent, "updated_at",
        Number(std::max(now, agent.GetInt("created_at"))));
    auto checked = Validate(agent, AgentRegistrationShape::kResource);
    if (!checked.ok()) return checked;
    request.operation = ObjectOperation::kPut;
    request.data = agent;
    request.ttl_seconds = 0;
  }
  auto result = execute();
  if (result.commit_unknown)
    result.value = Object({{"id", String(request.id)}});
  if (result.ok()) {
    result.value = method == "DELETE"
                       ? Object({{"id", String(request.id)},
                                 {"object", String("agent.deleted")},
                                 {"deleted", Boolean(true)}})
                       : std::move(agent);
  }
  return result;
}
}  // namespace q4t::server
