#include "q4t/server/agent_session.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
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

ObjectReply NativeRequest(const AgentSessionStart& start) {
  const auto& agent = *start.session.Find("agent");
  auto tools = Array();
  for (const auto& tool : agent.GetArray("tools")->array) {
    if (tool.GetString("type") != "function")
      return Error(503, "agent_tool_executor_unavailable",
                   "Session tool executor is unavailable");
    tools.array.push_back(tool);
  }
  if (agent.Find("multi_agent")->GetBool("enabled") ||
      agent.Find("experimental")->GetBool("time_awareness"))
    return Error(503, "agent_executor_unavailable",
                 "Requested session execution mode is unavailable");
  auto result = ObjectReply{};
  result.value = Object({{"background", Boolean(true)},
                         {"store", Boolean(true)},
                         {"input", start.input},
                         {"tools", std::move(tools)}});
  for (const auto* key :
       {"model", "instructions", "text", "reasoning", "service_tier"})
    if (Nonnull(agent, key)) Set(&result.value, key, *agent.Find(key));
  return result;
}

ObjectReply ProjectItem(const Json& from, const std::string& turn_id,
                        Json* items, Json* actions, bool completed) {
  const auto type = from.GetString("type");
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
      auto entry = Object({{"type", String(kind)}});
      const auto* key = kind == "input_image" ? "image_url" : "text";
      Set(&entry, key, Copy(part, key));
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
}  // namespace

ObjectReply PrepareAgentSessionExecution(
    ObjectStore& objects, const Json& request, const ChatPlanConfig& config,
    const StateAccess& access, int64_t created, ResponsesExecution& run) {
  const auto id = NewObjectId(ObjectKind::kAgentSession);
  const auto nonce = NewObjectId(ObjectKind::kAgentSession);
  if (id.empty() || nonce.empty())
    return Error(503, "agent_id_unavailable", "No session or turn identity");
  AgentSessionStart start;
  auto result = PrepareDirectAgentSession(objects, request, config, access, id,
                                          created, &start);
  if (!result.ok()) return result;
  auto native = NativeRequest(start);
  if (!native.ok()) return native;
  result = run.Prepare(native.value, config, access, created, true);
  if (!result.ok()) return result;
  ObjectRequest mutation;
  mutation.operation = ObjectOperation::kPut;
  mutation.kind = ObjectKind::kAgentSession;
  mutation.id = id;
  mutation.scope = access.scope;
  mutation.ttl_seconds = 0;
  mutation.data = Object(
      {{"session", start.session},
       {"turns", Array({Object({{"id", String("turn_" + nonce.substr(14))},
                                {"response_id", String(run.Id())}})})}});
  result = run.BindAgentSession(std::move(mutation));
  if (result.ok()) result.value = std::move(start.session);
  return result;
}

ObjectReply ReadAgentSessionTurn(ObjectStore& objects, const std::string& id,
                                 std::string_view turn_id,
                                 const StateAccess& access,
                                 AgentSessionView* view) {
  ObjectRequest lookup;
  lookup.kind = ObjectKind::kAgentSession;
  lookup.id = id;
  lookup.scope = access.scope;
  auto saved = objects.Execute(lookup, access.deadline, access.cancelled);
  if (!saved.ok()) return saved;
  const auto* data = saved.value.Find("data");
  const auto* session = data ? data->Find("session") : nullptr;
  const auto* turns = data ? data->GetArray("turns") : nullptr;
  if (!session || session->GetString("id") != id || !turns ||
      turns->array.empty())
    return InvalidRecord();
  const Json* link = &turns->array.back();
  if (!turn_id.empty()) {
    const auto it = std::ranges::find_if(turns->array, [&](const auto& entry) {
      return entry.GetString("id") == turn_id;
    });
    if (it == turns->array.end())
      return Error(404, "object_not_found", "Turn not found");
    link = &*it;
  }
  const auto token = link->GetString("id");
  auto record = ResponsesState(objects).ReadRecord(
      link->GetString("response_id"), access);
  if (!record.ok()) return record;
  if (record.value.GetString("agent_session_id") != id ||
      !record.value.GetArray("own_input"))
    return InvalidRecord();
  const auto& response = *record.value.Find("response");
  const auto status = response.GetString("status");
  AgentSessionView result{*session, {}, Array()};
  auto actions = Array();
  for (const auto* source :
       {record.value.GetArray("own_input"), response.GetArray("output")}) {
    for (const auto& entry : source->array) {
      auto projected = ProjectItem(entry, token, &result.items, &actions,
                                   status == "completed");
      if (!projected.ok()) return projected;
    }
  }
  const bool waiting = !actions.array.empty();
  const auto finished = record.value.GetInt("agent_turn_finished_at");
  const auto started = record.value.GetInt("agent_turn_started_at");
  const auto created = response.GetInt("created_at");
  const bool failed = status == "failed" || status == "incomplete";
  Json error;
  if (failed) {
    const auto* cause = response.Find("error");
    const auto text = cause ? cause->GetString("message") : "";
    const auto* details = response.Find("incomplete_details");
    error = Object(
        {{"code", String("server_error")},
         {"message", String(!text.empty() ? text
                            : details     ? details->GetString(
                                            "reason", "Incomplete model output")
                                      : "Model execution failed")}});
  }
  const auto usage = Usage(response);
  const auto* agent = session->Find("agent");
  if (!agent) return InvalidRecord();
  result.turn = Object(
      {{"id", String(token)},
       {"session_id", String(id)},
       {"agent_id", Copy(*agent, "id")},
       {"subagent_id", {}},
       {"object", String("agent.session.turn")},
       {"status", String(waiting  ? "waiting"
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
             : failed                                        ? "failed"
             : status == "queued" || status == "in_progress" ? "in_progress"
                                                             : "idle"));
  Set(&result.session, "error", Copy(error, "message"));
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
}  // namespace q4t::server
