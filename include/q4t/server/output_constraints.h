#pragma once

#include <algorithm>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

struct OutputConstraints {
  enum class ToolChoice { kAuto, kNone, kRequired, kNamed };
  ToolChoice choice = ToolChoice::kAuto;
  std::string named_tool;
  std::string named_type = "function";
  bool restrict_tools = false;
  std::vector<std::string> allowed_tools;
  bool parallel = true;
  bool active = false;
  bool json_output = false;
  // Empty for an unconstrained request. Includes the prefilling convention
  // for the native template's initial reasoning block.
  std::string structural_tag;

  bool AllowsTool(std::string_view name) const {
    return choice != ToolChoice::kNone &&
           (choice != ToolChoice::kNamed || name == named_tool) &&
           (!restrict_tools ||
            std::find(allowed_tools.begin(), allowed_tools.end(), name) !=
                allowed_tools.end());
  }
};

// Pure request/schema planning. Backend availability is checked separately,
// before tokenization, slot acquisition and any model work.
Status PlanOutputConstraints(const io::Json& request, OutputConstraints* out,
                             std::string* error_param = nullptr);
io::Json ConstraintTemplateRequest(const io::Json& request,
                                   const OutputConstraints& plan);
Status ValidateConstrainedArguments(const io::Json& function,
                                    std::string_view arguments);
Status ValidateConstrainedContent(const io::Json& request,
                                  std::string_view content);

}  // namespace q4t::server
