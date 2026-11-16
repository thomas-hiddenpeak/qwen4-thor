#include "q4t/server/responses_output.h"

#include <algorithm>
#include <limits>

#include "object_protocol.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/mcp_content.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_item_shape.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;
namespace {
bool Same(const io::Json* left, const io::Json* right) {
  if (!left || !right) return !left && !right;
  std::string a, b;
  return InputValueIdentity(*left, &a) && InputValueIdentity(*right, &b) &&
         a == b;
}
bool Arguments(std::string_view text) {
  io::Json arguments;
  return text.size() <= ChatResponseParser::kMaxToolBytes &&
         io::ParseJson(std::string(text), &arguments, true,
                       kSchemaJsonLimits) &&
         arguments.IsObject();
}
bool NativeFailureOutput(const io::Json& item) {
  const auto* error = item.Find("error");
  io::Json native;
  return error && error->GetString("type") == "mcp_tool_execution_error" &&
         item.GetString("output").size() <= (4U << 20) &&
         object_detail::Parse(item.GetString("output"), &native) &&
         ValidateMcpToolResult(native) && native.GetBool("isError") &&
         Same(error->Find("content"), native.Find("content"));
}
}  // namespace

Status ResponsesOutput::StartMcpItem(const io::Json& item, size_t* index,
                                     std::string* wire) {
  wire->clear();
  *index = kMaxItems;
  const auto type = item.GetString("type");
  if (!started_ || terminal_ ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems ||
      (type != "mcp_list_tools" && type != "mcp_approval_request" &&
       type != "mcp_call") ||
      !ValidApplicationId(item.GetString("id")) ||
      item.GetString("server_label").empty() ||
      std::any_of(Items().array.begin(), Items().array.end(),
                  [&](const auto& old) {
                    return old.GetString("id") == item.GetString("id");
                  }))
    return Status::Fail("Invalid MCP item start");
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "mcp");
  if (!status) return status;
  if (type == "mcp_call" &&
      (item.GetString("status") != "in_progress" ||
       (item.Has("error") && !item.Find("error")->IsNull()) ||
       (item.Has("output") && !item.Find("output")->IsNull()) ||
       item.GetString("arguments").size() > ChatResponseParser::kMaxToolBytes))
    return Status::Fail("MCP call must start before execution");
  if (type == "mcp_approval_request" && !Arguments(item.GetString("arguments")))
    return Status::Fail("MCP approval requires complete object arguments");
  if (type == "mcp_list_tools" &&
      (!item.GetArray("tools")->array.empty() ||
       (item.Has("error") && !item.Find("error")->IsNull())))
    return Status::Fail("MCP catalog must start before listing");
  status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  *index = Items().array.size();
  Items().array.push_back(item);
  call_indices_.push_back(std::numeric_limits<size_t>::max());
  Event("response.output_item.added",
        Object({{"output_index", Number(*index)}, {"item", item}}), wire);
  if (type == "mcp_approval_request") {
    Event("response.output_item.done",
          Object({{"output_index", Number(*index)}, {"item", item}}), wire);
    closed_items_.insert(*index);
  } else {
    Event(type == "mcp_list_tools" ? "response.mcp_list_tools.in_progress"
                                   : "response.mcp_call.in_progress",
          Location(*index), wire);
  }
  return {};
}

Status ResponsesOutput::AppendMcpArguments(size_t index, std::string_view delta,
                                           std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      closed_items_.contains(index) ||
      mcp_arguments_complete_.contains(index) ||
      Items().array[index].GetString("type") != "mcp_call" ||
      Items().array[index].GetString("status") != "in_progress")
    return Status::Fail("Invalid MCP argument delta");
  auto& arguments = Field(Items().array[index], "arguments").str;
  if (delta.size() > ChatResponseParser::kMaxToolBytes - arguments.size())
    return Status::Fail("MCP arguments exceed capacity");
  auto status = Charge(delta.size());
  if (!status) return status;
  arguments.append(delta);
  auto event = Location(index);
  Set(&event, "delta", String(std::string(delta)));
  Event("response.mcp_call_arguments.delta", std::move(event), wire);
  return {};
}

