#include "q4t/server/responses_agent_tree.h"

#include <algorithm>
#include <ctime>

#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_output.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;

bool ValidLeaf(std::string_view name) {
  return !name.empty() && name.size() <= 64 &&
         std::all_of(name.begin(), name.end(), [](char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
         });
}
Json Message(std::string_view role, std::string text) {
  return Object({{"role", String(std::string(role))},
                 {"content", String(std::move(text))}});
}
bool IsDescendant(std::string_view name, std::string_view ancestor) {
  return name.size() > ancestor.size() && name.starts_with(ancestor) &&
         name[ancestor.size()] == '/';
}
}  // namespace

const Json& ResponsesAgentTree::ModelTools(bool managed) {
  const auto build = [](bool with_lifecycle) {
    auto definitions = Array();
    const auto add = [&](std::string name, std::string description,
                         std::initializer_list<const char*> fields) {
      auto properties = Object(), required = Array();
      for (const auto* field : fields) {
        Set(&properties, field, Object({{"type", String("string")}}));
        required.array.push_back(String(field));
      }
      auto schema = Object({{"type", String("object")},
                            {"properties", std::move(properties)},
                            {"required", std::move(required)},
                            {"additionalProperties", Boolean(false)}});
      definitions.array.push_back(Object(
          {{"type", String("function")},
           {"function", Object({{"name", String("multi_agent_" + name)},
                                {"description", String(std::move(description))},
                                {"parameters", std::move(schema)}})}}));
    };
    add("spawn_agent",
        "Delegate a task to a named child. Returns its canonical target.",
        {"name", "task"});
    add("interrupt_agent",
        "Interrupt an active descendant and its active children.", {"target"});
    add("list_agents", "List agents in this response and their actual states.",
        {});
    add("send_message", "Deliver additional context to a named child.",
        {"target", "message"});
    add("followup_task", "Start a new task for an idle descendant.",
        {"target", "task"});
    add("wait_agent",
        "Wait for a named descendant and obtain its actual answer.",
        {"target"});
    if (with_lifecycle) {
      add("close_subagent",
          "Close the named descendant and interrupt its current task. Its "
          "history remains accessible.",
          {"target"});
      add("resume_subagent",
          "Reopen the named closed descendant. Use followup_task to start new "
          "work.",
          {"target"});
    }
    return definitions;
  };
  static const Json standard = build(false), sessions = build(true);
  return managed ? sessions : standard;
}

Json ResponsesAgentTree::ChildTools(std::string_view owner) const {
  bool active = false, idle = false, closed = false;
  for (const auto& [name, agent] : agents_) {
    if (!IsDescendant(name, owner)) continue;
    if (agent.closed_at)
      closed = true;
    else if (Active(agent))
      active = true;
    else
      idle = true;
  }
  const bool slot = HasSlot();
  auto tools = ModelTools(true);
  std::erase_if(tools.array, [&](const Json& tool) {
    const auto name = tool.Find("function")->GetString("name");
    if (name == "multi_agent_spawn_agent") return !slot;
    if (name == "multi_agent_followup_task") return !slot || !idle;
    if (name == "multi_agent_interrupt_agent") return !active;
    if (name == "multi_agent_close_subagent") return !active && !idle;
    if (name == "multi_agent_resume_subagent") return !closed;
    if (name == "multi_agent_wait_agent") return !active && !idle && !closed;
    return false;
  });
  return tools;
}

std::string_view ResponsesAgentTree::ActionName(std::string_view model_name,
                                                bool managed) {
  constexpr std::string_view prefix = "multi_agent_";
  if (model_name.starts_with(prefix)) {
    for (const auto& tool : ModelTools(managed).array) {
      if (tool.Find("function")->GetString("name") == model_name)
        return model_name.substr(prefix.size());
    }
  }
  return {};
}

