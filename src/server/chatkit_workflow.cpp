#include "chatkit_workflow.h"

#include <algorithm>

#include "object_protocol.h"

namespace q4t::server::chatkit_detail {
using namespace object_detail;

const io::Json* ResumableWorkflow(const io::Json& items) {
  if (!items.IsArray() || items.array.empty()) return nullptr;
  auto last = items.array.rbegin();
  if (last->GetString("type") == "chatkit.client_tool_call") ++last;
  if (last == items.array.rend() ||
      last->GetString("type") != "chatkit.task_group" ||
      last->GetBool("workflow_closed"))
    return nullptr;
  return &*last;
}

int64_t WorkflowItemTime(const io::Json* times, const std::string& id) {
  return times ? times->GetInt(id) : 0;
}

io::Json NewWorkflow(const io::Json& reasoning, const std::string& thread,
                     int64_t created) {
  return Object({
      {"id", String("wf_" + reasoning.GetString("id"))},
      {"thread_id", String(thread)},
      {"created_at", Number(created)},
      {"object", String("chatkit.thread_item")},
      {"type", String("chatkit.task_group")},
      {"workflow", Object({{"type", String("reasoning")},
                           {"tasks", Array()},
                           {"expanded", Boolean(false)},
                           {"summary", {}}})},
  });
}

io::Json PublicThought(const std::string& text) {
  return Object({{"type", String("thought")},
                 {"status_indicator", String("none")},
                 {"title", {}},
                 {"content", String(text)}});
}

io::Json& WorkflowTasks(io::Json& item) {
  return Field(Field(item, "workflow"), "tasks");
}

void CloseWorkflow(io::Json& item, int64_t observed_at) {
  // Older response journals do not carry per-item observation times.
  if (observed_at > 0) {
    const auto duration =
        std::max<int64_t>(0, observed_at - item.GetInt("created_at"));
    Set(&Field(item, "workflow"), "summary",
        Object({{"duration", Number(duration)}}));
  }
  Set(&item, "workflow_closed", Boolean(true));
}

void ManagementWorkflow(io::Json& item) {
  auto tasks = Array();
  for (const auto& task : WorkflowTasks(item).array) {
    tasks.array.push_back(
        Object({{"type", String("thought")},
                {"heading", {}},
                {"summary", String(task.GetString("content"))}}));
  }
  std::erase_if(item.object, [](const auto& field) {
    return field.first == "workflow" || field.first == "workflow_closed";
  });
  Set(&item, "tasks", std::move(tasks));
}

}  // namespace q4t::server::chatkit_detail
