#include "agent_automation_tool.h"
#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <ctime>
#include <limits>

#include "object_protocol.h"
#include "agent_subagent_internal.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server {
using namespace object_detail;

Status ResponsesOutput::AppendAgentItem(const io::Json& item,
                                        std::string* wire) {
  wire->clear();
  const auto* agent = item.Find("agent");
  const auto type = item.GetString("type");
  const bool managed = agent_detail::IsManagedAgentOperation(item) ||
                       automation_detail::IsToolItem(item);
  if (!started_ || terminal_ ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems ||
      (!managed && (!agent || agent->GetString("agent_name").empty() ||
                    (type != "multi_agent_call" &&
                     type != "multi_agent_call_output" && type != "message"))))
    return Status::Fail("Invalid hosted agent item");
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "multi_agent", true);
  if (type == "message" && (!item.GetArray("content") ||
                            item.GetArray("content")->array.size() != 1))
    return Status::Fail("Invalid agent answer");
  if (status) status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  const auto index = Items().array.size();
  Items().array.push_back(item);
  call_indices_.push_back(std::numeric_limits<size_t>::max());
  if (type == "message") {
    auto opened = item;
    Set(&opened, "status", String("in_progress"));
    Set(&opened, "content", Array());
    Event(
        "response.output_item.added",
        Object({{"output_index", Number(index)}, {"item", std::move(opened)}}),
        wire);
    const auto& part = item.GetArray("content")->array.front();
    const bool refusal = part.GetString("type") == "refusal";
    const auto* value = part.Find(refusal ? "refusal" : "text");
    auto empty = part;
    Set(&empty, refusal ? "refusal" : "text", String(""));
    auto fields = Location(index, true);
    Set(&fields, "part", std::move(empty));
    Event("response.content_part.added", std::move(fields), wire);
    if (!value->str.empty()) {
      fields = Location(index, true);
      Set(&fields, "delta", *value);
      if (!refusal) Set(&fields, "logprobs", Array());
      Event(refusal ? "response.refusal.delta" : "response.output_text.delta",
            std::move(fields), wire);
    }
    CloseItem(index, item.GetString("status") == "incomplete", wire);
    return {};
  }
  auto fields = Object({{"output_index", Number(index)}, {"item", item}});
  if (agent) Set(&fields, "agent", *agent);
  Event("response.output_item.added", fields, wire);
  Event("response.output_item.done", std::move(fields), wire);
  closed_items_.insert(index);
  return {};
}

Status ResponsesExecution::AppendAgent(const ResponseDelta& delta,
                                       bool* handled, std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  const auto& call = delta.call;
  const bool wakeup =
      delta.tool_start
          ? session_automations_ && call.name == automation_detail::kToolName
          : automation_pending_.contains(call.index);
  if (delta.tool_start && (agent_pending_.contains(call.index) ||
                           automation_pending_.contains(call.index)))
    return Status::Fail("Duplicate hosted agent call index");
  auto& pending = wakeup ? automation_pending_ : agent_pending_;
  auto& bytes = wakeup ? automation_argument_bytes_ : agent_argument_bytes_;
  auto found = pending.find(call.index);
  if (delta.tool_start && !wakeup &&
      (!agents_ ||
       ResponsesAgentTree::ActionName(call.name, !agent_session_id_.empty())
           .empty()))
    return {};
  if (!delta.tool_start && found == pending.end()) return {};
  *handled = true;
  wire->clear();
  if (call.custom ||
      call.arguments.size() > ResponsesOutput::kStorageLimit - bytes)
    return Status::Fail("Agent call exceeds argument capacity");
  if (delta.tool_start) {
    if (found != pending.end() || call.id.empty() ||
        call.arguments.size() > ChatResponseParser::kMaxToolBytes ||
        automation_pending_.size() + agent_pending_.size() >=
            kMaxNativeToolCalls)
      return Status::Fail("Invalid agent call");
    pending.emplace(call.index, call);
  } else {
    auto& original = found->second;
    if ((!call.id.empty() && call.id != original.id) ||
        (!call.name.empty() && call.name != original.name) ||
        call.arguments.size() >
            ChatResponseParser::kMaxToolBytes - original.arguments.size())
      return Status::Fail("Agent call changed identity or exceeded capacity");
    original.arguments += call.arguments;
  }
  bytes += call.arguments.size();
  return {};
}

