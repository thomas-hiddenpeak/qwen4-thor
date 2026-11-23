#include "agent_subagent_internal.h"

#include <algorithm>
#include <deque>
#include <unordered_map>

#include "agent_session_internal.h"
#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_output.h"

namespace q4t::server::agent_detail {
namespace {
using namespace object_detail;
using Kind = AgentSessionRouteKind;

ObjectReply Invalid() {
  return Error(503, "agent_session_record_invalid",
               "Invalid stored subagent history");
}
ObjectReply Missing() {
  return Error(404, "object_not_found", "Subagent or turn not found");
}

std::string Owner(const Json& item) {
  const auto* agent = item.Find("agent");
  return agent ? agent->GetString("agent_name") : "root";
}
std::string PublicId(const Json& session, const std::string& path) {
  if (path == "root") return session.Find("agent")->GetString("id");
  return "subagent_" +
         Digest(session.GetString("id") + "/" + path).substr(0, 32);
}
Json Text(const std::string& value, const char* type = "output_text") {
  return Array({Object({{"type", String(type)}, {"text", String(value)}})});
}

struct Call {
  const Json* source = nullptr;
  Json args;
  Json result;
};
ObjectReply Decode(const Json& output, const Json& response, Call* call) {
  const auto* items = response.GetArray("output");
  const auto id = output.GetString("call_id");
  const auto* content = output.GetArray("output");
  if (id.empty() || !items || !content || content->array.size() != 1 ||
      !io::ParseJson(content->array[0].GetString("text"), &call->result, true,
                     kSchemaJsonLimits) ||
      !call->result.IsObject())
    return Invalid();
  for (const auto& row : items->array) {
    if (row.GetString("id") == output.GetString("id")) break;
    if (row.GetString("type") != "multi_agent_call" ||
        row.GetString("call_id") != id)
      continue;
    if (call->source || Owner(row) != Owner(output) ||
        row.GetString("action") != output.GetString("action") ||
        !io::ParseJson(row.GetString("arguments"), &call->args, true,
                       kSchemaJsonLimits) ||
        !call->args.IsObject())
      return Invalid();
    call->source = &row;
  }
  return call->source ? ObjectReply{} : Invalid();
}

ObjectReply Action(const Json& session, const std::string& turn,
                   const Call& call, Json* result) {
  const auto action = call.source->GetString("action"),
             owner = Owner(*call.source);
  if (action == "list_agents") return {};
  const auto id = [&](const std::string& path) {
    return String(PublicId(session, path));
  };
  const auto target = call.args.GetString("target");
  auto item =
      Object({{"id", *call.source->Find("id")},
              {"turn_id", String(turn)},
              {"status",
               String(call.result.Has("error") ? "incomplete" : "completed")}});
  if (action == "spawn_agent") {
    Set(&item, "type", String("create_subagent_call"));
    Set(&item, "agent_id", id(owner));
    Set(&item, "content", Text(call.args.GetString("task")));
    Set(&item, "model", {});
    Set(&item, "reasoning_effort", {});
  } else {
    Set(&item, "sender_agent_id", id(owner));
    if (action == "wait_agent") {
      Set(&item, "type", String("wait_for_subagents_call"));
      Set(&item, "recipient_agent_ids", Array({id(target)}));
    } else {
      Set(&item, "recipient_agent_id", id(target));
      if (action == "send_message" || action == "followup_task") {
        Set(&item, "type", String("send_subagent_input_call"));
        Set(&item, "content",
            Text(call.args.GetString(action == "send_message" ? "message"
                                                              : "task")));
      } else if (action == "interrupt_agent") {
        Set(&item, "type", String("interrupt_subagent_call"));
      } else
        return Invalid();
    }
  }
  ChatPlanError check;
  if (!ValidateAgentSession(item, AgentSessionShape::kItem, &check))
    return Invalid();
  *result = std::move(item);
  return {};
}

// Read chronological receipts once. Descending pages keep the last limit+1
// rows before the cursor. Ascending pages keep the first limit+1 after it.
class HistoryPage {
 public:
  explicit HistoryPage(const ItemViewOptions& options) : options_(options) {}
  void Add(Json row) {
    if (!options_.after.empty()) {
      if (row.GetString("id") == options_.after) {
        matched_ = true;
        return;
      }
      if (options_.descending == matched_) return;
    }
    const auto maximum = static_cast<size_t>(options_.limit) + 1;
    if (rows_.size() == maximum) {
      if (!options_.descending) return;
      rows_.pop_front();
    }
    rows_.push_back(std::move(row));
  }
  ObjectReply Finish() {
    auto rows = Array();
    if (!options_.after.empty() && !matched_)
      return PageStoredItems(rows, options_, StoredItemView::kAgentSession);
    for (auto& row : rows_) rows.array.push_back(std::move(row));
    auto selected = options_;
    selected.after.clear();
    return PageStoredItems(rows, selected, StoredItemView::kAgentSession);
  }