bool ResponsesAgentTree::Active(const Agent& agent) {
  return !agent.closed_at &&
         (agent.state == State::kQueued || agent.state == State::kRunning);
}
bool ResponsesAgentTree::HasSlot() const {
  return static_cast<uint64_t>(std::count_if(agents_.begin(), agents_.end(),
                                             [](const auto& entry) {
                                               return Active(entry.second);
                                             })) < capacity_;
}
io::Json ResponsesAgentTree::Describe(const std::string& name,
                                      const Agent& agent) {
  std::string state;
  switch (agent.state) {
    case State::kQueued:
      state = "queued";
      break;
    case State::kRunning:
      state = "running";
      break;
    case State::kCompleted:
      state = "completed";
      break;
    case State::kInterrupted:
      state = "interrupted";
      break;
    case State::kIncomplete:
      state = "incomplete";
      break;
  }
  auto result = Object(
      {{"agent_name", String(name)},
       {"parent", String(agent.parent)},
       {"status", String(agent.closed_at ? "closed" : std::move(state))}});
  if (!agent.closed_at &&
      (agent.state == State::kCompleted || agent.state == State::kIncomplete))
    Set(&result, "output",
        *agent.messages.array[agent.delivered].Find("content"));
  return result;
}
Status ResponsesAgentTree::Act(std::string_view caller, std::string_view action,
                               const Json& arguments, Json* result) {
  *result = {};
  if (caller != "root") {
    const auto owner = agents_.find(std::string(caller));
    if (owner == agents_.end() || owner->second.state != State::kRunning)
      return Status::Fail("Agent caller has no active turn");
  }
  std::string path;
  const auto fields = [&](std::initializer_list<std::string_view> names) {
    return InputFields(arguments, names, &path, "arguments");
  };
  if (action == "list_agents") {
    auto status = fields({});
    if (!status) return status;
    auto list = Array();
    for (const auto& [name, agent] : agents_)
      list.array.push_back(Describe(name, agent));
    *result = Object({{"agents", std::move(list)}});
    return {};
  }
  if (action == "spawn_agent") {
    auto status = fields({"name", "task"});
    if (!status) return status;
    const auto leaf = arguments.GetString("name"),
               task = arguments.GetString("task");
    if (!ValidLeaf(leaf) || task.empty())
      return Status::Fail("Agent requires a name and task");
    if (task.size() > ResponsesOutput::kStorageLimit - stored_text_)
      return Status::Fail("Agent history budget exhausted");
    const auto name = std::string(caller) + "/" + leaf;
    if (agents_.contains(name))
      return Status::Fail("Agent name already exists");
    if (!HasSlot()) return Status::Fail("Concurrent agent limit reached");
    if (agents_.size() >= ResponsesOutput::kMaxItems)
      return Status::Fail("Response item budget exhausted");
    Agent agent;
    agent.parent = caller;
    agent.opened_at = std::time(nullptr);
    agent.messages = Array({Message("user", task)});
    const auto found = agents_.emplace(name, std::move(agent)).first;
    stored_text_ += task.size();
    *result = Describe(found->first, found->second);
    return {};
  }
  const bool send = action == "send_message",
             follow = action == "followup_task";
  if (!send && !follow && action != "interrupt_agent" && action != "wait_agent")
    return Status::Fail("Unknown agent action");
  const auto status = send     ? fields({"target", "message"})
                      : follow ? fields({"target", "task"})
                               : fields({"target"});
  if (!status) return status;
  const auto target = arguments.GetString("target");
  if (send && target == "root" && caller != "root") {
    const auto text = arguments.GetString("message");
    if (text.empty() ||
        text.size() > ResponsesOutput::kStorageLimit - stored_text_ ||
        root_messages_.array.size() >= ResponsesOutput::kMaxItems)
      return Status::Fail("Invalid root message or history budget exhausted");
    if (!root_messages_.IsArray()) root_messages_ = Array();
    const auto notice = Object(
        {{"sender", String(std::string(caller))}, {"message", String(text)}});
    root_messages_.array.push_back(
        Message("user", "[Agent message]\n" + OutputJson(notice)));
    stored_text_ += text.size();
    *result =
        Object({{"target", String(target)}, {"status", String("delivered")}});
    return {};
  }
  const auto found = agents_.find(target);
  if (found == agents_.end()) return Status::Fail("Unknown agent");
  auto& agent = found->second;
  if (agent.closed_at && (send || follow || action == "interrupt_agent"))
    return Status::Fail("Agent is closed");
  if (send || follow) {
    const auto content = arguments.GetString(send ? "message" : "task");
    if (content.empty()) return Status::Fail("Empty agent message or task");
    if (follow && (!IsDescendant(target, caller) || Active(agent)))
      return Status::Fail("Follow-up requires an idle descendant");
    if (follow && !HasSlot())
      return Status::Fail("Concurrent agent limit reached");
    if (agent.messages.array.size() >= ResponsesOutput::kMaxItems - 1 ||
        content.size() > ResponsesOutput::kStorageLimit - stored_text_)
      return Status::Fail("Agent history budget exhausted");
    agent.messages.array.push_back(Message("user", content));
    stored_text_ += content.size();
    if (follow) agent.state = State::kQueued;
  } else if (action == "interrupt_agent") {
    if (!IsDescendant(target, caller))
      return Status::Fail("Interrupt requires a descendant");
    for (auto& [name, member] : agents_)
      if ((name == target || IsDescendant(name, target)) && Active(member))
        member.state = State::kInterrupted;
  } else if (!IsDescendant(target, caller)) {
    return Status::Fail("Wait requires a descendant");
  }
  *result = Describe(found->first, agent);
  return {};
}
int64_t ResponsesAgentTree::OpenedAt(const std::string& name) const {
  const auto found = agents_.find(name);
  return found == agents_.end() ? -1 : found->second.opened_at;
}
Status ResponsesAgentTree::SetClosed(std::string_view caller, bool close,
                                     const Json& args, Json* result,
                                     int64_t legacy_opened_at) {
  std::string bad;
  auto fields = InputFields(args, {"target"}, &bad, "arguments");
  if (!fields) return fields;
  const auto path = args.GetString("target");
  if (!IsDescendant(path, caller))
    return Status::Fail("Lifecycle action requires a descendant");
  if (caller != "root") {
    const auto owner = agents_.find(std::string(caller));
    if (owner == agents_.end() || owner->second.state != State::kRunning ||
        owner->second.closed_at)
      return Status::Fail("Agent caller has no active turn");
  }
  const auto found = agents_.find(path);
  if (found == agents_.end()) return Status::Fail("Unknown agent");
  auto& agent = found->second;
  if (bool(agent.closed_at) == close)
    return Status::Fail(close ? "Agent is already closed"
                              : "Agent is already active");
  if (!agent.opened_at) {
    if (legacy_opened_at <= 0)
      return Status::Fail("Missing original subagent creation time");
    agent.opened_at = legacy_opened_at;
  }
  if (close) {
    for (auto& [name, member] : agents_)
      if ((name == path || IsDescendant(name, path)) && Active(member))
        member.state = State::kInterrupted;
  }
  agent.closed_at =
      close ? std::max<int64_t>(std::time(nullptr), agent.opened_at) : 0;
  *result =
      Object({{"agent_name", String(path)},
              {"parent", String(agent.parent)},
              {"opened_at", Number(agent.opened_at)},
              {"closed_at", agent.closed_at ? Number(agent.closed_at) : Json{}},
              {"status", String(close ? "closed" : "active")}});
  return {};
}

