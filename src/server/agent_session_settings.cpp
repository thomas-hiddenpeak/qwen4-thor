#include "agent_session_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply InvalidSettings() {
  return Error(503, "agent_session_record_invalid",
               "Invalid stored session settings");
}
void Remove(Json* value, const std::string& name) {
  std::erase_if(value->object,
                [&](const auto& pair) { return pair.first == name; });
}
// Each revision contains only the settings that differ from the original.
// A null override clears an optional setting inherited from the original.
void Apply(Json* result, const Json& changes) {
  for (const auto& [name, value] : changes.object) {
    if (name == "agent") {
      for (const auto& [key, item] : value.object)
        Set(&Field(*result, "agent"), key, item);
    } else if ((name == "spend_control" || name == "prompt_cache_options") &&
               value.IsNull()) {
      Remove(result, name);
    } else {
      Set(result, name, value);
    }
  }
}
}  // namespace

namespace agent_detail {
void ApplyPromptCacheOptions(Json* session, const Json& request) {
  if (const auto* options = request.Find("prompt_cache_options")) {
    if (options->IsNull())
      Remove(session, "prompt_cache_options");
    else
      Set(session, "prompt_cache_options",
          Object({{"ttl", String(options->GetString("ttl", "30m"))}}));
  }
}

ObjectReply SessionSettings(const Json& data, const Json* link) {
  const auto* seed = data.Find("session");
  const auto* agent = seed ? seed->Find("agent") : nullptr;
  if (!seed || seed->GetString("object") != "agent.session" || !agent ||
      !agent->Find("reasoning") || !agent->GetArray("tools"))
    return InvalidSettings();
  ObjectReply result;
  result.value = *seed;
  const auto* selector =
      link ? link->Find("settings_version") : data.Find("settings_version");
  if (selector) {
    const auto* versions = data.Find("settings");
    const auto* changes = versions ? versions->Find(selector->str) : nullptr;
    if (!changes || !changes->IsObject()) return InvalidSettings();
    Apply(&result.value, *changes);
  }
  return result;
}

ObjectReply BindSessionSettings(Json* data, const Json& session, Json* link) {
  const auto* seed = data->Find("session");
  if (!seed || !session.Find("agent") || !seed->Find("agent"))
    return InvalidSettings();
  const auto changed = [](const Json* a, const Json* b) {
    return a && b ? OutputJson(*a) != OutputJson(*b) : a != b;
  };
  auto delta = Object(), agent = Object();
  for (const auto& [key, value] : session.Find("agent")->object) {
    if (changed(&value, seed->Find("agent")->Find(key)))
      Set(&agent, key, value);
  }
  if (!agent.object.empty()) Set(&delta, "agent", std::move(agent));
  for (const auto* key : {"metadata", "review", "spend_control", "environment",
                          "prompt_cache_options"}) {
    const auto* value = session.Find(key);
    if (changed(value, seed->Find(key)))
      Set(&delta, key, value ? *value : Json{});
  }
  std::string version;
  if (!delta.object.empty()) {
    if (!InputValueIdentity(delta, &version))
      return Error(413, "agent_settings_limit",
                   "Session settings exceed the identity budget");
    auto& versions = Field(*data, "settings");
    if (versions.IsNull()) versions = Object();
    if (!versions.IsObject()) return InvalidSettings();
    if (!versions.Has(version)) Set(&versions, version, std::move(delta));
  }
  auto* target = link ? link : data;
  if (version.empty())
    Remove(target, "settings_version");
  else
    Set(target, "settings_version", String(version));
  // Keep snapshots referenced by execution. Coalesce superseded updates made
  // between turns so metadata changes alone cannot accumulate unused history.
  if (!link && data->Has("settings")) {
    auto& versions = Field(*data, "settings");
    const auto* turns = data->GetArray("turns");
    if (!turns) return InvalidSettings();
    std::erase_if(versions.object, [&](const auto& entry) {
      return entry.first != version &&
             std::ranges::none_of(turns->array, [&](const auto& turn) {
               return turn.GetString("settings_version") == entry.first;
             });
    });
  }
  return {};
}
}  // namespace agent_detail

