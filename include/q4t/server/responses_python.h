#pragma once

#include "q4t/server/chat_plan.h"

namespace q4t::server {
inline constexpr char kPythonToolName[] = "q4t_builtin_code_interpreter";
Status ValidatePythonTool(const io::Json& tool, ChatPlanError* error,
                          const std::string& path);
io::Json PythonToolFunction();
}  // namespace q4t::server