namespace {
using AgentEmit = std::function<ObjectReply(const Json&)>;
using LifecycleCall = std::function<ObjectReply(
    const std::string&, const std::string&, std::string_view, const Json&,
    const std::string&, bool, Json*)>;
using AgentClaim = std::function<ObjectReply(const std::string&)>;

// The model runner returns after releasing its GPU sequence. Dispatching a
// deeper wait never nests GPU ownership or starts another HTTP request.
class AgentDispatch {
 public:
  AgentDispatch(ResponsesAgentTree& tree,
                const ResponsesExecution::AgentModelStep& step,
                const AgentEmit& emit, const AgentClaim& claim,
                const LifecycleCall& lifecycle, const StateAccess& access,
                size_t* used, size_t limit)
      : tree_(tree),
        step_(step),
        emit_(emit),
        claim_(claim),
        lifecycle_(lifecycle),
        access_(access),
        used_(*used),
        limit_(limit) {}

  ObjectReply Call(const std::string& owner, const std::string& id,
                   std::string_view name, const std::string& bytes,
                   Json* result) {
    const auto action = ResponsesAgentTree::ActionName(name, bool(lifecycle_));
    Json args;
    if (id.empty() || action.empty() ||
        !io::ParseJson(bytes, &args, true, kSchemaJsonLimits) ||
        !args.IsObject())
      return Error(502, "agent_arguments_invalid",
                   "Malformed generated agent call");
    auto check = Check();
    if (!check.ok()) return check;
    if (action == "close_subagent" || action == "resume_subagent") {
      const bool available = used_ < limit_;
      if (available) ++used_;
      return lifecycle_(owner, id, action, args, bytes, available, result);
    }
    auto item = Object({{"agent", Object({{"agent_name", String(owner)}})},
                        {"action", String(std::string(action))},
                        {"call_id", String(id)}});
    auto call = item;
    Set(&call, "id", String(detail::NewRequestId("agentcall_")));
    Set(&call, "type", String("multi_agent_call"));
    Set(&call, "arguments", String(bytes));
    check = emit_(call);
    if (!check.ok()) return check;
    if (used_ >= limit_) {
      *result = Object({{"error", String("tool_call_budget_exhausted")}});
    } else {
      ++used_;
      auto status = tree_.Act(owner, action, args, result);
      if (!status) {
        *result = Object({{"error", String(status.message())}});
      } else if (action == "wait_agent" &&
                 result->GetString("status") == "queued") {
        const auto target = result->GetString("agent_name");
        check = Run(target);
        if (!check.ok()) return check;
        status = tree_.Act(owner, action, args, result);
        if (!status) return Error(500, "agent_state_invalid", status.message());
      }
    }
    Set(&item, "id", String(detail::NewRequestId("agentresult_")));
    Set(&item, "type", String("multi_agent_call_output"));
    Set(&item, "output",
        Array({Object({{"type", String("output_text")},
                       {"text", String(OutputJson(*result))},
                       {"annotations", Array()}})}));
    return emit_(item);
  }

  ObjectReply Run(const std::string& target) {
    ResponsesAgentTree::Turn turn;
    if (!tree_.Claim(target, &turn))
      return Error(500, "agent_state_invalid", "Agent turn already claimed");
    auto started = claim_(target);
    if (!started.ok()) return started;
    for (;;) {
      auto checked = Check();
      if (!checked.ok()) return checked;
      Json message;
      bool completed = false;
      auto reply = step_(turn, used_ < limit_, &message, &completed);
      if (!reply.ok()) return reply;
      const auto* calls = message.GetArray("tool_calls");
      if (!completed || !calls || calls->array.empty()) {
        const auto* refusal = message.Find("refusal");
        const bool refused =
            refusal && refusal->IsString() && !refusal->str.empty();
        const auto answer =
            refused ? refusal->str : message.GetString("content");
        auto status = tree_.Finish(turn, answer, completed);
        if (!status) return Error(503, "agent_state_invalid", status.message());
        auto part = refused ? Object({{"type", String("refusal")},
                                      {"refusal", String(answer)}})
                            : Object({{"type", String("output_text")},
                                      {"text", String(answer)},
                                      {"annotations", Array()},
                                      {"logprobs", Array()}});
        return emit_(
            Object({{"id", String(detail::NewRequestId("msg_"))},
                    {"type", String("message")},
                    {"role", String("assistant")},
                    {"status", String(completed ? "completed" : "incomplete")},
                    {"agent", Object({{"agent_name", String(turn.name)}})},
                    {"content", Array({std::move(part)})}}));
      }
      auto exchange = Array({message});
      for (const auto& call : calls->array) {
        const auto* function = call.Find("function");
        if (!function)
          return Error(502, "agent_arguments_invalid",
                       "Missing agent function");
        Json value;
        reply = Call(target, call.GetString("id"), function->GetString("name"),
                     function->GetString("arguments"), &value);
        if (!reply.ok()) return reply;
        exchange.array.push_back(
            Object({{"role", String("tool")},
                    {"tool_call_id", *call.Find("id")},
                    {"content", String(OutputJson(value))}}));
      }
      auto status = tree_.RecordExchange(&turn, exchange);
      if (!status) return Error(503, "agent_state_invalid", status.message());
    }
  }