ObjectReply UpdateAgentSession(ObjectStore& objects, const std::string& id,
                               const Json& request,
                               const ChatPlanConfig& config,
                               const StateAccess& access) {
  ChatPlanError error;
  auto valid =
      ValidateAgentSession(request, AgentSessionShape::kUpdate, &error);
  if (!valid) {
    auto result = Error(error.http_status, error.code, valid.message());
    result.value = Object({{"param", String(error.param)}});
    return result;
  }
  auto row = agent_detail::ReadRow(objects, id, access);
  if (!row.ok()) return row;
  const auto* raw = row.value.Find("data");
  if (!raw) return InvalidSettings();
  auto data = *raw;
  auto settings = agent_detail::SessionSettings(data);
  if (!settings.ok()) return settings;
  auto& session = settings.value;
  if (const auto* overrides = request.Find("agent")) {
    auto patch = *overrides;
    if (const auto* change = overrides->Find("reasoning")) {
      auto reasoning = *session.Find("agent")->Find("reasoning");
      if (change->Has("effort"))
        Set(&reasoning, "effort", *change->Find("effort"));
      Set(&patch, "reasoning", std::move(reasoning));
    }
    Json private_http;
    bool tool_search = false;
    auto normalized = NormalizeSessionAgent(*session.Find("agent"), patch,
                                            config, &private_http, &tool_search);
    if (!normalized.ok()) return normalized;
    if (patch.Has("tools")) {
      Set(&data, "tool_search", Boolean(tool_search));
      std::string sealed;
      auto secured = agent_detail::SealAgentHttp(objects, access, id, private_http, &sealed);
      if (!secured.ok()) return secured;
      Remove(&data, "sealed_http");
      if (!sealed.empty()) Set(&data, "sealed_http", String(std::move(sealed)));
    }
    Set(&session, "agent", std::move(normalized.value));
  }
  if (const auto* meta = request.Find("metadata")) {
    auto normalized = NormalizeConversationMetadata(*meta);
    if (!normalized.ok()) return normalized;
    Set(&session, "metadata", std::move(normalized.value));
  }
  agent_detail::ApplyPromptCacheOptions(&session, request);
  if (const auto* review = request.Find("review"))
    Set(&session, "review", *review);
  if (const auto* spend = request.Find("spend_control")) {
    if (spend->IsNull() || !Nonnull(*spend, "limit")) {
      Remove(&session, "spend_control");
    } else {
      const auto* limit = spend->Find("limit");
      if (!IsInputInteger(*limit) || limit->AsInt() <= 0)
        return Error(400, "invalid_request",
                     "Expected a positive spending limit");
      Json consumed;
      if (const auto* old = session.Find("spend_control");
          old && old->Has("consumed"))
        consumed = *old->Find("consumed");
      Set(&session, "spend_control",
          Object({{"limit", *limit}, {"consumed", consumed}}));
    }
  }
  if (!ValidateAgentSession(session, AgentSessionShape::kResource, &error))
    return InvalidSettings();
  // Prepare the public result before the write. A confirmed CAS can then
  // acknowledge this revision even if another caller immediately deletes it.
  AgentSessionView view;
  auto projected =
      agent_detail::ProjectHistory(objects, data, id, "", access, &view, false);
  if (!projected.ok()) return projected;
  for (const auto* key : {"agent", "metadata", "review", "spend_control",
                          "prompt_cache_options"}) {
    if (const auto* value = session.Find(key))
      Set(&view.session, key, *value);
    else
      Remove(&view.session, key);
  }
  auto bound = agent_detail::BindSessionSettings(&data, session);
  if (!bound.ok()) return bound;
  ObjectRequest write;
  write.kind = ObjectKind::kAgentSession;
  write.operation = ObjectOperation::kPut;
  write.scope = access.scope;
  write.id = id;
  write.ttl_seconds = 0;
  write.expected_revision = row.value.GetInt("revision");
  write.data = std::move(data);
  auto updated = objects.Execute(write, access.deadline, access.cancelled);
  if (!updated.ok()) {
    if (updated.commit_unknown) updated.value = Object({{"id", String(id)}});
    return updated;
  }
  updated.value = std::move(view.session);
  return updated;
}
}  // namespace q4t::server
