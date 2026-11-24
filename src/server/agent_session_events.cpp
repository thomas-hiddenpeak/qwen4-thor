#include "agent_automation_tool.h"
#include "q4t/server/agent_session.h"

#include "agent_session_internal.h"
#include "agent_subagent_internal.h"
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

size_t VisibleOutputs(const Json& output, size_t end) {
  size_t count = 0;
  for (size_t i = 0; i < end; ++i)
    if (!IsPrivateAgentOutput(output.array[i]) &&
        output.array[i].GetString("type") != "function_call_output")
      ++count;
  return count;
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

ObjectReply EmitChildEvents(const std::vector<Json>& rows,
                            const std::string& prefix, Json* events,
                            std::set<std::string>* delivered = nullptr) {
  for (const auto& row : rows) {
    const auto* child = row.Find("subagent");
    const auto id = child ? child->GetString("id") : row.GetString("turn_id");
    if (delivered && delivered->contains(id)) continue;
    if (!Emit(events, prefix + "_" + id, row.GetString("type").c_str(), row))
      return Invalid();
    if (delivered) delivered->insert(id);
  }
  return {};
}

ObjectReply EmitChildClaims(const SubagentStepEvents& children,
                            const std::string& response_id,
                            AgentEventCursor* cursor, Json* events) {
  const auto claims = children.claims.find(cursor->sequence);
  if (claims == children.claims.end()) return {};
  return EmitChildEvents(claims->second, response_id + "_claim", events,
                         &cursor->emitted_child_claims);
}

ObjectReply CloseWaiting(AgentSessionView snapshot, const Json& link,
                         const AgentEventCursor& cursor, Json* events) {
  if (snapshot.turn.GetString("status") != "cancelled") return Invalid();
  Set(&snapshot.turn, "created_at", Number(cursor.created_at));
  if (cursor.started_at)
    Set(&snapshot.turn, "started_at", Number(cursor.started_at));
  Set(&snapshot.turn, "usage", cursor.turn_usage);
  Set(&snapshot.session, "usage", cursor.session_usage);
  const auto key = link.GetString("response_id") + "_cancel";
  const auto loc = Object({{"session_id", Member(snapshot.session, "id")},
                           {"turn_id", Member(snapshot.turn, "id")},
                           {"turn", snapshot.turn},
                           {"usage", cursor.turn_usage}});
  if (!Emit(events, key, "agent.session.turn.cancelled", loc) ||
      !Emit(events, key, "agent.session.idle",
            Object({{"session", snapshot.session}})))
    return Invalid();
  return {};
}

ObjectReply Translate(const Json& session, const Json& link, const Json& record,
                      const Json& event, const SubagentStepEvents& children,
                      AgentEventCursor& cursor, Json* events, bool* end_step,
                      bool* settled) {
  const auto type = event.GetString("type");
  auto turn = link.GetString("id");
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
      cursor.turn_output_offset = 0;
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
      if (!cursor.step && !cursor.created_seen &&
          !Emit(events, prefix, "agent.session.created", resource))
        return Invalid();
      if (first && !Emit(events, prefix, "agent.session.turn.created", loc))
        return Invalid();
    } else if (type == "response.in_progress") {
      if (!Emit(events, prefix, "agent.session.in_progress", resource) ||
          !Emit(events, prefix, "agent.session.turn.in_progress", loc))
        return Invalid();
    } else if (terminal) {
      auto flushed = EmitChildEvents(children.terminal, prefix, events);
      if (!flushed.ok()) return flushed;
      cursor.session_usage = std::move(total);
      cursor.turn_usage = usage;
      *end_step = true;
      // The SDK dispatches a client function on item.added. Native Responses
      // starts that item before it has all arguments and before accepting
      // results is possible. Publish the callable item at the durable handoff.
      for (size_t index = 0; index < snapshot.items.array.size(); ++index) {
        const auto& item = snapshot.items.array[index];
        if (item.GetString("type") != "function_call" ||
            automation_detail::IsToolItem(item))
          continue;
        auto call = Object(
            {{"session_id", String(id)},
             {"turn_id", String(turn)},
             {"output_index", Number(cursor.turn_output_offset +
                                     VisibleOutputs(snapshot.items, index))},
             {"item", item}});
        const auto key = prefix + "_call_" + std::to_string(index);
        if (snapshot.turn.GetString("status") == "waiting" &&
            item.GetString("status") == "completed" &&
            !Emit(events, key, "agent.session.turn.item.added", call))
          return Invalid();
        if (!Emit(events, key, "agent.session.turn.item.done", call))
          return Invalid();
      }
      const auto& output = *response->GetArray("output");
      cursor.turn_output_offset += VisibleOutputs(output, output.array.size());
      if (state == "completed" &&
          snapshot.turn.GetString("status") == "cancelled") {
        *settled = true;
        return CloseWaiting(std::move(snapshot), link, cursor, events);
      }
      if (snapshot.session.GetString("status") == "requires_action") {
        cursor.waiting = true;
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
  // Agent history omits Responses-only tool discovery. Keep the item, text
  // and summary events at the same visible output index during live/replay.
  if (event.Has("output_index")) {
    const auto* response = record.Find("response");
    const auto* output = response ? response->GetArray("output") : nullptr;
    const auto raw = event.GetInt("output_index", -1);
    if (!output || raw < 0 || static_cast<size_t>(raw) >= output->array.size())
      return Invalid();
    const auto child = children.outputs.find(raw);
    if (child != children.outputs.end()) {
      turn = child->second.turn;
      Set(&loc, "turn_id", String(turn));
      Set(&loc, "output_index", Number(child->second.index));
    } else {
      if (IsPrivateAgentOutput(output->array[raw])) return {};
      Set(&loc, "output_index",
          Number(cursor.turn_output_offset + VisibleOutputs(*output, raw)));
    }
  }
  if (type == "response.output_item.added" ||
      type == "response.output_item.done") {
    const auto* item = event.Find("item");
    if (!item) return Invalid();
    if (item->GetString("type") == "function_call" &&
        (!automation_detail::IsToolItem(*item) || type.ends_with(".added")))
      return {};
    // Function results enter the turn as input items. The SDK's
    // item.done union accepts only model output, and the next model item
    // keeps its contiguous output index.
    if (item->GetString("type") == "function_call_output" &&
        automation_detail::IsToolItem(*item)) {
      if (type.ends_with(".done")) return {};
      Set(&loc, "output_index", Json{});
    }
    auto items = Array(), actions = Array();
    auto projected = IsAgentOperationResult(*item)
                         ? ParentActionItem(*item, *record.Find("response"),
                                            session, turn, &items)
                         : ProjectItem(*item, turn, &items, &actions, false);
    if (!projected.ok()) return projected;
    if (items.array.empty()) return {};
    Set(&loc, "item", std::move(items.array.front()));
    if (!Emit(events, prefix,
              type.ends_with(".done") ? "agent.session.turn.item.done"
                                      : "agent.session.turn.item.added",
              loc))
      return Invalid();
    if (type == "response.output_item.done") {
      const auto edges = children.item_done.find(event.GetInt("output_index"));
      if (edges != children.item_done.end())
        return EmitChildEvents(edges->second, prefix, events);
    }
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
  for (const auto* key :
       {"item_id", "content_index", "summary_index", "text", "delta"})
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
      (links->array.empty() && !data->GetBool("created_idle")))
    return Invalid();
  AgentEventCursor next;
  next.created_seen = true;
  for (const auto& link : links->array) {
    auto record = ResponsesState(objects).ReadRecord(
        link.GetString("response_id"), access);
    if (!record.ok()) return record;
    AgentSessionView step;
    auto settings = SessionSettings(*data, &link);
    if (!settings.ok()) return settings;
    auto projected = ProjectStep(settings.value, link, record.value, &step);
    if (!projected.ok()) return projected;
    const auto turn = step.turn.GetString("id");
    if (next.turn_id != turn) {
      next.turn_id = turn;
      next.turn_output_offset = 0;
      next.turn_usage = {};
      next.created_at = step.turn.GetInt("created_at");
      next.started_at = 0;
    }
    if (!next.started_at) next.started_at = step.turn.GetInt("started_at");
    const auto status = step.turn.GetString("status");
    if (status == "queued" || status == "in_progress") {
      if (&link != &links->array.back()) return Invalid();
      next.sequence = record.value.GetArray("events")->array.size();
      SubagentStepEvents children;
      auto indexed =
          IndexSubagentEvents(settings.value, record.value, &children);
      if (!indexed.ok()) return indexed;
      if (auto boundary = children.claims.find(next.sequence);
          boundary != children.claims.end())
        for (const auto& claim : boundary->second)
          next.emitted_child_claims.insert(claim.GetString("turn_id"));
    } else {
      const auto& output = *record.value.Find("response")->GetArray("output");
      next.turn_output_offset += VisibleOutputs(output, output.array.size());
      const auto& usage = *step.turn.Find("usage");
      if (!AddUsage(&next.session_usage, usage) ||
          !AddUsage(&next.turn_usage, usage))
        return Invalid();
      if (status == "waiting" && &link == &links->array.back()) {
        next.waiting = true;
        next.sequence = record.value.GetArray("events")->array.size();
      } else {
        ++next.step;
      }
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
  if (data->GetBool("created_idle") && !cursor->created_seen) {
    const auto at_creation = Object({{"session", *session}});
    if (!Emit(events, id, "agent.session.created", at_creation) ||
        !Emit(events, id, "agent.session.idle", at_creation))
      return Invalid();
    cursor->created_seen = true;
    *settled = true;
    return {};
  }
  if (cursor->step == links->array.size()) return {};
  if (cursor->waiting) {
    const auto& pending = links->array[cursor->step];
    if (pending.Has("cancel_requested_at")) {
      auto settings = SessionSettings(*data, &pending);
      if (!settings.ok()) return settings;
      auto stored = ResponsesState(objects).ReadRecord(
          pending.GetString("response_id"), access);
      if (!stored.ok()) return stored;
      AgentSessionView snapshot;
      auto projected =
          ProjectStep(settings.value, pending, stored.value, &snapshot);
      if (!projected.ok()) return projected;
      auto closed = CloseWaiting(std::move(snapshot), pending, *cursor, events);
      if (!closed.ok()) return closed;
      *settled = true;
    } else if (cursor->step + 1 == links->array.size()) {
      return {};
    }
    cursor->waiting = false;
    ++cursor->step;
    cursor->sequence = 0;
    cursor->emitted_child_claims.clear();
    if (*settled) return {};
  }
  const auto& link = links->array[cursor->step];
  auto settings = SessionSettings(*data, &link);
  if (!settings.ok()) return settings;
  const auto response_id = link.GetString("response_id");
  auto record = ResponsesState(objects).ReadRecord(response_id, access);
  if (!record.ok()) return record;
  if (record.value.GetString("agent_session_id") != id) return Invalid();
  SubagentStepEvents children;
  auto indexed = IndexSubagentEvents(settings.value, record.value, &children);
  if (!indexed.ok()) return indexed;
  const auto& journal = *record.value.GetArray("events");
  const auto& response = *record.value.Find("response");
  // Native Responses keeps the cancelled snapshot and its committed prefix.
  // There is no native response.cancelled SSE record. Emit the Agent terminal
  // after that prefix, including when storage reserved only the final snapshot.
  const auto status = response.GetString("status");
  if (cursor->sequence == journal.array.size()) {
    auto claims = EmitChildClaims(children, response_id, cursor, events);
    if (!claims.ok()) return claims;
  }
  if (cursor->sequence == journal.array.size() &&
      (status == "cancelled" ||
       (journal.array.empty() && status == "failed"))) {
    auto event =
        Object({{"type", String("response." + response.GetString("status"))},
                {"response", response}});
    bool end = false;
    auto result = Translate(settings.value, link, record.value, event, children,
                            *cursor, events, &end, settled);
    if (result.ok() && end) {
      ++cursor->step;
      cursor->sequence = 0;
      cursor->emitted_child_claims.clear();
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
    auto claims = EmitChildClaims(children, response_id, cursor, events);
    if (!claims.ok()) return claims;
    bool end = false;
    auto result = Translate(settings.value, link, record.value, event, children,
                            *cursor, events, &end, settled);
    if (!result.ok()) return result;
    for (auto i = before; i < events->array.size(); ++i)
      bytes += OutputJson(events->array[i]).size();
    ++cursor->sequence;
    if (end) {
      if (cursor->sequence != journal.array.size()) return Invalid();
      if (!cursor->waiting) {
        ++cursor->step;
        cursor->sequence = 0;
        cursor->emitted_child_claims.clear();
      }
      break;
    }
  }
  return {};
}
}  // namespace q4t::server
