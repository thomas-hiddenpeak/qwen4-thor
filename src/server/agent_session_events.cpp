#include "q4t/server/agent_session.h"

#include "agent_session_internal.h"
#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace agent_detail;

ObjectReply Invalid() {
  return Error(503, "agent_session_journal_invalid",
               "Invalid session event history");
}

Json Member(const Json& value, const char* key) {
  const auto* field = value.Find(key);
  return field ? *field : Json{};
}

bool Emit(Json* events, const std::string& prefix, const char* type,
          Json data) {
  Set(&data, "type", String(type));
  Set(&data, "event_id", String("evt_" + Digest(prefix + "_" + type)));
  ChatPlanError error;
  if (!ValidateAgentSession(data, AgentSessionShape::kEvent, &error))
    return false;
  events->array.push_back(std::move(data));
  return true;
}

ObjectReply Translate(const Json& session, const Json& link, const Json& record,
                      const Json& event, AgentEventCursor& cursor, Json* events,
                      bool* end_step, bool* settled) {
  const auto type = event.GetString("type");
  const auto turn = link.GetString("id");
  const auto id = session.GetString("id");
  const auto response_id = link.GetString("response_id");
  const auto prefix =
      "evt_" + response_id + "_" + std::to_string(cursor.sequence);
  auto loc = Object({{"session_id", String(id)}, {"turn_id", String(turn)}});
  if (const auto* response = event.Find("response")) {
    if (response->GetString("id") != response_id) return Invalid();
    auto phase = Object(
        {{"response", *response},
         {"own_input", Array()},
         {"agent_session_id", String(id)},
         {"agent_turn_started_at", Member(record, "agent_turn_started_at")},
         {"agent_turn_finished_at", Member(record, "agent_turn_finished_at")}});
    const auto state = response->GetString("status");
    const bool terminal = state == "completed" || state == "incomplete" ||
                          state == "cancelled" || state == "failed";
    if (!terminal) Set(&phase, "agent_turn_finished_at", Number(0));
    if (state == "queued") Set(&phase, "agent_turn_started_at", Number(0));
    AgentSessionView snapshot;
    auto projected = ProjectStep(session, link, phase, &snapshot);
    if (!projected.ok()) return projected;
    const bool first = cursor.turn_id != turn;
    if (first) {
      cursor.turn_id = turn;
      cursor.turn_usage = {};
      cursor.created_at = snapshot.turn.GetInt("created_at");
      cursor.started_at = 0;
    }
    if (!cursor.started_at)
      cursor.started_at = phase.GetInt("agent_turn_started_at");
    Set(&snapshot.turn, "created_at", Number(cursor.created_at));
    if (cursor.started_at)
      Set(&snapshot.turn, "started_at", Number(cursor.started_at));
    auto total = cursor.session_usage, usage = cursor.turn_usage;
    if (!AddUsage(&total, *snapshot.session.Find("usage")) ||
        !AddUsage(&usage, *snapshot.turn.Find("usage")))
      return Invalid();
    Set(&snapshot.session, "usage", total);
    Set(&snapshot.turn, "usage", usage);
    Set(&loc, "turn", snapshot.turn);
    auto resource = Object({{"session", snapshot.session}});
    if (type == "response.created") {
      if (!cursor.step &&
          !Emit(events, prefix, "agent.session.created", resource))
        return Invalid();
      if (first && !Emit(events, prefix, "agent.session.turn.created", loc))
        return Invalid();
    } else if (type == "response.in_progress") {
      if (!Emit(events, prefix, "agent.session.in_progress", resource) ||
          !Emit(events, prefix, "agent.session.turn.in_progress", loc))
        return Invalid();
    } else if (terminal) {
      cursor.session_usage = std::move(total);
      cursor.turn_usage = usage;
      *end_step = true;
      // The SDK dispatches a client function on item.added. Native Responses
      // starts that item before it has all arguments and before accepting
      // results is possible. Publish the callable item at the durable handoff.
      for (size_t index = 0; index < snapshot.items.array.size(); ++index) {
        const auto& item = snapshot.items.array[index];
        if (item.GetString("type") != "function_call") continue;
        auto call = Object({{"session_id", String(id)},
                            {"turn_id", String(turn)},
                            {"output_index", Number(index)},
                            {"item", item}});
        const auto key = prefix + "_call_" + std::to_string(index);
        if (state == "completed" && item.GetString("status") == "completed" &&
            !Emit(events, key, "agent.session.turn.item.added", call))
          return Invalid();
        if (!Emit(events, key, "agent.session.turn.item.done", call))
          return Invalid();
      }
      if (snapshot.session.GetString("status") == "requires_action") {
        if (!Emit(events, prefix, "agent.session.requires_action", resource))
          return Invalid();
      } else {
        Set(&loc, "usage", std::move(usage));
        const char* turn_type =
            state == "completed"   ? "agent.session.turn.completed"
            : state == "cancelled" ? "agent.session.turn.cancelled"
                                   : "agent.session.turn.failed";
        const char* session_type =
            snapshot.session.GetString("status") == "failed"
                ? "agent.session.failed"
                : "agent.session.idle";
        if (!Emit(events, prefix, turn_type, loc) ||
            !Emit(events, prefix, session_type, resource))
          return Invalid();
        *settled = true;
      }
    }
    return {};
  }
  if (type == "response.output_item.added" ||
      type == "response.output_item.done") {
    const auto* item = event.Find("item");
    if (!item) return Invalid();
    if (item->GetString("type") == "function_call") return {};
    auto items = Array(), actions = Array();
    auto projected = ProjectItem(*item, turn, &items, &actions, false);
    if (!projected.ok()) return projected;
    Set(&loc, "item", std::move(items.array.front()));
    Set(&loc, "output_index", Member(event, "output_index"));
    if (!Emit(events, prefix,
              type.ends_with(".done") ? "agent.session.turn.item.done"
                                      : "agent.session.turn.item.added",
              loc))
      return Invalid();
    return {};
  }
  // Raw chain-of-thought, probability fields and Responses-only tool argument
  // deltas have no public Agent event equivalent. The typed tool is published
  // from its actual output item. Only SDK summary events carry reasoning text.
  auto suffix = type.starts_with("response.") ? type.substr(9) : "";
  const bool summary = suffix.starts_with("reasoning_summary_");
  if (!summary && !suffix.starts_with("content_part.") &&
      !suffix.starts_with("output_text.") && !suffix.starts_with("refusal."))
    return {};
  for (const auto* key : {"output_index", "item_id", "content_index",
                          "summary_index", "text", "delta"})
    if (event.Has(key)) Set(&loc, key, Member(event, key));
  if (suffix.starts_with("refusal.")) {
    suffix = "output_text." + suffix.substr(8);
    if (event.Has("refusal")) Set(&loc, "text", Member(event, "refusal"));
  }
  if (const auto* part = event.Find("part")) {
    const auto kind = part->GetString("type");
    if (kind != "summary_text" && kind != "output_text" && kind != "refusal")
      return {};
    Set(&loc, "part",
        Object(
            {{"type", String(kind == "refusal" ? "output_text" : kind)},
             {"text", Member(*part, kind == "refusal" ? "refusal" : "text")}}));
  }
  if (suffix == "reasoning_summary_part.done")
    Set(&loc, "status", Member(event, "status"));
  const auto target = "agent.session.turn." + suffix;
  if (!Emit(events, prefix, target.c_str(), loc)) return Invalid();
  return {};
}
}  // namespace

