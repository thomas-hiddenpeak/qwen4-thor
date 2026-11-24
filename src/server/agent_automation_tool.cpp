#include "agent_automation_tool.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;
using Kind = AutomationRouteKind;
ObjectReply Invalid(std::string name) {
  auto result = Error(400, "invalid_request", "Invalid wakeup argument");
  result.value = Object({{"param", String(std::move(name))}});
  return result;
}
bool IsAction(std::string_view value) {
  return value == "get" || value == "pause" || value == "resume" ||
         value == "delete";
}
Json Summary(const Json& row, bool instructions) {
  auto result = Object();
  for (const char* field :
       {"id", "name", "type", "status", "run_at", "expression", "timezone"})
    if (const auto* value = row.Find(field)) Set(&result, field, *value);
  if (instructions) {
    const auto* target = row.Find("target");
    if (target)
      Set(&result, "instructions", String(target->GetString("instructions")));
  }
  return result;
}
}  // namespace

Json SessionTool() {
  const auto text = Object({{"type", String("string")}});
  const auto integer = Object({{"type", String("integer")}});
  auto actions = Array();
  for (const char* op :
       {"create", "list", "get", "update", "pause", "resume", "delete"})
    actions.array.push_back(String(op));
  auto fields = Object(
      {{"action",
        Object({{"type", String("string")}, {"enum", std::move(actions)}})},
       {"automation_id", text},
       {"after", text},
       {"name", text},
       {"instructions", text},
       {"type",
        Object({{"type", String("string")},
                {"enum", Array({String("once"), String("periodic")})}})},
       {"run_at", integer},
       {"delay_seconds", integer},
       {"expression", text},
       {"timezone", text}});
  return Object(
      {{"type", String("function")},
       {"name", String(kToolName)},
       // Each operation accepts a different subset of these fields.
       // Preserve optional arguments when translating Responses to Chat.
       {"strict", Boolean(false)},
       {"description",
        String("Manage scheduled wakeups for this session. Create with name, "
               "instructions and type: once needs run_at (Unix seconds) or a "
               "positive delay_seconds; "
               "periodic needs a five-field cron expression and IANA timezone. "
               "List uses an optional "
               "after cursor. Get, update, pause, resume and delete take an "
               "automation_id.")},
       {"parameters", Object({{"type", String("object")},
                              {"properties", std::move(fields)},
                              {"required", Array({String("action")})},
                              {"additionalProperties", Boolean(false)}})}});
}

bool IsToolItem(const Json& item) {
  const auto type = item.GetString("type");
  return (type == "function_call" || type == "function_call_output") &&
         item.GetString("id").starts_with("agentauto_") &&
         item.GetString("name") == kToolName;
}

ObjectReply PlanSessionTool(ObjectStore& objects, const StateAccess& access,
                            const ChatPlanConfig& config, const Json& args,
                            int64_t now, bool dispatcher_available,
                            SessionAutomationChange* caller) {
  if (!caller || caller->write ||
      !caller->session.starts_with("agent_session_") ||
      !ValidId(caller->session) || !ValidId(caller->call_id))
    return Error(500, "automation_caller_invalid", "Invalid automation caller");
  if (!args.IsObject()) return Invalid("arguments");
  const auto op = args.GetString("action");
  const bool create = op == "create", update = op == "update";
  if (!create && !update && op != "list" && !IsAction(op))
    return Invalid("action");
  for (const auto& [key, _] : args.object) {
    if (key == "action") continue;
    if (op == "list" && key == "after") continue;
    if (!create && op != "list" && key == "automation_id") continue;
    if (create && key == "type") continue;
    if ((create || update) &&
        (key == "name" || key == "instructions" || key == "run_at" ||
         key == "delay_seconds" || key == "expression" || key == "timezone"))
      continue;
    return Invalid(key);
  }
  if (op == "list") {
    std::string query = "limit=10";
    if (const auto* cursor = args.Find("after")) {
      if (!cursor->IsString() || !cursor->str.starts_with("automation_") ||
          !ValidId(cursor->str))
        return Invalid("after");
      query += "&after=" + cursor->str;
    }
    auto page =
        List(objects, access, {Kind::kCollection}, query, caller->session);
    if (page.ok())
      for (auto& row : Field(page.value, "data").array)
        row = Summary(row, false);
    return page;
  }
  if ((create || op == "resume") && !dispatcher_available)
    return Error(503, "automation_dispatcher_unavailable",
                 "Automation execution is unavailable");
  const auto id = args.GetString("automation_id");
  if (!create && (!id.starts_with("automation_") || !ValidId(id)))
    return Invalid("automation_id");
  auto body = Object();
  for (const char* name : {"type", "name", "run_at", "expression", "timezone"})
    if (const auto* field = args.Find(name)) Set(&body, name, *field);
  if (const auto* delay = args.Find("delay_seconds")) {
    constexpr int64_t max_epoch = (int64_t{1} << 53) - 1;
    if (args.Has("run_at") || !IsInputInteger(*delay) || delay->AsInt() <= 0 ||
        now <= 0 || delay->AsInt() > max_epoch - now)
      return Invalid("delay_seconds");
    Set(&body, "run_at", Number(now + delay->AsInt()));
  }
  if (create || args.Has("instructions")) {
    const auto* text = args.Find("instructions");
    if (!text || !text->IsString() || text->str.empty())
      return Invalid("instructions");
    Set(&body, "target",
        Object({{"type", String("existing_session")},
                {"session_id", String(caller->session)},
                {"instructions", *text}}));
  }
  const auto kind = create           ? Kind::kCollection
                    : op == "pause"  ? Kind::kPause
                    : op == "resume" ? Kind::kResume
                                     : Kind::kResource;
  const auto method = op == "delete" ? "DELETE" : op == "get" ? "GET" : "POST";
  auto result =
      Registry(objects, access, config, {kind, id}, method, body, now, caller);
  if (result.ok() && op != "delete")
    result.value = Summary(result.value, op == "get");
  return result;
}
}  // namespace q4t::server::automation_detail