Json ResponsesAgentTree::SaveSession() const {
  auto members = Array();
  for (const auto& [name, agent] : agents_) {
    auto row = Object({{"name", String(name)},
                       {"parent", String(agent.parent)},
                       {"messages", agent.messages},
                       {"delivered", Number(agent.delivered)},
                       {"generation", Number(agent.generation)},
                       {"status", *Describe(name, agent).Find("status")}});
    if (agent.opened_at) Set(&row, "opened_at", Number(agent.opened_at));
    if (agent.closed_at) {
      Set(&row, "closed_at", Number(agent.closed_at));
      Set(&row, "status",
          String(agent.state == State::kCompleted    ? "completed"
                 : agent.state == State::kIncomplete ? "incomplete"
                                                     : "interrupted"));
    }
    members.array.push_back(std::move(row));
  }
  return members;
}

Status ResponsesAgentTree::RestoreSession(const Json& saved) {
  if (!saved.IsArray() || saved.array.size() > ResponsesOutput::kMaxItems)
    return Status::Fail("Invalid stored session agent list");
  decltype(agents_) restored;
  size_t bytes = 0;
  for (const auto& row : saved.array) {
    const auto name = row.GetString("name"), parent = row.GetString("parent");
    const auto* messages = row.GetArray("messages");
    const auto* delivered = row.Find("delivered");
    const auto* generation = row.Find("generation");
    const auto* opened = row.Find("opened_at");
    const auto* closed = row.Find("closed_at");
    if (row.object.size() != 6U + (opened != nullptr) + (closed != nullptr) ||
        (opened && (!IsInputInteger(*opened) || opened->number <= 0)) ||
        (closed && (!IsInputInteger(*closed) || closed->number <= 0 ||
                    (opened && closed->number < opened->number))) ||
        !IsDescendant(name, parent) ||
        !ValidLeaf(std::string_view(name).substr(parent.size() + 1)) ||
        (parent != "root" && !restored.contains(parent)) || !messages ||
        messages->array.empty() ||
        messages->array.size() > ResponsesOutput::kMaxItems || !delivered ||
        !IsInputInteger(*delivered) || delivered->number < 0 ||
        delivered->number > messages->array.size() || !generation ||
        !IsInputInteger(*generation) || generation->number < 0 ||
        generation->number >= 9007199254740991.0)
      return Status::Fail("Invalid stored session agent");
    if (messages->array.front().GetString("role") != "user" ||
        !messages->array.front().Find("content") ||
        !messages->array.front().Find("content")->IsString())
      return Status::Fail("Missing initial session agent task");
    const auto size = OutputJson(*messages).size();
    if (size > ResponsesOutput::kStorageLimit - bytes)
      return Status::Fail("Stored session agents exceed the history budget");
    bytes += size;
    Agent agent;
    agent.parent = parent;
    agent.opened_at = row.GetInt("opened_at");
    agent.closed_at = row.GetInt("closed_at");
    agent.messages = *messages;
    agent.delivered = delivered->AsInt();
    agent.generation = generation->AsInt();
    const auto state = row.GetString("status");
    if (agent.closed_at && state != "completed" && state != "incomplete" &&
        state != "interrupted")
      return Status::Fail("Closed subagent has an active stored task");
    if (state == "completed" || state == "incomplete") {
      if (agent.delivered >= messages->array.size() ||
          messages->array[agent.delivered].GetString("role") != "assistant" ||
          !messages->array[agent.delivered].Find("content") ||
          !messages->array[agent.delivered].Find("content")->IsString())
        return Status::Fail("Missing stored session agent answer");
      agent.state =
          state == "completed" ? State::kCompleted : State::kIncomplete;
    } else if (state == "interrupted" || state == "running" ||
               state == "queued") {
      agent.state = State::kInterrupted;
    } else {
      return Status::Fail("Invalid stored session agent status");
    }
    if (!restored.emplace(name, std::move(agent)).second)
      return Status::Fail("Duplicate stored session agent");
  }
  agents_ = std::move(restored);
  stored_text_ = bytes;
  root_messages_ = {};
  return {};
}

