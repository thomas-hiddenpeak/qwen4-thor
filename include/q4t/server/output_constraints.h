#pragma once

#include <string>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

struct OutputConstraints {
  enum class ToolChoice { kAuto, kNone, kRequired, kNamed };
  ToolChoice choice = ToolChoice::kAuto;
  std::string named_tool;
  bool parallel = true;
  bool active = false;
  bool json_output = false;
  // Empty for an unconstrained request. Includes the prefilling convention
  // for the native template's initial reasoning block.
  std::string structural_tag;
};

// Pure request/schema planning. Backend availability is checked separately,
// before tokenization, slot acquisition and any model work.
Status PlanOutputConstraints(const io::Json& request, OutputConstraints* out);
io::Json ConstraintTemplateRequest(const io::Json& request,
                                   const OutputConstraints& plan);
Status ValidateConstrainedArguments(const io::Json& function,
                                    std::string_view arguments);
Status ValidateConstrainedContent(const io::Json& request,
                                  std::string_view content);

}  // namespace q4t::server
