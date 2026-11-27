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
  if (IsManagedAgentOperation(item) &&
      item.GetString("type") == "function_call_output") {
    Json receipt;
    if (!io::ParseJson(item.GetString("output"), &receipt, true,
                       kSchemaJsonLimits))
      return {};
    return receipt.GetString("owner");
  }
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
  std::string owner, action;
  Json args;
  Json result;
};
ObjectReply Decode(const Json& output, const Json& response, Call* call) {
  const auto* items = response.GetArray("output");
  const auto id = output.GetString("call_id");
  if (id.empty() || !items) return Invalid();
  const bool managed = IsManagedAgentOperation(output);
  call->owner = Owner(output);
  if (managed) {
    Json receipt;
    if (call->owner.empty() ||
        !io::ParseJson(output.GetString("output"), &receipt, true,
                       kSchemaJsonLimits) ||
        !receipt.Find("result") || !receipt.Find("result")->IsObject())
      return Invalid();
    call->result = *receipt.Find("result");
    call->action = output.GetString("name").substr(
        std::string_view("multi_agent_").size());
    if (IsChildAgentOutput(output) != (call->owner != "root")) return Invalid();
  } else {
    const auto* content = output.GetArray("output");
    if (!content || content->array.size() != 1 ||
        !io::ParseJson(content->array[0].GetString("text"), &call->result, true,
                       kSchemaJsonLimits) ||
        !call->result.IsObject())
      return Invalid();
    call->action = output.GetString("action");
  }
  for (const auto& row : items->array) {
    if (row.GetString("id") == output.GetString("id")) break;
    if (row.GetString("type") !=
            (managed ? "function_call" : "multi_agent_call") ||
        row.GetString("call_id") != id)
      continue;
    if (call->source ||
        (managed ? (!IsManagedAgentOperation(row) ||
                    IsChildAgentOutput(row) != IsChildAgentOutput(output) ||
                    row.GetString("name") != output.GetString("name"))
                 : (Owner(row) != call->owner ||
                    row.GetString("action") != call->action)) ||
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
  const auto& action = call.action;
  const auto& owner = call.owner;
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
      } else if (action == "close_subagent" || action == "resume_subagent") {
        Set(&item, "type", String(action + "_call"));
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

Json ChildResource(const Json& session, const std::string& path,
                   const std::string& parent, const std::string& task,
                   int64_t opened, int64_t closed) {
  return Object({{"id", String(PublicId(session, path))},
                 {"object", String("agent.session.subagent")},
                 {"session_id", *session.Find("id")},
                 {"name", String(path.substr(parent.size() + 1))},
                 {"instructions", Text(task)},
                 {"parent_agent_id", String(PublicId(session, parent))},
                 {"opened_at", Number(opened)},
                 {"closed_at", closed ? Number(closed) : Json{}},
                 {"status", String(closed ? "closed" : "active")}});
}

struct Member {
  std::string path;
  size_t output_count = 0;
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

Json TurnEvent(const Json& turn) {
  const auto status = turn.GetString("status");
  auto event =
      Object({{"type", String("agent.session.turn." +
                              (status == "queued" ? "created" : status))},
              {"session_id", *turn.Find("session_id")},
              {"turn_id", *turn.Find("id")},
              {"turn", turn}});
  if (status != "queued" && status != "in_progress") Set(&event, "usage", {});
  return event;
}

void StartRecorded(Member* child, const Json& session, const Call& call,
                   int64_t at, const Json* claim, const Json* ttl, size_t index,
                   SubagentStepEvents* events) {
  const auto started = !claim              ? 0
                       : claim->IsObject() ? claim->GetInt("at")
                                           : claim->AsInt();
  Start(child, session, *call.source, call.args.GetString("task"), at, started);
  child->output_count = 0;
  auto& turn = child->turns.array.back();
  Set(&turn, "prompt_cache_options",
      started && ttl ? Object({{"ttl", *ttl}}) : Json{});
  if (!events) return;
  auto queued = turn;
  Set(&queued, "status", String("queued"));
  Set(&queued, "started_at", {});
  Set(&queued, "prompt_cache_options", {});
  events->item_done[index].push_back(TurnEvent(queued));
  if (claim && claim->IsObject())
    events->claims[claim->GetInt("sequence")].push_back(TurnEvent(turn));
}

ObjectReply ProjectFamily(const Json& session, const Json& record,
                          Family* family,
                          SubagentStepEvents* events = nullptr) {
  const auto* response = record.Find("response");
  const auto* output = response ? response->GetArray("output") : nullptr;
  if (!output || output->array.size() > ResponsesOutput::kMaxItems)
    return Invalid();
  const auto* times = record.Find("agent_item_times");
  const auto* claims = record.Find("agent_claim_times");
  const auto* ttls = record.Find("agent_claim_cache_ttls");
  if (ttls && !ValidSubagentCacheTtls(*ttls, claims)) return Invalid();
  for (size_t index = 0; index < output->array.size(); ++index) {
    const auto& item = output->array[index];
    const auto type = item.GetString("type");
    if (!IsAgentOperationResult(item) &&
        !(type == "message" && Owner(item) != "root"))
      continue;
    const auto at = times ? times->GetInt(item.GetString("id"), 0) : 0;
    if (at <= 0) return Invalid();
    const auto owner = PublicId(session, Owner(item));
    auto* actor = family->Find(owner);
    if (Owner(item) != "root" && !actor) return Invalid();
    if (events && actor &&
        (type == "message" || item.GetString("action") != "list_agents")) {
      if (actor->turns.array.empty()) return Invalid();
      events->outputs[index] = {actor->turns.array.back().GetString("id"),
                                actor->output_count++};
    }
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
      if (turn.GetString("status") == "failed")
        Set(&turn, "error",
            Object({{"code", String("server_error")},
                    {"message", String("Incomplete subagent output")}}));
      if (events) events->item_done[index].push_back(TurnEvent(turn));
      continue;
    }
    Call call;
    auto decoded = Decode(item, *response, &call);
    if (!decoded.ok()) return decoded;
    const auto& action = call.action;
    if (actor && action != "list_agents") {
      if (actor->turns.array.empty()) return Invalid();
      auto& turn = actor->turns.array.back();
      Json operation;
      auto projected = Action(session, turn.GetString("id"), call, &operation);
      if (!projected.ok()) return projected;
      actor->items.array.push_back(std::move(operation));
    }
    if (call.result.Has("error")) continue;
    const auto item_id = item.GetString("id");
    const auto* claim = claims ? claims->Find(item_id) : nullptr;
    const auto* ttl = ttls ? ttls->Find(item_id) : nullptr;
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
      child.path = path;
      child.resource = ChildResource(session, path, Owner(item), task, at, 0);
      if (events)
        events->item_done[index].push_back(
            Object({{"type", String("agent.session.subagent.created")},
                    {"subagent", child.resource}}));
      StartRecorded(&child, session, call, at, claim, ttl, index, events);
      family->entries.push_back(std::move(child));
    } else if (action == "followup_task") {
      const auto path = call.args.GetString("target");
      const auto id = PublicId(session, path);
      if (call.result.GetString("agent_name") != path) return Invalid();
      auto* child = family->Find(id);
      // A replay page indexes one native step. An existing idle child's first
      // action here can be a follow-up to a task in an older step.
      if (!child && events) {
        family->index.emplace(id, family->entries.size());
        child = &family->entries.emplace_back();
        child->path = path;
        child->resource = Object({{"id", String(id)}});
      }
      if (!child) return Invalid();
      StartRecorded(child, session, call, at, claim, ttl, index, events);
    } else if (action == "interrupt_agent" || action == "close_subagent" ||
               action == "resume_subagent") {
      const auto target = call.args.GetString("target");
      auto* member = family->Find(PublicId(session, target));
      if (!events && !member) return Invalid();
      if (action != "interrupt_agent") {
        const auto parent = call.result.GetString("parent");
        const auto opened = call.result.GetInt("opened_at");
        const auto closed = call.result.GetInt("closed_at");
        if (call.result.GetString("agent_name") != target ||
            !target.starts_with(parent + "/") || opened <= 0 ||
            (action == "close_subagent") != (closed > 0))
          return Invalid();
        const auto* members = record.GetArray("agent_tree");
        if (!members || members->array.size() > ResponsesOutput::kMaxItems)
          return Invalid();
        const auto original =
            std::ranges::find_if(members->array, [&](const Json& entry) {
              return entry.GetString("name") == target &&
                     entry.GetString("parent") == parent;
            });
        if (original == members->array.end()) return Invalid();
        const auto* transcript = original->GetArray("messages");
        if (!transcript || transcript->array.empty() ||
            transcript->array.front().GetString("role") != "user")
          return Invalid();
        const auto* task = transcript->array.front().Find("content");
        if (!task || !task->IsString()) return Invalid();
        auto resource =
            ChildResource(session, target, parent, task->str, opened, closed);
        if (member) member->resource = resource;
        if (events)
          events->item_done[index].push_back(Object(
              {{"type", String(closed ? "agent.session.subagent.closed"
                                      : "agent.session.subagent.active")},
               {"subagent", std::move(resource)}}));
        if (!closed) continue;
      }
      for (auto& member : family->entries) {
        if (member.path != target && !member.path.starts_with(target + "/"))
          continue;
        if (member.turns.array.empty()) return Invalid();
        auto& turn = member.turns.array.back();
        const auto status = turn.GetString("status");
        if (status == "queued" || status == "in_progress") {
          Set(&turn, "status", String("cancelled"));
          Set(&turn, "completed_at", Number(at));
          if (events) events->item_done[index].push_back(TurnEvent(turn));
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
        if (events) events->terminal.push_back(TurnEvent(turn));
      }
    }
  }
  return {};
}
}  // namespace

bool ValidSubagentCacheTtls(const Json& ttls, const Json* claims) {
  if (!ttls.IsObject() || !claims || !claims->IsObject() ||
      ttls.object.size() > ResponsesOutput::kMaxItems)
    return false;
  return std::ranges::all_of(ttls.object, [&](const auto& field) {
    const auto& [id, ttl] = field;
    return claims->Has(id) && ttl.IsString() &&
           (ttl.str == "30m" || ttl.str == "12h");
  });
}

ObjectReply IndexSubagentEvents(const Json& session, const Json& record,
                                SubagentStepEvents* events) {
  if (!record.Has("agent_item_times")) return {};
  Family family;
  return ProjectFamily(session, record, &family, events);
}

ObjectReply ReadSubagentOpening(ObjectStore& objects,
                                const std::string& session,
                                const std::string& path,
                                const StateAccess& access, int64_t* opened) {
  const auto id = "subagent_" + Digest(session + "/" + path).substr(0, 32);
  const auto route = "/v1/agents/sessions/" + session + "/subagents/" + id;
  auto resource =
      ReadSessionSubagents(objects, ParseAgentSessionRoute(route), {}, access);
  if (resource.ok()) *opened = resource.value.GetInt("opened_at");
  return resource;
}

bool IsManagedAgentOperation(const Json& item) {
  const auto type = item.GetString("type");
  if (type != "function_call" && type != "function_call_output") return false;
  const auto name = item.GetString("name");
  if (name != "multi_agent_close_subagent" &&
      name != "multi_agent_resume_subagent")
    return false;
  const auto id = item.GetString("id");
  return id.starts_with("agentop_root_") || id.starts_with("agentop_child_");
}

bool IsChildAgentOutput(const Json& item) {
  if (IsManagedAgentOperation(item))
    return item.GetString("id").starts_with("agentop_child_");
  const auto* owner = item.Find("agent");
  return owner && owner->GetString("agent_name") != "root";
}

bool IsAgentOperationResult(const Json& item) {
  const auto type = item.GetString("type");
  return type == "multi_agent_call_output" ||
         (type == "function_call_output" && IsManagedAgentOperation(item));
}

bool IsPrivateAgentOutput(const Json& item) {
  const auto type = item.GetString("type");
  return IsAgentDiscoveryItem(type) || IsChildAgentOutput(item) ||
         (type == "function_call" && IsManagedAgentOperation(item)) ||
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