ObjectReply OpenAgentEventSubscription(ObjectStore& objects,
                                       const std::string& id,
                                       const StateAccess& access,
                                       AgentEventCursor* cursor) {
  auto row = agent_detail::ReadRow(objects, id, access);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* session = data ? data->Find("session") : nullptr;
  const auto* links = data ? data->GetArray("turns") : nullptr;
  if (!session || session->GetString("id") != id || !links ||
      links->array.empty())
    return Invalid();
  AgentEventCursor next;
  for (const auto& link : links->array) {
    auto record = ResponsesState(objects).ReadRecord(
        link.GetString("response_id"), access);
    if (!record.ok()) return record;
    AgentSessionView step;
    auto projected = ProjectStep(*session, link, record.value, &step);
    if (!projected.ok()) return projected;
    const auto turn = step.turn.GetString("id");
    if (next.turn_id != turn) {
      next.turn_id = turn;
      next.turn_usage = {};
      next.created_at = step.turn.GetInt("created_at");
      next.started_at = 0;
    }
    if (!next.started_at) next.started_at = step.turn.GetInt("started_at");
    const auto status = step.turn.GetString("status");
    if (status == "queued" || status == "in_progress") {
      if (&link != &links->array.back()) return Invalid();
      next.sequence = record.value.GetArray("events")->array.size();
    } else {
      const auto& usage = *step.turn.Find("usage");
      if (!AddUsage(&next.session_usage, usage) ||
          !AddUsage(&next.turn_usage, usage))
        return Invalid();
      ++next.step;
    }
  }
  *cursor = std::move(next);
  return {};
}