 private:
  ObjectReply Check() const {
    if (access_.cancelled && access_.cancelled())
      return Error(499, "request_cancelled", "Agent request cancelled");
    if (std::chrono::steady_clock::now() >= access_.deadline)
      return Error(408, "request_deadline_exceeded", "Agent deadline expired");
    return {};
  }
  ResponsesAgentTree& tree_;
  const ResponsesExecution::AgentModelStep& step_;
  const AgentEmit& emit_;
  const AgentClaim& claim_;
  const LifecycleCall& lifecycle_;
  const StateAccess& access_;
  size_t& used_;
  size_t limit_;
};
}  // namespace

void ResponsesExecution::ConfigureAgentModelStep(
    const ResponsesAgentTree::Turn& turn, bool allow_tools,
    Json* request) const {
  auto messages = turn.messages;
  if (!agent_session_id_.empty()) {
    auto instructions = Object(
        {{"role", String("system")},
         {"content",
          String("You are the delegated agent " + turn.name +
                 ". Complete the assigned task and return your answer to your "
                 "parent. Use a tool only when the task needs one.")}});
    messages.array.insert(messages.array.begin(), std::move(instructions));
  }
  Set(request, "messages", std::move(messages));
  if (allow_tools)
    Set(request, "tools",
        agent_session_id_.empty() ? ResponsesAgentTree::ModelTools()
                                  : agents_->ChildTools(turn.name));
}

ObjectReply ResponsesExecution::RunAgentLifecycle(
    const std::string& owner, const std::string& call_id,
    std::string_view action, const Json& args, const std::string& arguments,
    bool available, Json* result,
    const std::function<bool(const std::string&)>& publish) {
  auto tree = *agents_;
  if (available) {
    int64_t opened = 0;
    const auto target = args.GetString("target");
    if (tree.OpenedAt(target) == 0) {
      auto old = agent_detail::ReadSubagentOpening(
          objects_, plan_.agent_session_id, target, access_, &opened);
      if (!old.ok()) return old;
    }
    auto changed =
        tree.SetClosed(owner, action == "close_subagent", args, result, opened);
    if (!changed) *result = Object({{"error", String(changed.message())}});
  } else {
    *result = Object({{"error", String("tool_call_budget_exhausted")}});
  }
  const char* prefix = owner == "root" ? "agentop_root_" : "agentop_child_";
  auto call = Object({{"name", String("multi_agent_" + std::string(action))},
                      {"type", String("function_call")},
                      {"id", String(detail::NewRequestId(prefix))},
                      {"call_id", String(call_id)},
                      {"status", String("completed")},
                      {"arguments", String(arguments)}});
  auto receipt =
      Object({{"name", *call.Find("name")},
              {"type", String("function_call_output")},
              {"id", String(detail::NewRequestId(prefix))},
              {"call_id", String(call_id)},
              {"status", String("completed")},
              {"output", String(OutputJson(Object({{"owner", String(owner)},
                                                   {"result", *result}})))}});
  // Both standard Responses items and the actual tree change share a single
  // checkpoint.
  auto candidate = output_;
  std::string first, last;
  auto status = candidate.AppendAgentItem(call, &first);
  if (status) status = candidate.AppendAgentItem(receipt, &last);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  output_ = std::move(candidate);
  *agents_ = std::move(tree);
  plan_.agent_tree = agents_->SaveSession();
  if (!plan_.agent_item_times.IsObject()) plan_.agent_item_times = Object();
  const auto at = Number(std::time(nullptr));
  for (const auto* item : {&call, &receipt})
    Set(&plan_.agent_item_times, item->GetString("id"), at);
  first += last;
  status = RecordOutput(status, &first);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  auto saved = Checkpoint();
  if (!saved.ok()) return saved;
  if (publish && !publish(first))
    return Error(499, "request_cancelled", "Agent subscriber cancelled");
  return ObjectReply{};
}