Status ResponsesOutput::CompleteMcpArguments(size_t index, std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      closed_items_.contains(index) ||
      mcp_arguments_complete_.contains(index) ||
      Items().array[index].GetString("type") != "mcp_call" ||
      Items().array[index].GetString("status") != "in_progress" ||
      !Arguments(Items().array[index].GetString("arguments")))
    return Status::Fail("MCP arguments are incomplete or already final");
  mcp_arguments_complete_.insert(index);
  auto event = Location(index);
  Set(&event, "arguments", *Items().array[index].Find("arguments"));
  Event("response.mcp_call_arguments.done", std::move(event), wire);
  return {};
}

Status ResponsesOutput::CallingMcp(size_t index) {
  if (!started_ || terminal_ || index >= Items().array.size() ||
      closed_items_.contains(index) ||
      !mcp_arguments_complete_.contains(index) ||
      Items().array[index].GetString("type") != "mcp_call" ||
      Items().array[index].GetString("status") != "in_progress")
    return Status::Fail("MCP call has no complete arguments");
  Set(&Items().array[index], "status", String("calling"));
  return {};
}

Status ResponsesOutput::CompleteMcpItem(size_t index, const io::Json& item,
                                        std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      closed_items_.contains(index))
    return Status::Fail("MCP item is not active");
  const auto& old = Items().array[index];
  const auto type = old.GetString("type");
  if (type != "mcp_call" && type != "mcp_list_tools")
    return Status::Fail("Invalid MCP completion type");
  for (const auto* key : {"id", "type", "server_label"})
    if (!Same(old.Find(key), item.Find(key)))
      return Status::Fail("MCP item identity changed");
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "mcp");
  if (!status) return status;
  bool failed = false;
  if (type == "mcp_call") {
    for (const auto* key : {"name", "arguments", "approval_request_id"})
      if (!Same(old.Find(key), item.Find(key)))
        return Status::Fail("MCP call identity or arguments changed");
    if (!mcp_arguments_complete_.contains(index) ||
        (item.GetString("status") != "completed" &&
         item.GetString("status") != "failed"))
      return Status::Fail("MCP completion requires a terminal invocation");
    failed = item.GetString("status") == "failed";
    if ((!failed && old.GetString("status") != "calling") ||
        (failed && (!item.Find("error") || item.Find("error")->IsNull())) ||
        (!failed && item.Has("error") && !item.Find("error")->IsNull()) ||
        (!failed &&
         (!item.Find("output") || !item.Find("output")->IsString())) ||
        (failed && item.Has("output") && !item.Find("output")->IsNull() &&
         (old.GetString("status") != "calling" || !NativeFailureOutput(item))))
      return Status::Fail("MCP result is inconsistent with invocation");
  } else {
    failed = item.Has("error") && !item.Find("error")->IsNull();
    if (failed && !item.GetArray("tools")->array.empty())
      return Status::Fail("Failed MCP listing cannot expose tools");
  }
  status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  Items().array[index] = item;
  Event(type == "mcp_call" ? failed ? "response.mcp_call.failed"
                                    : "response.mcp_call.completed"
        : failed           ? "response.mcp_list_tools.failed"
                           : "response.mcp_list_tools.completed",
        Location(index), wire);
  Event("response.output_item.done",
        Object({{"output_index", Number(index)}, {"item", item}}), wire);
  closed_items_.insert(index);
  return {};
}

bool ResponsesOutput::CloseInterruptedMcp(size_t index, std::string* wire) {
  auto& item = Items().array[index];
  const auto type = item.GetString("type");
  if (type != "mcp_call" && type != "mcp_list_tools") return false;
  if (type == "mcp_call") {
    Set(&item, "status", String("incomplete"));
  } else {
    // McpListTools has no status field in the public SDK contract.
    Set(&item, "error", String("MCP listing interrupted"));
    Event("response.mcp_list_tools.failed", Location(index), wire);
  }
  Event("response.output_item.done",
        Object({{"output_index", Number(index)}, {"item", item}}), wire);
  closed_items_.insert(index);
  return true;
}
}  // namespace q4t::server