bool ResponsesAgentTree::Claim(const std::string& name, Turn* turn) {
  *turn = {};
  const auto found = agents_.find(name);
  if (found == agents_.end() || found->second.closed_at ||
      found->second.state != State::kQueued)
    return false;
  auto& agent = found->second;
  agent.state = State::kRunning;
  agent.delivered = agent.messages.array.size();
  *turn = {name, ++agent.generation, agent.messages};
  return true;
}
std::string ResponsesAgentTree::FirstQueued() const {
  for (const auto& [name, agent] : agents_)
    if (agent.state == State::kQueued) return name;
  return {};
}
std::vector<std::string> ResponsesAgentTree::QueuedNames() const {
  std::vector<std::string> names;
  for (const auto& [name, agent] : agents_)
    if (agent.state == State::kQueued) names.push_back(name);
  return names;
}
bool ResponsesAgentTree::Queued(const std::string& name) const {
  const auto found = agents_.find(name);
  return found != agents_.end() && found->second.state == State::kQueued;
}
Status ResponsesAgentTree::RecordExchange(Turn* turn, const Json& messages) {
  const auto found = agents_.find(turn->name);
  if (found == agents_.end() || found->second.state != State::kRunning ||
      found->second.generation != turn->generation || !messages.IsArray())
    return Status::Fail("Stale agent exchange");
  auto& agent = found->second;
  const auto bytes = OutputJson(messages).size();
  if (messages.array.size() >=
          ResponsesOutput::kMaxItems - agent.messages.array.size() ||
      bytes > ResponsesOutput::kStorageLimit - stored_text_)
    return Status::Fail("Agent history budget exhausted");
  agent.messages.array.insert(agent.messages.array.begin() + agent.delivered,
                              messages.array.begin(), messages.array.end());
  agent.delivered += messages.array.size();
  turn->messages.array.insert(turn->messages.array.end(),
                              messages.array.begin(), messages.array.end());
  stored_text_ += bytes;
  return {};
}
Status ResponsesAgentTree::Finish(const Turn& turn, std::string answer,
                                  bool completed) {
  const auto found = agents_.find(turn.name);
  if (found == agents_.end() || found->second.state != State::kRunning ||
      found->second.generation != turn.generation)
    return Status::Fail("Stale agent result");
  auto& agent = found->second;
  if (answer.size() > ResponsesOutput::kStorageLimit - stored_text_)
    return Status::Fail("Agent history budget exhausted");
  // Messages delivered during this turn belong after the model's answer.
  stored_text_ += answer.size();
  agent.messages.array.insert(agent.messages.array.begin() + agent.delivered,
                              Message("assistant", std::move(answer)));
  agent.state = completed ? State::kCompleted : State::kIncomplete;
  return {};
}
}  // namespace q4t::server
