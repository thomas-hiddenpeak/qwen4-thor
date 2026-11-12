#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/tool_result_schema.h"

namespace q4t::server {
using namespace object_detail;

Status ResponsesExecution::BindActiveTools(const io::Json& active,
                                           ChatPlanError* error) {
  const auto request = Object({{"tools", active}});
  auto status =
      PlanResponsesFileSearchBindings(request, &file_search_bindings_, error);
  if (!status) return status;
  std::vector<ResponsesToolIdentity> identities;
  status = PlanResponsesToolIdentities(request, &identities, error);
  if (!status) return status;
  has_async_tools_ = std::any_of(identities.begin(), identities.end(),
                                 [](const auto& tool) { return tool.async; });
  program_tools_ = active;
  program_enabled_ = std::any_of(
      active.array.begin(), active.array.end(), [](const auto& tool) {
        return tool.GetString("type") == "programmatic_tool_calling";
      });
  tool_search_catalog_.search = {};
  for (const auto& tool : active.array)
    if (tool.GetString("type") == "tool_search") {
      tool_search_catalog_.search = tool;
      if (!tool_search_catalog_.search.Has("execution"))
        Set(&tool_search_catalog_.search, "execution", String("server"));
    }
  return {};
}

Status ResponsesExecution::AppendToolSearch(const ResponseDelta& delta,
                                            bool* handled, std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  auto found = tool_search_pending_.find(delta.call.index);
  if (delta.tool_start) {
    if (delta.call.name != kResponsesToolSearchModelName) {
      if (found != tool_search_pending_.end())
        return Status::Fail("Tool search index changed identity");
      return {};
    }
    *handled = true;
    wire->clear();
    if (!HasToolSearch() || found != tool_search_pending_.end() ||
        delta.call.custom || delta.call.id.empty())
      return Status::Fail("Invalid native tool search identity");
    if (delta.call.arguments.size() > ChatResponseParser::kMaxToolBytes ||
        delta.call.arguments.size() >
            ResponsesOutput::kStorageLimit - tool_search_argument_bytes_)
      return Status::Fail("Tool search arguments exceed memory budget");
    NativeSearchCall pending;
    pending.call = delta.call;
    const bool server =
        tool_search_catalog_.search.GetString("execution") == "server";
    auto status = output_.StartToolSearch(delta.call, server,
                                          &pending.output_index, wire);
    if (!status) return status;
    tool_search_argument_bytes_ += delta.call.arguments.size();
    tool_search_pending_.emplace(delta.call.index, std::move(pending));
    return RecordOutput(status, wire);
  }
  if (found == tool_search_pending_.end()) return {};
  *handled = true;
  wire->clear();
  auto& pending = found->second.call;
  if (delta.call.custom ||
      (!delta.call.id.empty() && delta.call.id != pending.id) ||
      (!delta.call.name.empty() && delta.call.name != pending.name) ||
      delta.call.arguments.size() >
          ChatResponseParser::kMaxToolBytes - pending.arguments.size() ||
      delta.call.arguments.size() >
          ResponsesOutput::kStorageLimit - tool_search_argument_bytes_)
    return Status::Fail("Tool search call changed or exceeded budget");
  pending.arguments += delta.call.arguments;
  tool_search_argument_bytes_ += delta.call.arguments.size();
  return {};
}

ObjectReply ResponsesExecution::RunToolSearches(
    size_t completed_calls, bool* client_wait,
    const std::function<bool(const std::string&)>& publish) {
  *client_wait = false;
  if (!begun_ || plan_.finished || commit_unknown_ || !PendingToolSearch())
    return Error(500, "response_state_invalid", "No executable tool search");
  if (access_.cancelled && access_.cancelled())
    return Error(499, "request_cancelled", "Tool discovery cancelled");
  if (std::chrono::steady_clock::now() >= access_.deadline)
    return Error(408, "request_deadline_exceeded", "Tool discovery expired");
  auto arguments = Array(), checks = Array();
  const auto schema = ResponsesToolSearchArgumentsSchema(tool_search_catalog_);
  for (const auto& [index, pending] : tool_search_pending_) {
    if (pending.dispatched)
      return Error(409, "tool_search_already_dispatched",
                   "A dispatched search cannot be replayed");
    if (index >= completed_calls)
      return Error(502, "tool_search_incomplete",
                   "Unvalidated tool arguments must not execute");
    io::Json value;
    auto status =
        DecodeResponsesToolSearchArguments(pending.call.arguments, &value);
    if (!status)
      return Error(502, "tool_search_arguments_invalid", status.message());
    checks.array.push_back(
        Object({{"schema", schema},
                {"output", String(OutputJson(value))},
                {"param", String("tool_search.arguments")}}));
    arguments.array.push_back(std::move(value));
  }
  // Advisory/open schemas still validate actual values in the bounded worker;
  // they never acquire a false strict-decoding guarantee.
  auto validated = ValidateToolResultSchemas(
      checks, {access_.scope, access_.deadline, access_.cancelled});
  if (!validated.ok()) {
    if (validated.http_status == 400)
      return Error(502, "tool_search_arguments_invalid",
                   "Generated search arguments do not satisfy their schema");
    return validated;
  }
  const auto limit = static_cast<uint64_t>(
      envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  const size_t executing =
      std::min<uint64_t>(arguments.array.size(),
                         limit - std::min<uint64_t>(limit, native_tool_calls_));
  if (executing > kMaxNativeToolCalls - native_tool_calls_)
    return Error(503, "tool_search_resource_exhausted",
                 "Native tool execution work budget exhausted");
  const bool server =
      tool_search_catalog_.search.GetString("execution") == "server";
  size_t current = 0;
  for (auto& [index, pending] : tool_search_pending_) {
    (void)index;
    pending.dispatched = true;
    const bool skipped = current >= executing;
    io::Json definitions;
    ChatPlanError error;
    if (!skipped && server) {
      auto status = DiscoverResponsesTools(
          tool_search_catalog_, arguments.array[current], &definitions, &error);
      if (!status)
        return Error(error.http_status, error.code, status.message());
    }
    auto candidate = output_;
    std::string wire, part;
    auto status = candidate.CompleteToolSearchCall(
        pending.output_index, arguments.array[current], skipped, &wire);
    if (status && !skipped && server) {
      status = candidate.CompleteToolSearchOutput(pending.output_index,
                                                  definitions, &part);
      wire += part;
    }
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    if (!skipped) {
      ++native_tool_calls_;
      *client_wait |= !server;
    }
    output_ = std::move(candidate);
    status = RecordOutput({}, &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    // The pure server lookup and its matching result are one checkpoint;
    // clients see no completed dispatch before its durable receipt.
    auto saved = Checkpoint();
    if (!saved.ok()) return saved;
    if (publish && !publish(wire))
      return Error(499, "request_cancelled",
                   "Tool output subscriber cancelled");
    ++current;
  }
  tool_search_pending_.clear();
  tool_search_argument_bytes_ = 0;
  return {};
}
}  // namespace q4t::server