ObjectReply ReplayAgentSessionEvents(ObjectStore& objects,
                                     const std::string& id,
                                     const StateAccess& access,
                                     AgentEventCursor* cursor, Json* events,
                                     bool* settled) {
  *events = Array();
  *settled = false;
  auto row = agent_detail::ReadRow(objects, id, access);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* session = data ? data->Find("session") : nullptr;
  const auto* links = data ? data->GetArray("turns") : nullptr;
  if (!session || session->GetString("id") != id || !links ||
      cursor->step > links->array.size())
    return Invalid();
  if (cursor->step == links->array.size()) return {};
  const auto& link = links->array[cursor->step];
  const auto response_id = link.GetString("response_id");
  auto record = ResponsesState(objects).ReadRecord(response_id, access);
  if (!record.ok()) return record;
  if (record.value.GetString("agent_session_id") != id) return Invalid();
  const auto& journal = *record.value.GetArray("events");
  const auto& response = *record.value.Find("response");
  // A storage-capacity terminal may have consumed its reservation and retained
  // just a failed snapshot, without the original journal.
  if (journal.array.empty() && (response.GetString("status") == "failed" ||
                                response.GetString("status") == "cancelled")) {
    auto event =
        Object({{"type", String("response." + response.GetString("status"))},
                {"response", response}});
    bool end = false;
    auto result = Translate(*session, link, record.value, event, *cursor,
                            events, &end, settled);
    if (result.ok() && end) {
      ++cursor->step;
      cursor->sequence = 0;
    }
    return result;
  }
  if (cursor->sequence > journal.array.size()) return Invalid();
  size_t bytes = 0;
  for (size_t n = 0;
       n < 64 && cursor->sequence < journal.array.size() && bytes < 1024 * 1024;
       ++n) {
    const auto& event = journal.array[cursor->sequence];
    if (event.GetInt("sequence_number", -1) !=
        static_cast<int64_t>(cursor->sequence))
      return Invalid();
    const auto before = events->array.size();
    bool end = false;
    auto result = Translate(*session, link, record.value, event, *cursor,
                            events, &end, settled);
    if (!result.ok()) return result;
    for (auto i = before; i < events->array.size(); ++i)
      bytes += OutputJson(events->array[i]).size();
    ++cursor->sequence;
    if (end) {
      if (cursor->sequence != journal.array.size()) return Invalid();
      ++cursor->step;
      cursor->sequence = 0;
      break;
    }
  }
  return {};
}
}  // namespace q4t::server