 private:
  const ItemViewOptions& options_;
  std::deque<Json> rows_;
  bool matched_ = false;
};

struct Member {
  Json resource, turns = Array(), items = Array();
};
struct Family {
  std::vector<Member> entries;
  std::unordered_map<std::string, size_t> index;
  Member* Find(const std::string& id) {
    const auto it = index.find(id);
    return it == index.end() ? nullptr : &entries[it->second];
  }
};
std::string TurnId(const Json& call) {
  return "turn_" +
         Digest(call.GetString("id") + "/subagent-turn").substr(0, 32);
}
void Start(Member* member, const Json& session, const Json& call,
           const std::string& input, int64_t at, int64_t started) {
  const auto turn = TurnId(call);
  member->turns.array.push_back(
      Object({{"id", String(turn)},
              {"object", String("agent.session.turn")},
              {"session_id", *session.Find("id")},
              {"agent_id", *member->resource.Find("id")},
              {"subagent_id", *member->resource.Find("id")},
              {"status", String(started ? "in_progress" : "queued")},
              {"created_at", Number(at)},
              {"started_at", started ? Number(started) : Json{}},
              {"completed_at", {}},
              {"error", {}},
              {"usage", {}}}));
  member->items.array.push_back(
      Object({{"id", String("msg_" + Digest(turn + "/input").substr(0, 32))},
              {"turn_id", String(turn)},
              {"type", String("message")},
              {"role", String("user")},
              {"phase", {}},
              {"status", String("completed")},
              {"content", Text(input, "input_text")}}));
}

ObjectReply ProjectFamily(const Json& session, const Json& record,
                          Family* family) {
  const auto* response = record.Find("response");
  const auto* output = response ? response->GetArray("output") : nullptr;
  if (!output || output->array.size() > ResponsesOutput::kMaxItems)
    return Invalid();
  const auto* times = record.Find("agent_item_times");
  const auto* claims = record.Find("agent_claim_times");
  for (const auto& item : output->array) {
    const auto type = item.GetString("type");
    if (type != "multi_agent_call_output" &&
        !(type == "message" && Owner(item) != "root"))
      continue;
    const auto at = times ? times->GetInt(item.GetString("id"), 0) : 0;
    if (at <= 0) return Invalid();
    const auto owner = PublicId(session, Owner(item));
    auto* actor = family->Find(owner);
    if (Owner(item) != "root" && !actor) return Invalid();
    if (type == "message") {
      if (!actor || actor->turns.array.empty()) return Invalid();
      auto& turn = actor->turns.array.back();
      auto actions = Array();
      auto added = ProjectItem(item, turn.GetString("id"), &actor->items,
                               &actions, true);
      if (!added.ok()) return added;
      Set(&turn, "completed_at", Number(at));
      Set(&turn, "status",
          String(item.GetString("status") == "completed" ? "completed"
                                                         : "failed"));
      continue;
    }
    Call call;
    auto decoded = Decode(item, *response, &call);
    if (!decoded.ok()) return decoded;
    const auto action = item.GetString("action");
    if (actor && action != "list_agents") {
      if (actor->turns.array.empty()) return Invalid();
      auto& turn = actor->turns.array.back();
      Json operation;
      auto projected = Action(session, turn.GetString("id"), call, &operation);
      if (!projected.ok()) return projected;
      actor->items.array.push_back(std::move(operation));
    }
    if (call.result.Has("error")) continue;
    if (action == "spawn_agent") {
      const auto name = call.args.GetString("name"),
                 task = call.args.GetString("task");
      const auto path = Owner(item) + "/" + name;
      if (call.result.GetString("agent_name") != path ||
          call.result.GetString("parent") != Owner(item) || name.empty())
        return Invalid();
      const auto id = PublicId(session, path);
      if (family->entries.size() >= ResponsesOutput::kMaxItems ||
          !family->index.emplace(id, family->entries.size()).second)
        return Invalid();
      Member child;
      child.resource = Object({{"id", String(id)},
                               {"object", String("agent.session.subagent")},
                               {"session_id", *session.Find("id")},
                               {"name", String(name)},
                               {"instructions", Text(task)},
                               {"parent_agent_id", String(owner)},
                               {"opened_at", Number(at)},
                               {"closed_at", {}},
                               {"status", String("active")}});
      Start(&child, session, *call.source, task, at,
            claims ? claims->GetInt(item.GetString("id")) : 0);
      family->entries.push_back(std::move(child));
    } else if (action == "followup_task") {
      auto* child =
          family->Find(PublicId(session, call.args.GetString("target")));
      if (!child) return Invalid();
      Start(child, session, *call.source, call.args.GetString("task"), at,
            claims ? claims->GetInt(item.GetString("id")) : 0);
    } else if (action == "interrupt_agent") {
      const auto target = call.args.GetString("target");
      const auto interrupted = PublicId(session, target);
      auto* child = family->Find(interrupted);
      if (!child) return Invalid();
      for (auto& member : family->entries) {
        if (member.turns.array.empty()) return Invalid();
        auto id = member.resource.GetString("id");
        while (id != interrupted) {
          auto* parent = family->Find(id);
          if (!parent) break;
          id = parent->resource.GetString("parent_agent_id");
        }
        if (id != interrupted) continue;
        auto& turn = member.turns.array.back();
        const auto status = turn.GetString("status");
        if (status == "queued" || status == "in_progress") {
          Set(&turn, "status", String("cancelled"));
          Set(&turn, "completed_at", Number(at));
        }
      }
    }
  }
  // A terminal worker can leave a queued/running task behind on interruption.
  // Its history stays available to future user turns in this session.
  const auto ended = record.GetInt("agent_turn_finished_at");
  if (ended) {
    for (auto& member : family->entries) {
      auto& turn = member.turns.array.back();
      if (turn.GetString("status") == "queued" ||
          turn.GetString("status") == "in_progress") {
        Set(&turn, "status", String("cancelled"));
        Set(&turn, "completed_at", Number(ended));
      }
    }
  }
  return {};
}
}  // namespace

bool IsPrivateAgentOutput(const Json& item) {
  const auto type = item.GetString("type");
  return IsAgentDiscoveryItem(type) || Owner(item) != "root" ||
         type == "multi_agent_call" ||
         (type == "multi_agent_call_output" &&
          item.GetString("action") == "list_agents");
}

ObjectReply ParentActionItem(const Json& output, const Json& response,
                             const Json& session, const std::string& turn,
                             Json* items) {
  Call call;
  auto decoded = Decode(output, response, &call);
  if (!decoded.ok()) return decoded;
  Json item;
  auto projected = Action(session, turn, call, &item);
  if (projected.ok() && !item.IsNull()) items->array.push_back(std::move(item));
  return projected;
}

ObjectReply ReadSessionSubagents(ObjectStore& objects,
                                 const AgentSessionRoute& route,
                                 const ItemViewOptions& options,
                                 const StateAccess& access) {
  const std::string id(route.session);
  auto row = ReadRow(objects, id, access);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* links = data ? data->GetArray("turns") : nullptr;
  if (!links || links->array.size() >= kMaxTransactionObjects) return Invalid();
  if (options.limit < 1 || options.limit > 100)
    return PageStoredItems(Array(), options, StoredItemView::kAgentSession);
  Family family;
  HistoryPage page(options);
  Json selected_turn;
  const auto collect_turn = [&](Json& turn) {
    if (!route.turn.empty() && turn.GetString("id") == route.turn)
      selected_turn = turn;
    if (route.kind == Kind::kSubagentTurns) page.Add(std::move(turn));
  };
  ResponsesState state(objects);
  for (const auto& link : links->array) {
    if (!link.GetBool("multi_agent")) continue;
    auto stored = state.ReadRecord(link.GetString("response_id"), access);
    if (!stored.ok()) return stored;
    if (stored.value.GetString("agent_session_id") != id) return Invalid();
    if (!stored.value.Has("agent_item_times")) continue;
    auto settings = SessionSettings(*data, &link);
    if (!settings.ok()) return settings;
    auto built = ProjectFamily(settings.value, stored.value, &family);
    if (!built.ok()) return built;
    for (auto& member : family.entries) {
      const bool wanted = member.resource.GetString("id") == route.subagent;
      if (wanted && (route.kind == Kind::kSubagentItems ||
                     route.kind == Kind::kSubagentTurnItems)) {
        for (auto& item : member.items.array) {
          if (route.turn.empty() || item.GetString("turn_id") == route.turn)
            page.Add(std::move(item));
        }
      }
      member.items.array.clear();
      // Only the last turn can still be settled by a later native step.
      if (member.turns.array.size() > 1) {
        const auto last = std::prev(member.turns.array.end());
        if (wanted)
          for (auto it = member.turns.array.begin(); it != last; ++it)
            collect_turn(*it);
        member.turns.array.erase(member.turns.array.begin(), last);
      }
    }
  }
  if (route.kind == Kind::kSubagents) {
    for (auto& member : family.entries) page.Add(std::move(member.resource));
    return page.Finish();
  }
  auto* target = family.Find(std::string(route.subagent));
  if (!target) return Missing();
  if (route.kind == Kind::kSubagent) {
    row.value = std::move(target->resource);
    return row;
  }
  if (!target->turns.array.empty()) collect_turn(target->turns.array.back());
  if (!route.turn.empty() && selected_turn.IsNull()) return Missing();
  if (route.kind == Kind::kSubagentTurn) {
    row.value = std::move(selected_turn);
    return row;
  }
  return page.Finish();
}
}  // namespace q4t::server::agent_detail
