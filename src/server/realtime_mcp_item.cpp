#include "q4t/server/realtime_mcp_item.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
Status Invalid(ChatPlanError* error, std::string message) {
  *error = {"realtime_mcp_call_invalid", "mcp_call", 400};
  return Status::Fail(std::move(message));
}
}  // namespace
Status ProjectRealtimeMcpCall(const Json& native,
                              RealtimeMcpCallProjection* projection,
                              ChatPlanError* error) {
  if (!projection || !error)
    return Status::Fail("Missing Realtime MCP projection owner");
  *error = {};
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(native, 4ULL << 20, &bytes)) {
    *error = {"realtime_mcp_call_capacity", "mcp_call", 413};
    return Status::Fail("MCP call projection exceeds capacity");
  }
  auto status = ValidateResponsesItemShape(native, ResponsesItemShape::kOutput,
                                           error, "mcp_call");
  if (!status) return status;
  if (native.GetString("type") != "mcp_call" ||
      !native.GetString("id").starts_with("mcp_") ||
      !ValidRequestId(native.GetString("id")))
    return Invalid(error, "Invalid native MCP call identity");
  const auto state = native.GetString("status");
  const auto* failure = native.Find("error");
  const auto* output = native.Find("output");
  const bool failed = failure && !failure->IsNull();
  const bool has_output = output && !output->IsNull();
  RealtimeMcpCallProjection next;
  using Phase = RealtimeMcpCallProjection::Phase;
  if (state == "completed") {
    if (failed || !output || !output->IsString())
      return Invalid(error, "Completed native MCP call has no valid result");
    next.phase = Phase::kCompleted;
  } else if (state == "failed") {
    if (!failed)
      return Invalid(error, "Failed native MCP call has no failure evidence");
    next.phase = Phase::kFailed;
  } else if (state == "in_progress" || state == "calling") {
    if (failed || has_output)
      return Invalid(error,
                     "Pending native MCP call contains a terminal result");
  } else {
    return Invalid(error, "Native MCP call has no known execution state");
  }
  next.item = native;
  std::erase_if(next.item.object,
                [](const auto& field) { return field.first == "status"; });
  if (failed) {
    auto mapped = *failure;
    const auto kind = failure->GetString("type");
    if (kind == "mcp_protocol_error") {
      Set(&mapped, "type", String("protocol_error"));
    } else if (kind == "mcp_tool_execution_error") {
      const auto* content = failure->Find("content");
      if (!content) return Invalid(error, "Native tool error has no content");
      mapped = Object(
          {{"type", String("tool_execution_error")},
           {"message", String(content->IsString() ? content->str
                                                  : OutputJson(*content))}});
    } else if (kind != "http_error") {
      return Invalid(error, "Unknown native MCP error discriminator");
    }
    Set(&next.item, "error", std::move(mapped));
  }
  status =
      ValidateRealtimeShape(next.item, RealtimeShape::kItem, error, "mcp_call");
  if (!status) return status;
  // Structured error-to-string projection can allocate a larger buffer.
  if (!MeasureRealtimeStorage(next.item, 4ULL << 20, &bytes)) {
    *error = {"realtime_mcp_call_capacity", "mcp_call", 413};
    return Status::Fail("Projected MCP call exceeds capacity");
  }
  *projection = std::move(next);
  return {};
}
}  // namespace q4t::server
