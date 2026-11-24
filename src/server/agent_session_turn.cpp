#include "q4t/server/agent_session.h"

#include <algorithm>
#include <ctime>
#include <limits>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/request_limits.h"
#include "q4t/server/input_snapshot_bindings.h"

#include "agent_session_internal.h"
#include "agent_automation_tool.h"
#include "agent_subagent_internal.h"
#include "agent_workspace_copy.h"
#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_execution.h"

namespace q4t::server {
namespace {
using namespace object_detail;

ObjectReply InvalidRecord() {
  return Error(503, "agent_session_record_invalid",
               "Stored Agent session turn is invalid");
}

Json Copy(const Json& source, const char* key) {
  const auto* value = source.Find(key);
  return value ? *value : Json{};
}

// Project durable Responses errors, including records from older processes,
// into the SessionTurnError categories published by the Agents SDK.
const char* SessionErrorCode(std::string_view code) {
  if (code == "client_rpm_exhausted" || code == "client_tpm_exhausted")
    return "rate_limit_exceeded";
  if (code == "request_deadline_exceeded" || code == "request_timeout")
    return "request_timeout";
  // The client token cap applies to a single logical request. A smaller
  // subsequent turn is eligible immediately under the same client policy.
  if (code == "client_prompt_limit_exceeded" ||
      code == "client_output_limit_exceeded")
    return "invalid_request";
  if (code == "context_length_exceeded") return "context_length_exceeded";
  if (code == "service_draining" || code == "client_capacity_exhausted")
    return "server_overloaded";
  return "server_error";
}

ObjectReply NativeRequest(const Json& session, Json input, bool tool_search) {
  const auto& agent = *session.Find("agent");
  const auto& session_tools = agent.GetArray("tools")->array;
  auto tools = Array();
  // The registration enables discovery, but the session owner can revoke its
  // last deferred function while the current call is waiting for a result.
  if (tool_search && std::ranges::any_of(session_tools, [](const Json& tool) {
        return tool.GetString("type") == "function" &&
               tool.GetBool("defer_loading");
      })) {
    tools.array.push_back(Object({{"type", String("tool_search")},
                                  {"execution", String("server")}}));
  }
  for (const auto& tool : session_tools) {
    if (tool.GetString("type") == "automations") {
      tools.array.push_back(automation_detail::SessionTool());
      continue;
    }
    if (tool.GetString("type") == "web_search") {
      const auto mode = tool.GetString("mode");
      if (mode == "disabled") continue;
      auto native = Object({{"type", String("web_search")},
                            {"external_web_access", Boolean(mode == "live")},
                            {"search_context_size", Copy(tool, "context_size")}});
      if (Nonnull(tool, "allowed_domains"))
        Set(&native, "filters",
            Object({{"allowed_domains", Copy(tool, "allowed_domains")}}));
      if (Nonnull(tool, "location")) {
        auto location = Copy(tool, "location");
        Set(&location, "type", String("approximate"));
        Set(&native, "user_location", std::move(location));
      }
      tools.array.push_back(std::move(native));
      continue;
    }
    if (tool.GetString("type") == "mcp") {
      const auto& transport = *tool.Find("transport");
      if (tool.GetString("connection_origin") != "service" ||
          transport.GetString("type") != "http")
        return Error(503, "agent_environment_executor_unavailable",
                     "MCP tool needs an execution environment");
      auto native = Object({{"type", String("mcp")},
                            {"server_label", Copy(tool, "server_label")},
                            {"server_url", Copy(transport, "server_url")},
                            {"require_approval", String("never")}});
      if (Nonnull(tool, "allowed_tools"))
        Set(&native, "allowed_tools", Copy(tool, "allowed_tools"));
      tools.array.push_back(std::move(native));
      continue;
    }
    if (tool.GetString("type") != "function")
      return Error(503, "agent_tool_executor_unavailable",
                   "Session tool executor is unavailable");
    tools.array.push_back(tool);
  }
  auto result = ObjectReply{};
  result.value = Object({{"background", Boolean(true)},
                         {"store", Boolean(true)},
                         {"input", std::move(input)},
                         {"tools", std::move(tools)}});
  if (agent.Find("multi_agent")->GetBool("enabled"))
    Set(&result.value, "multi_agent", *agent.Find("multi_agent"));
  for (const auto* key :
       {"model", "instructions", "text", "reasoning", "service_tier"})
    if (Nonnull(agent, key)) Set(&result.value, key, *agent.Find(key));
  return result;
}

}  // namespace
namespace agent_detail {
// A session may replace functions between model calls. Rebind only the
// model's view of previously loaded functions. The stored Responses records
// and the output contract of a pending function call keep their originals.
ObjectReply ReconcileSessionDiscovery(const Json& tools, Json* history) {
  bool search = false;
  std::unordered_map<std::string, const Json*> deferred;
  for (const auto& tool : tools.array) {
    const auto type = tool.GetString("type");
    if (type == "tool_search") search = true;
    if (type == "function" && tool.GetBool("defer_loading"))
      deferred.emplace(tool.GetString("name"), &tool);
  }
  if (!search || deferred.empty()) {
    std::erase_if(history->array, [](const Json& item) {
      const auto type = item.GetString("type");
      return type == "tool_search_call" || type == "tool_search_output";
    });
    return {};
  }
  // Each current definition need only be activated once in this projection.
  // This bounds replacement schemas by the size of the current catalog even
  // if the same small historical definition was discovered many times.
  for (auto& item : history->array) {
    if (item.GetString("type") != "tool_search_output") continue;
    auto selected = Array();
    const auto* prior = item.GetArray("tools");
    if (!prior) return InvalidRecord();
    for (const auto& old : prior->array) {
      if (old.GetString("type") != "function") continue;
      const auto found = deferred.find(old.GetString("name"));
      if (found == deferred.end()) continue;
      if (item.GetString("status", "completed") == "completed") {
        selected.array.push_back(*found->second);
        deferred.erase(found);
      }
    }
    Set(&item, "tools", std::move(selected));
  }
  return {};
}

bool IsAgentDiscoveryItem(std::string_view type) {
  return type == "mcp_list_tools" || type == "tool_search_call" ||
         type == "tool_search_output";
}
ObjectReply ProjectItem(const Json& from, const std::string& turn_id,
                        Json* items, Json* actions, bool completed) {
  const auto type = from.GetString("type");
  // Server-side discovery has no public AgentSessionItem. Its durable
  // Responses items remain available to the next model step.
  if (IsAgentDiscoveryItem(type)) return {};
  Json item = Object({{"type", String(type)},
                      {"turn_id", String(turn_id)},
                      {"id", Copy(from, "id")},
                      {"status", Copy(from, "status")}});
  if (type == "message") {
    Set(&item, "phase", Copy(from, "phase"));
    Set(&item, "role", Copy(from, "role"));
    if (from.GetString("role") == "user")
      Set(&item, "status", String("completed"));
    auto content = Array();
    const auto* parts = from.GetArray("content");
    if (!parts) return InvalidRecord();
    for (const auto& part : parts->array) {
      const auto kind = part.GetString("type");
      const bool refusal = kind == "refusal";
      auto entry = Object({{"type", String(refusal ? "output_text" : kind)}});
      const auto* key = kind == "input_image" ? "image_url" : "text";
      Set(&entry, key, Copy(part, refusal ? "refusal" : key));
      content.array.push_back(std::move(entry));
    }
    Set(&item, "content", std::move(content));
  } else if (type == "function_call") {
    Json arguments;
    // A durable checkpoint may contain only the prefix of a JSON argument.
    // Expose a typed action only after the entire native call has completed.
    const auto raw = from.GetString("arguments");
    if (auto parsed = io::ParseJson(raw, &arguments, true, kSchemaJsonLimits);
        !parsed && completed && from.GetString("status") == "completed")
      return InvalidRecord();
    Set(&item, "arguments", std::move(arguments));
    for (const auto* key : {"name", "call_id"})
      Set(&item, key, Copy(from, key));
    if (completed && from.GetString("status") == "completed") {
      auto action = item;
      std::erase_if(action.object, [](const auto& field) {
        return field.first == "id" || field.first == "status";
      });
      actions->array.push_back(std::move(action));
    }
  } else if (type == "function_call_output") {
    Set(&item, "call_id", Copy(from, "call_id"));
    Set(&item, "error", Copy(from, "error"));
    Set(&item, "output", Copy(from, "output"));
    Set(&item, "status",
        String(Nonnull(from, "error") ? "failed" : "completed"));
  } else if (type == "web_search_call") {
    const auto status = from.GetString("status");
    if (status == "searching") Set(&item, "status", String("in_progress"));
    if (status == "failed") Set(&item, "status", String("incomplete"));
    auto action = Copy(from, "action");
    // Sources stay in the native receipt. The Agent search action has only
    // query and queries, and the open/find actions retain their URL and text.
    std::erase_if(action.object, [](const auto& field) {
      return field.first == "sources";
    });
    Set(&item, "action", std::move(action));
  } else if (type == "mcp_call") {
    for (const auto* field : {"server_label", "name", "error"})
      Set(&item, field, Copy(from, field));
    const auto status = from.GetString("status");
    if (status == "calling") Set(&item, "status", String("in_progress"));
    for (const auto* field : {"arguments", "output"}) {
      auto value = Copy(from, field);
      if (value.IsString()) {
        Json parsed;
        if (io::ParseJson(value.str, &parsed, true, kSchemaJsonLimits)) {
          value = std::move(parsed);
        } else if (std::string_view(field) == "arguments") {
          if (status == "completed") return InvalidRecord();
          value = {};
        }
      }
      Set(&item, field, std::move(value));
    }
  } else if (type == "reasoning") {
    Set(&item, "summary", Copy(from, "summary"));
  } else {
    return InvalidRecord();
  }
  ChatPlanError error;
  if (!ValidateAgentSession(item, AgentSessionShape::kItem, &error))
    return InvalidRecord();
  items->array.push_back(std::move(item));
  return {};
}

Json Usage(const Json& response) {
  const auto* raw = response.Find("usage");
  if (!raw || raw->IsNull()) return {};
  auto usage = *raw;
  auto& details = Field(usage, "input_tokens_details");
  std::erase_if(details.object, [](const auto& entry) {
    return entry.first == "cache_write_tokens";
  });
  return usage;
}
}  // namespace agent_detail

using namespace agent_detail;

ObjectReply PrepareAgentSessionExecution(
    ObjectStore& objects, const Json& request, const ChatPlanConfig& config,
    const StateAccess& access, int64_t created, ResponsesExecution& run,
    std::optional<ObjectRequest> delivery) {
  const auto id = NewObjectId(ObjectKind::kAgentSession);
  const auto nonce = NewObjectId(ObjectKind::kAgentSession);
  if (id.empty() || nonce.empty())
    return Error(503, "agent_id_unavailable", "No session or turn identity");
  AgentSessionStart start;
  auto result = PrepareDirectAgentSession(objects, request, config, access, id,
                                          created, &start);
  if (!result.ok()) return result;
  ObjectRequest mutation;
  mutation.operation = ObjectOperation::kPut;
  mutation.kind = ObjectKind::kAgentSession;
  mutation.id = id;
  mutation.scope = access.scope;
  mutation.ttl_seconds = 0;
  mutation.data = Object({{"session", start.session}, {"turns", Array()}});
  if (start.tool_search) Set(&mutation.data, "tool_search", Boolean(true));
  if (!start.sealed_http.empty())
    Set(&mutation.data, "sealed_http", String(start.sealed_http));
  if (delivery) {
    if (delivery->kind != ObjectKind::kAutomationInvocation ||
        delivery->scope != access.scope || delivery->expected_revision < 1 ||
        delivery->operation != ObjectOperation::kPut)
      return Error(500, "automation_delivery_invalid", "Invalid delivery receipt");
    Set(&Field(delivery->data, "resource"), "session_id", String(id));
  }
  if (start.input.array.empty()) {
    if (!start.environment_write) return InvalidRecord();
    Set(&mutation.data, "created_idle", Boolean(true));
    std::vector<ObjectRequest> writes;
    writes.push_back(std::move(mutation));
    writes.push_back(std::move(*start.environment_write));
    if (delivery) writes.push_back(std::move(*delivery));
    result = objects.ExecuteBatch(writes, access.deadline, access.cancelled);
    if (start.workspace_copies) start.workspace_copies->Settle(result);
    if (result.commit_unknown) result.value = Object({{"id", String(id)}});
  } else {
    auto native = NativeRequest(start.session, std::move(start.input),
                                start.tool_search);
    if (!native.ok()) return native;
    run.ConfigureAgentSession(start.session, start.sealed_http);
    auto selected = config;
    selected.responses_multi_agent = native.value.Has("multi_agent");
    result = run.Prepare(native.value, selected, access, created, true);
    if (!result.ok()) return result;
    auto link = Object({{"id", String("turn_" + nonce.substr(14))},
                        {"response_id", String(run.Id())}});
    if (selected.responses_multi_agent) Set(&link, "multi_agent", Boolean(true));
    if (start.tool_search) Set(&link, "tool_search", Boolean(true));
    if (!start.sealed_http.empty())
      Set(&link, "sealed_http", String(start.sealed_http));
    Field(mutation.data, "turns").array.push_back(std::move(link));
    result = run.BindAgentSession(std::move(mutation),
                                  std::move(start.environment_write), std::move(start.workspace_copies));
    if (result.ok() && delivery)
      result = run.BindAutomationDelivery(std::move(*delivery));
  }
  if (result.ok()) result.value = std::move(start.session);
  return result;
}

namespace agent_detail {
ObjectReply ProjectStep(const Json& session, const Json& link,
                        const Json& record, AgentSessionView* view) {
  const auto id = session.GetString("id");
  const auto token = link.GetString("id");
  const auto* raw = record.Find("response");
  if (record.GetString("agent_session_id") != id ||
      !record.GetArray("own_input") || !raw || !raw->GetArray("output"))
    return InvalidRecord();
  const auto& response = *raw;
  const auto status = response.GetString("status");
  AgentSessionView result{session, {}, Array(), Array()};
  auto actions = Array();
  for (const auto* source :
       {record.GetArray("own_input"), response.GetArray("output")}) {
    for (const auto& entry : source->array) {
      if (IsPrivateAgentOutput(entry)) continue;
      if (IsAgentOperationResult(entry)) {
        auto action = ParentActionItem(entry, response, session, token, &result.items);
        if (!action.ok()) return action;
        continue;
      }
      if (source == response.GetArray("output") &&
          automation_detail::IsToolItem(entry)) {
        auto projected =
            ProjectItem(entry, token, &result.items, &actions, false);
        if (!projected.ok()) return projected;
        continue;
      }
      const Json* input = &entry;
      Json tool_result;
      if (entry.GetString("type") == "function_call_output") {
        const auto* results = link.GetArray("tool_results");
        if (!results) return InvalidRecord();
        const auto it =
            std::ranges::find_if(results->array, [&](const auto& row) {
              return row.GetString("call_id") == entry.GetString("call_id");
            });
        if (it == results->array.end()) return InvalidRecord();
        tool_result = entry;
        Set(&tool_result, "error", Copy(*it, "error"));
        Set(&tool_result, "output", Copy(*it, "output"));
        input = &tool_result;
      }
      auto projected = ProjectItem(*input, token, &result.items, &actions,
                                   status == "completed");
      if (!projected.ok()) return projected;
    }
  }
  const auto cancel_at = link.GetInt("cancel_requested_at");
  const bool abandoned = cancel_at > 0 && !actions.array.empty();
  if (abandoned) {
    actions = Array();
    if (const auto* saved = link.GetArray("saved_tool_results")) {
      for (const auto& source : saved->array) {
        auto item = SavedAgentToolResult(link.GetString("response_id"), source);
        auto projected = ProjectItem(item, token, &result.items, &actions, false);
        if (!projected.ok()) return projected;
      }
    }
  }
  const bool waiting = !actions.array.empty();
  const auto finished = std::max(record.GetInt("agent_turn_finished_at"),
                                 abandoned ? cancel_at : int64_t{0});
  const auto started = record.GetInt("agent_turn_started_at");
  const auto created = response.GetInt("created_at");
  const bool failed = status == "failed" || status == "incomplete";
  Json error;
  if (failed) {
    const auto* cause = response.Find("error");
    const auto text = cause ? cause->GetString("message") : "";
    const auto* details = response.Find("incomplete_details");
    error = Object(
        {{"code", String(SessionErrorCode(cause ? cause->GetString("code") : ""))},
         {"message", String(!text.empty() ? text
                            : details     ? details->GetString(
                                            "reason", "Incomplete model output")
                                      : "Model execution failed")}});
  }
  // A retryable failure ends this turn. The SDK requires the session to
  // return to idle before it will submit the next turn on the same history.
  const auto category = error.GetString("code");
  const bool session_failed =
      failed && category != "rate_limit_exceeded" &&
      category != "server_overloaded" && category != "request_timeout" &&
      category != "invalid_request";
  const auto usage = Usage(response);
  const auto* agent = session.Find("agent");
  if (!agent) return InvalidRecord();
  result.turn = Object(
      {{"id", String(token)},
       {"session_id", String(id)},
       {"agent_id", Copy(*agent, "id")},
       {"subagent_id", {}},
       {"object", String("agent.session.turn")},
       {"status", String(abandoned ? "cancelled" : waiting  ? "waiting"
                         : failed ? "failed"
                                  : status)},
       {"created_at", Number(created)},
       {"started_at", started ? Number(started) : Json{}},
       {"completed_at", finished && !waiting ? Number(finished) : Json{}},
       {"error", error},
       {"usage", usage}});
  Set(&result.session, "required_actions", std::move(actions));
  Set(&result.session, "status",
      String(waiting                                         ? "requires_action"
             : session_failed                                ? "failed"
             : status == "queued" || status == "in_progress" ? "in_progress"
                                                             : "idle"));
  Set(&result.session, "error", session_failed ? Copy(error, "message") : Json{});
  Set(&result.session, "usage", usage);
  Set(&result.session, "last_active_at",
      Number(std::max({created, started, finished})));
  ChatPlanError invalid;
  if (!ValidateAgentSession(result.session, AgentSessionShape::kResource,
                            &invalid) ||
      !ValidateAgentSession(result.turn, AgentSessionShape::kTurn, &invalid))
    return InvalidRecord();
  *view = std::move(result);
  return {};
}

bool AddUsage(Json* total, const Json& usage) {
  if (usage.IsNull()) return true;
  if (total->IsNull()) {
    *total = usage;
    return true;
  }
  const auto add = [](Json& target, const Json& part, const char* key) {
    const auto a = target.GetInt(key, -1), b = part.GetInt(key, -1);
    if (a < 0 || b < 0 || a > std::numeric_limits<int64_t>::max() - b)
      return false;
    Set(&target, key, Number(a + b));
    return true;
  };
  const auto* in = usage.Find("input_tokens_details");
  const auto* out = usage.Find("output_tokens_details");
  return in && out && add(*total, usage, "input_tokens") &&
         add(*total, usage, "output_tokens") &&
         add(*total, usage, "total_tokens") &&
         add(Field(*total, "input_tokens_details"), *in, "cached_tokens") &&
         add(Field(*total, "output_tokens_details"), *out, "reasoning_tokens");
}

ObjectReply ReadRow(ObjectStore& objects, const std::string& id,
                    const StateAccess& access) {
  ObjectRequest lookup;
  lookup.kind = ObjectKind::kAgentSession;
  lookup.id = id;
  lookup.scope = access.scope;
  return objects.Execute(lookup, access.deadline, access.cancelled);
}

}  // namespace agent_detail

namespace {
ObjectReply EventReceipt(const Json& receipts, const std::string& name,
                         const std::string& digest, bool* replay) {
  if (!receipts.IsObject()) return InvalidRecord();
  const auto* seen = receipts.Find(name);
  *replay = seen != nullptr;
  if (seen && (!seen->IsString() || seen->str != digest))
    return Error(409, "idempotency_key_conflict",
                 "Idempotency key was used for different events");
  return {};
}

}  // namespace

namespace agent_detail {
ObjectReply ProjectHistory(ObjectStore& objects, const Json& data,
                           const std::string& id, std::string_view turn_id,
                           const StateAccess& access, AgentSessionView* view,
                           bool all_items) {
  const auto* session = data.Find("session");
  const auto* links = data.GetArray("turns");
  if (!session || session->GetString("id") != id || !links ||
      links->array.size() >= kMaxTransactionObjects)
    return InvalidRecord();
  if (links->array.empty()) {
    if (!data.GetBool("created_idle")) return InvalidRecord();
    if (!turn_id.empty()) return Error(404, "object_not_found", "Turn not found");
    auto settings = SessionSettings(data);
    if (!settings.ok()) return settings;
    *view = {std::move(settings.value), {}, Array(), Array()};
    return {};
  }
  const std::string selected = turn_id.empty()
                                   ? links->array.back().GetString("id")
                                   : std::string(turn_id);
  AgentSessionView result{{}, {}, Array(), Array()};
  Json session_usage, turn_usage;
  std::string last_turn;
  std::unordered_set<std::string> ids;
  size_t item_bytes = 0;
  for (const auto& link : links->array) {
    auto record = ResponsesState(objects).ReadRecord(
        link.GetString("response_id"), access);
    if (!record.ok()) return record;
    AgentSessionView step;
    auto settings = SessionSettings(data, &link);
    if (!settings.ok()) return settings;
    auto projected = ProjectStep(settings.value, link, record.value, &step);
    if (!projected.ok()) return projected;
    const auto current = step.turn.GetString("id");
    if (last_turn != current) {
      if (!ids.insert(current).second) return InvalidRecord();
      result.turns.array.push_back(step.turn);
      last_turn = current;
      turn_usage = {};
    }
    auto& aggregate = result.turns.array.back();
    const auto created = Copy(aggregate, "created_at");
    const auto started = Copy(aggregate, "started_at");
    const auto usage = Copy(step.turn, "usage");
    if (!AddUsage(&session_usage, usage) || !AddUsage(&turn_usage, usage))
      return InvalidRecord();
    aggregate = std::move(step.turn);
    Set(&aggregate, "created_at", created);
    if (!started.IsNull()) Set(&aggregate, "started_at", started);
    Set(&aggregate, "usage", turn_usage);
    if (current == selected) result.turn = aggregate;
    if (current == selected || all_items) {
      for (auto& item : step.items.array) {
        const auto bytes = OutputJson(item).size();
        if (bytes > kExpandedApplicationBytes - item_bytes)
          return Error(413, "agent_session_view_limit",
                       "Session items exceed the read budget");
        item_bytes += bytes;
        result.items.array.push_back(std::move(item));
      }
    }
    result.session = std::move(step.session);
  }
  if (result.turn.IsNull())
    return Error(404, "object_not_found", "Turn not found");
  auto latest = SessionSettings(data);
  if (!latest.ok()) return latest;
  for (auto key : {"agent", "metadata", "review", "spend_control", "environment"}) {
    if (const auto* value = latest.value.Find(key))
      Set(&result.session, key, *value);
    else
      std::erase_if(result.session.object,
                    [&](const auto& field) { return field.first == key; });
  }
  Set(&result.session, "usage", std::move(session_usage));
  if (const auto* pending = data.GetArray("pending_tool_results")) {
    auto& required = Field(result.session, "required_actions").array;
    for (const auto& source : pending->array) {
      const auto removed = std::erase_if(required, [&](const auto& action) {
        return action.GetString("call_id") == source.GetString("call_id") &&
               action.GetString("turn_id") == source.GetString("turn_id");
      });
      if (removed != 1) return InvalidRecord();
    }
  }
  *view = std::move(result);
  return {};
}
}  // namespace agent_detail

ObjectReply ReadAgentSessionTurn(ObjectStore& objects, const std::string& id,
                                 std::string_view turn_id,
                                 const StateAccess& access,
                                 AgentSessionView* view, bool all_items) {
  auto saved = ReadRow(objects, id, access);
  if (!saved.ok()) return saved;
  const auto* data = saved.value.Find("data");
  return data ? ProjectHistory(objects, *data, id, turn_id, access, view,
                               all_items)
              : InvalidRecord();
}
ObjectReply ListAgentSessions(ObjectStore& objects, const Json& query,
                              const StateAccess& access) {
  if (!query.IsObject())
    return Error(400, "invalid_request", "Expected session list options");
  auto paging = Object(), filters = Object();
  std::string agent_id;
  for (const auto& [key, value] : query.object) {
    if (key == "agent_id") {
      if (!value.IsString() || !ValidId(value.str))
        return Error(400, "invalid_request", "Invalid agent filter");
      agent_id = value.str;
    } else if (key.starts_with("metadata[") && key.ends_with(']')) {
      Set(&filters, key.substr(9, key.size() - 10), value);
    } else if (key == "limit" || key == "after" || key == "order") {
      Set(&paging, key, value);
    } else {
      return Error(400, "invalid_request", "Unknown session list option");
    }
  }
  auto metadata = NormalizeConversationMetadata(filters);
  if (!metadata.ok()) return metadata;
  ChatPlanError error;
  ItemViewOptions options;
  auto valid = ParseItemViewOptions(paging, &options, &error, true);
  if (!valid) {
    auto result = Error(error.http_status, error.code, valid.message());
    result.value = Object({{"param", String(error.param)}});
    return result;
  }
  ObjectRequest lookup;
  lookup.operation = ObjectOperation::kList;
  lookup.kind = ObjectKind::kAgentSession;
  lookup.scope = access.scope;
  lookup.descending = options.descending;
  lookup.after = options.after;
  lookup.limit = std::min(8, options.limit + 1);
  auto page = Array();
  bool more = false;
  size_t page_bytes = 0;
  for (;;) {
    auto listed = objects.Execute(lookup, access.deadline, access.cancelled);
    if (!listed.ok()) return listed;
    const auto* rows = listed.value.GetArray("data");
    if (!rows) return InvalidRecord();
    for (const auto& row : rows->array) {
      const auto* data = row.Find("data");
      if (!data) return InvalidRecord();
      auto latest = SessionSettings(*data);
      if (!latest.ok()) return latest;
      const auto* session = &latest.value;
      const auto* agent = session->Find("agent");
      const auto* meta = session->Find("metadata");
      if (!agent || !meta || !meta->IsObject()) return InvalidRecord();
      if ((!agent_id.empty() && agent->GetString("id") != agent_id) ||
          !std::ranges::all_of(metadata.value.object, [&](const auto& pair) {
            const auto* match = meta->Find(pair.first);
            return match && match->IsString() && match->str == pair.second.str;
          }))
        continue;
      if (page.array.size() == static_cast<size_t>(options.limit)) {
        more = true;
        break;
      }
      AgentSessionView view;
      auto result = ProjectHistory(objects, *data, session->GetString("id"), "",
                                   access, &view, false);
      if (!result.ok()) return result;
      const auto bytes = OutputJson(view.session).size();
      if (bytes > kChatBodyBytes - page_bytes) {
        if (page.array.empty())
          return Error(413, "agent_session_view_limit",
                       "Session exceeds the list budget");
        more = true;
        break;
      }
      page_bytes += bytes;
      page.array.push_back(std::move(view.session));
    }
    if (more || !listed.value.GetBool("has_more")) break;
    const auto after = listed.value.GetString("last_id");
    if (after.empty() || after == lookup.after) return InvalidRecord();
    lookup.after = after;
  }
  auto edge = [&](bool first) {
    return page.array.empty()
               ? Json{}
               : Copy(first ? page.array.front() : page.array.back(), "id");
  };
  auto first = edge(true), last = edge(false);
  ObjectReply result;
  result.value = Object({{"object", String("list")},
                         {"has_more", Boolean(more)},
                         {"first_id", std::move(first)},
                         {"last_id", std::move(last)},
                         {"data", std::move(page)}});
  return result;
}

ObjectReply PrepareAgentSessionEvents(ObjectStore& objects,
                                      const std::string& id,
                                      const Json& request, std::string_view key,
                                      const ChatPlanConfig& config,
                                      const StateAccess& access,
                                      ResponsesExecution& run, bool* replay) {
  *replay = false;
  const auto* events = request.GetArray("events");
  if (!request.IsObject() || request.object.size() != 1 || !events ||
      events->array.empty() || events->array.size() > 256)
    return Error(400, "invalid_request",
                 "Expected between 1 and 256 input events");
  for (const auto& event : events->array) {
    ChatPlanError error;
    auto valid =
        ValidateAgentSession(event, AgentSessionShape::kInputEvent, &error);
    if (!valid) {
      auto reply = Error(error.http_status, error.code, valid.message());
      reply.value = Object({{"param", String(error.param)}});
      return reply;
    }
  }
  auto saved = ReadRow(objects, id, access);
  if (!saved.ok()) return saved;
  const auto revision = saved.value.GetInt("revision");
  auto data = Copy(saved.value, "data");
  if (!data.IsObject()) return InvalidRecord();
  if (!key.empty()) {
    std::string digest;
    if (!InputValueIdentity(request, &digest))
      return Error(413, "agent_event_limit",
                   "Session event payload exceeds the identity budget");
    const auto name = Digest(key);
    auto& receipts = Field(data, "event_receipts");
    if (receipts.IsNull()) receipts = Object();
    auto known = EventReceipt(receipts, name, digest, replay);
    if (!known.ok() || *replay) return known;
    Set(&receipts, name, String(digest));
  }
  AgentSessionView view;
  auto projected = ProjectHistory(objects, data, id, "", access, &view, false);
  if (!projected.ok()) return projected;
  const auto state = view.session.GetString("status");
  if (state == "in_progress")
    return Error(409, "active_turn_not_steerable",
                 "The current turn is still running");
  const bool waiting = state == "requires_action";
  auto input = Array();
  auto pending = Copy(data, "pending_tool_results");
  if (pending.IsNull()) pending = Array();
  if (!pending.IsArray()) return InvalidRecord();
  const auto* required = view.session.GetArray("required_actions");
  std::unordered_set<std::string> received;
  for (const auto& event : events->array) {
    const auto type = event.GetString("type");
    if (type == "agent.session.input.message") {
      if (waiting)
        return Error(409, "agent_action_required",
                     "Resolve the outstanding calls first");
      const auto* messages = event.GetArray("input");
      if (!messages || messages->array.empty())
        return Error(400, "invalid_request", "Expected at least one message");
      for (auto message : messages->array) {
        if (message.GetArray("content")->array.empty())
          return Error(400, "invalid_request", "Expected message content");
        Set(&message, "type", String("message"));
        input.array.push_back(std::move(message));
      }
      continue;
    }
    if (type != "agent.session.input.tool_result")
      return Error(503, "agent_event_executor_unavailable",
                   "Requested session event executor is unavailable");
    const auto call = event.GetString("call_id");
    if (!waiting || event.GetString("turn_id") != view.turn.GetString("id") ||
        !received.insert(call).second ||
        !std::ranges::any_of(required->array, [&](const auto& action) {
          return action.GetString("type") == "function_call" &&
                 action.GetString("call_id") == call;
        }))
      return Error(409, "agent_action_mismatch",
                   "Tool result does not match a pending call in this turn");
    const auto output = Copy(event, "output");
    const auto error = Copy(event, "error");
    if (event.GetBool("success") ? output.IsNull() || !error.IsNull()
                                 : !error.IsString() || error.str.empty())
      return Error(400, "invalid_request",
                   "Expected the output of a successful call or the error of a "
                   "failed call");
    pending.array.push_back(event);
  }
  ObjectRequest mutation;
  mutation.kind = ObjectKind::kAgentSession;
  mutation.operation = ObjectOperation::kPut;
  mutation.scope = access.scope;
  mutation.id = id;
  mutation.expected_revision = revision;
  mutation.ttl_seconds = 0;
  if (waiting && received.size() < required->array.size()) {
    Set(&data, "pending_tool_results", std::move(pending));
    mutation.data = std::move(data);
    auto written = objects.Execute(mutation, access.deadline, access.cancelled);
    if (written.commit_unknown) written.value = Object({{"id", String(id)}});
    return written;
  }
  if (waiting) {
    for (const auto& event : pending.array) {
      auto output = Copy(event, "output");
      if (!event.GetBool("success")) {
        output = String(OutputJson(
            Object({{"error", Copy(event, "error")}, {"output", output}})));
      }
      input.array.push_back(Object({{"type", String("function_call_output")},
                                    {"call_id", Copy(event, "call_id")},
                                    {"output", std::move(output)}}));
    }
  }
  auto& links = Field(data, "turns");
  // Deletion also commits the session and its attached environment.
  const size_t owners = view.session.Find("environment")->Has("id") ? 2 : 1;
  if (links.array.size() + owners + 1 > kMaxTransactionObjects)
    return Error(413, "agent_session_turn_limit", "Session step limit reached");
  auto execution_settings = view.session;
  if (waiting) {
    auto original = SessionSettings(data, &links.array.back());
    if (!original.ok()) return original;
    Set(&execution_settings, "review", Copy(original.value, "review"));
    auto& current_agent = Field(execution_settings, "agent");
    const auto& original_agent = *original.value.Find("agent");
    Set(&current_agent, "model", Copy(original_agent, "model"));
    Set(&current_agent, "multi_agent", Copy(original_agent, "multi_agent"));
    const auto live_tool = [](const Json& tool) {
      const auto type = tool.GetString("type");
      return type == "function" || type == "mcp";
    };
    auto tools = Copy(current_agent, "tools");
    std::erase_if(tools.array,
                  [&](const auto& tool) { return !live_tool(tool); });
    for (const auto& prior : original_agent.GetArray("tools")->array)
      if (!live_tool(prior)) tools.array.push_back(prior);
    Set(&current_agent, "tools", std::move(tools));
  }
  // Changes to built-ins take effect on the next logical turn. Older links
  // predate session tool search and therefore carry its disabled default.
  const bool tool_search =
      (waiting ? links.array.back() : data).GetBool("tool_search");
  auto native = NativeRequest(execution_settings, std::move(input), tool_search);
  if (!native.ok()) return native;
  CancelledAgentHistory previous{id, {}};
  if (!links.array.empty()) {
    const auto& last = links.array.back();
    Set(&native.value, "previous_response_id", Copy(last, "response_id"));
    previous.results = Copy(last, "saved_tool_results");
  }
  const bool cancelled = view.turn.GetString("status") == "cancelled";
  run.ConfigureAgentSession(execution_settings, data.GetString("sealed_http"));
  auto selected = config;
  selected.responses_multi_agent = native.value.Has("multi_agent");
  auto prepared = run.Prepare(native.value, selected, access, std::time(nullptr),
                              true, cancelled ? &previous : nullptr);
  if (!prepared.ok()) return prepared;
  auto turn = view.turn.GetString("id");
  if (!waiting) {
    const auto nonce = NewObjectId(ObjectKind::kAgentSession);
    if (nonce.empty())
      return Error(503, "agent_id_unavailable", "No turn identity");
    turn = "turn_" + nonce.substr(14);
  }
  auto next = Object({{"id", String(turn)}, {"response_id", String(run.Id())}});
  if (selected.responses_multi_agent) Set(&next, "multi_agent", Boolean(true));
  if (tool_search) Set(&next, "tool_search", Boolean(true));
  if (waiting) Set(&next, "tool_results", std::move(pending));
  if (data.Has("sealed_http")) Set(&next, "sealed_http", Copy(data, "sealed_http"));
  auto bound = BindSessionSettings(&data, execution_settings, &next);
  if (!bound.ok()) return bound;
  Field(data, "turns").array.push_back(std::move(next));
  std::erase_if(data.object, [](const auto& field) {
    return field.first == "pending_tool_results";
  });
  mutation.data = std::move(data);
  return run.BindAgentSession(std::move(mutation));
}
ObjectReply ReconcileAgentEventReceipt(
    ObjectStore& objects, const std::string& id, const Json& request,
    std::string_view key, const StateAccess& access, ObjectReply result) {
  if (key.empty() ||
      (!result.commit_unknown && result.code != "object_revision_conflict"))
    return result;
  auto row = ReadRow(objects, id, access);
  const auto* data = row.ok() ? row.value.Find("data") : nullptr;
  const auto* receipts = data ? data->Find("event_receipts") : nullptr;
  std::string digest;
  if (!receipts || !InputValueIdentity(request, &digest)) return result;
  bool replay = false;
  auto known = EventReceipt(*receipts, Digest(key), digest, &replay);
  return replay ? known : result;
}
}  // namespace q4t::server