ObjectReply ResponsesExecution::RunAgents(
    size_t completed_calls, const AgentModelStep& step,
    const std::function<bool(const std::string&)>& publish) {
  if (!begun_ || plan_.finished || commit_unknown_ || !agents_ || !step ||
      (!PendingAgents() && !QueuedAgents()))
    return Error(500, "response_state_invalid", "No executable agent calls");
  const AgentEmit emit = [&](const Json& item) -> ObjectReply {
    std::string wire;
    auto status = output_.AppendAgentItem(item, &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    if (!plan_.agent_session_id.empty()) {
      const auto action = item.GetString("action");
      auto at = std::time(nullptr);
      if (item.GetString("type") == "multi_agent_call_output" &&
          (action == "spawn_agent" || action == "followup_task")) {
        Json result;
        const auto& text = item.GetArray("output")->array.front();
        if (!io::ParseJson(text.GetString("text"), &result, true,
                           kSchemaJsonLimits))
          return Error(503, "agent_state_invalid",
                       "Invalid native dispatch result");
        if (result.GetString("status") == "queued") {
          const auto target = result.GetString("agent_name");
          agent_queued_items_[target] = item.GetString("id");
          if (action == "spawn_agent") at = agents_->OpenedAt(target);
        }
      }
      plan_.agent_tree = agents_->SaveSession();
      if (!plan_.agent_item_times.IsObject()) plan_.agent_item_times = Object();
      Set(&plan_.agent_item_times, item.GetString("id"), Number(at));
    }
    status = RecordOutput(status, &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    auto saved = Checkpoint();
    if (!saved.ok()) return saved;
    if (publish && !publish(wire))
      return Error(499, "request_cancelled", "Agent subscriber cancelled");
    return {};
  };
  const auto limit =
      std::min<uint64_t>(kMaxNativeToolCalls,
                         envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  const AgentClaim claim = [&](const std::string& target) -> ObjectReply {
    if (plan_.agent_session_id.empty()) return {};
    const auto found = agent_queued_items_.find(target);
    if (found == agent_queued_items_.end())
      return Error(503, "agent_state_invalid", "Missing queued child dispatch");
    if (!plan_.agent_claim_times.IsObject()) plan_.agent_claim_times = Object();
    Set(&plan_.agent_claim_times, found->second,
        Object({{"at", Number(std::time(nullptr))},
                {"sequence", Number(events_.array.size())}}));
    agent_queued_items_.erase(found);
    plan_.agent_tree = agents_->SaveSession();
    return Checkpoint();
  };
  LifecycleCall lifecycle;
  if (!plan_.agent_session_id.empty()) {
    lifecycle = [&](const std::string& owner, const std::string& call_id,
                    std::string_view action, const Json& args,
                    const std::string& arguments, bool available,
                    Json* result) {
      return RunAgentLifecycle(owner, call_id, action, args, arguments,
                               available, result, publish);
    };
  }
  AgentDispatch dispatch(*agents_, step, emit, claim, lifecycle, access_,
                         &native_tool_calls_, limit);
  for (const auto& [index, call] : agent_pending_) {
    Json arguments;
    if (index >= completed_calls ||
        !io::ParseJson(call.arguments, &arguments, true, kSchemaJsonLimits) ||
        !arguments.IsObject())
      return Error(502, "agent_call_incomplete", "Incomplete agent arguments");
  }
  auto pending = std::move(agent_pending_);
  const auto eligible = agents_->QueuedNames();
  agent_pending_.clear();
  agent_argument_bytes_ = 0;
  for (const auto& [index, call] : pending) {
    (void)index;
    Json result;
    auto reply =
        dispatch.Call("root", call.id, call.name, call.arguments, &result);
    if (!reply.ok()) return reply;
  }
  // A spawned task yields one parent model round. A following interrupt or
  // message can reach it before a GPU slot is claimed. When the root returns
  // or exhausts tool calls, finish the remaining work within its same budget.
  const bool drain = pending.empty() || native_tool_calls_ >= limit;
  for (const auto& target : eligible) {
    if (!agents_->Queued(target)) continue;
    auto reply = dispatch.Run(target);
    if (!reply.ok()) return reply;
  }
  while (drain && QueuedAgents()) {
    auto reply = dispatch.Run(agents_->FirstQueued());
    if (!reply.ok()) return reply;
  }
  return {};
}
}  // namespace q4t::server
