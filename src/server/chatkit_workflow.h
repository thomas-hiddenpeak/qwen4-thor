#pragma once

#include "q4t/server/chatkit.h"

namespace q4t::server::chatkit_detail {

// Stored task groups retain the browser workflow. The management API projects
// each public thought into a task with a heading and summary.
const io::Json* ResumableWorkflow(const io::Json& items);
io::Json NewWorkflow(const io::Json& reasoning, const std::string& thread,
                     int64_t created);
io::Json PublicThought(const std::string& text);
io::Json& WorkflowTasks(io::Json& item);
void CloseWorkflow(io::Json& item, int64_t observed_at);
void ManagementWorkflow(io::Json& item);
int64_t WorkflowItemTime(const io::Json* times, const std::string& id);

}  // namespace q4t::server::chatkit_detail
