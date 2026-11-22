#include "q4t/server/live_responses_turn.h"

#include <algorithm>
#include <set>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kItems = 256, kBytes = 8ULL << 20;
ObjectReply Invalid() {
  return Error(400, "invalid_tool_output",
               "Tool result does not match a completed call");
}
ObjectReply Broken() {
  return Error(503, "live_response_unconfirmed",
               "Native Responses event ownership is unconfirmed");
}
ObjectReply Full() {
  return Error(429, "live_response_capacity",
               "Delegated response capacity exhausted");
}
std::string Expected(const Json& item) {
  const auto type = item.GetString("type");
  if (type == "mcp_approval_request") return "mcp_approval_response";
  if (type == "tool_search_call")
    return item.GetString("execution") == "client" ? "tool_search_output" : "";
  if (type == "shell_call") {
    const auto* env = item.Find("environment");
    if (!env || env->GetString("type") != "local") return {};
  } else if (type != "function_call" && type != "custom_tool_call" &&
             type != "computer_call" && type != "local_shell_call" &&
             type != "apply_patch_call") {
    return {};
  }
  return type + "_output";
}
std::string Owner(const Json& item, bool result) {
  const auto type = item.GetString("type");
  if (type == "mcp_approval_request" || type == "local_shell_call" ||
      type == "local_shell_call_output")
    return item.GetString("id");
  if (result && type == "mcp_approval_response")
    return item.GetString("approval_request_id");
  return item.GetString("call_id");
}
bool SameCaller(const Json& call, const Json& result) {
  const auto* a = call.Find("caller");
  const auto* b = result.Find("caller");
  const auto kind = [](const Json* caller) {
    return caller && !caller->IsNull() ? caller->GetString("type") : "direct";
  };
  return kind(a) == kind(b) &&
         (kind(a) != "program" ||
          a->GetString("caller_id") == b->GetString("caller_id"));
}
bool Checks(const Json& call, const Json& result) {
  const auto* pending = call.GetArray("pending_safety_checks");
  const auto* acknowledged = result.GetArray("acknowledged_safety_checks");
  if ((!pending ? 0 : pending->array.size()) !=
      (!acknowledged ? 0 : acknowledged->array.size()))
    return false;
  std::set<std::string> ids;
  if (pending)
    for (const auto& item : pending->array)
      if (item.GetString("id").empty() ||
          !ids.insert(item.GetString("id")).second)
        return false;
  if (acknowledged)
    for (const auto& item : acknowledged->array)
      if (!ids.erase(item.GetString("id"))) return false;
  return ids.empty();
}
}  // namespace
LiveResponsesTurn::LiveResponsesTurn(std::string delegation,
                                     std::string client_event,
                                     LiveEventSink emit)
    : delegation_(std::move(delegation)),
      client_event_(std::move(client_event)),
      emit_(std::move(emit)) {
  failed_ =
      !ValidApplicationId(delegation_) || client_event_.size() > 256 || !emit_;
}
ObjectReply LiveResponsesTurn::Emit(Json event, bool correlate) {
  if (correlate && !client_event_.empty())
    Set(&event, "client_event_id", String(client_event_));
  try {
    auto reply = emit_(std::move(event));
    if (reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
        reply.http_status < 300)
      return {};
    if (reply.ok()) reply = Broken();
    reply.commit_unknown = true;
    Abort();
    return reply;
  } catch (...) {
    Abort();
    auto reply = Broken();
    reply.commit_unknown = true;
    return reply;
  }
}
ObjectReply LiveResponsesTurn::Register(const Json& item, size_t index) {
  if (index >= kItems || items_.contains(index)) return Broken();
  size_t charge = 0;
  if (!MeasureRealtimeStorage(item, kBytes - bytes_, &charge)) return Full();
  ChatPlanError error;
  if (!ValidateResponsesItemShape(item, ResponsesItemShape::kOutput, &error,
                                  "item"))
    return Broken();
  const auto expected = Expected(item);
  if (!expected.empty() &&
      item.GetString("status", "completed") == "completed") {
    const auto id = Owner(item, false);
    if (!ValidApplicationId(id) || calls_.contains(id)) return Broken();
    calls_.emplace(id, Call{expected, index, {}});
    if (continuation_) {
      auto error =
          Object({{"type", String("invalid_request_error")},
                  {"code", String(Missing().code)},
                  {"message", String("Another tool call needs a result and a "
                                     "new response.create")}});
      auto event = Object({{"type", String("error")}});
      if (!continuation_->empty()) {
        Set(&error, "client_event_id", String(*continuation_));
        Set(&event, "client_event_id", String(*continuation_));
      }
      Set(&event, "error", std::move(error));
      continuation_.reset();
      auto receipt = Emit(std::move(event), false);
      if (!receipt.ok()) return receipt;
    }
  }
  items_.emplace(index, item);
  bytes_ += charge;
  return {};
}
ObjectReply LiveResponsesTurn::Event(const Json& native, uint64_t offset_ms) {
  if (failed_ || finished_ || !terminal_.empty()) return Broken();
  const auto type = native.GetString("type");
  auto projected = native;
  if (auto* response =
          projected.Has("response") ? &Field(projected, "response") : nullptr) {
    const auto id = response->GetString("id");
    if (!ValidApplicationId(id) ||
        (!response_id_.empty() && response_id_ != id)) {
      Abort();
      return Broken();
    }
    if (type == "response.created") {
      if (!response_id_.empty() || offset_ms > 9007199254740991ULL) {
        Abort();
        return Broken();
      }
      response_id_ = id;
      auto created =
          Object({{"type", String("session.delegation.created")},
                  {"offset_ms", Number(static_cast<int64_t>(offset_ms))},
                  {"delegation", Object({{"type", String("delegation")},
                                         {"id", String(delegation_)},
                                         {"target", String("responses")},
                                         {"response_id", String(id)}})}});
      auto reply = Emit(std::move(created));
      if (!reply.ok()) return reply;
    }
    Set(response, "output", Array());
    Set(response, "tools", Array());
    Set(response, "instructions", Json{});
    std::erase_if(response->object,
                  [](const auto& field) { return field.first == "input"; });
  }
  if (response_id_.empty() && type != "error") {
    Abort();
    return Broken();
  }
  if (type == "response.output_item.done") {
    const auto* item = native.Find("item");
    const auto* index = native.Find("output_index");
    if (!item || !index || !IsInputInteger(*index) || index->number < 0 ||
        index->number >= kItems) {
      Abort();
      return Broken();
    }
    auto reply = Register(*item, static_cast<size_t>(index->number));
    if (!reply.ok()) {
      Abort();
      return reply;
    }
  }
  if (type == "response.completed" || type == "response.incomplete" ||
      type == "response.failed" || type == "error")
    terminal_ = type;
  if (!terminal_.empty() && terminal_ != "response.completed") Abort();
  return Emit(Object(
      {{"type", String("response.event")},
       {"delegation_id", response_id_.empty() ? Json{} : String(delegation_)},
       {"event", std::move(projected)}}));
}
ObjectReply LiveResponsesTurn::Result(const Json& item) {
  const auto it = calls_.find(Owner(item, true));
  if (failed_ || consumed_ || it == calls_.end() ||
      !it->second.result.IsNull() ||
      it->second.expected != item.GetString("type"))
    return Invalid();
  size_t charge = 0;
  if (!MeasureRealtimeStorage(item, kBytes - bytes_, &charge)) return Full();
  ChatPlanError error;
  if (!ValidateResponsesItemShape(item, ResponsesItemShape::kInput, &error,
                                  "item"))
    return Invalid();
  const auto& call = items_.at(it->second.index);
  if (!SameCaller(call, item) || !Checks(call, item) ||
      (it->second.expected == "tool_search_output" &&
       item.GetString("execution") != "client") ||
      (item.Has("status") && !item.Find("status")->IsNull() &&
       item.GetString("status") != "completed"))
    return Invalid();
  it->second.result = item;
  bytes_ += charge;
  return {};
}
bool LiveResponsesTurn::WaitingForResults() const {
  return std::ranges::any_of(
      calls_, [](const auto& entry) { return entry.second.result.IsNull(); });
}
ObjectReply LiveResponsesTurn::Missing() const {
  const bool functions = std::ranges::all_of(calls_, [](const auto& entry) {
    return !entry.second.result.IsNull() ||
           entry.second.expected == "function_call_output";
  });
  return Error(
      400,
      functions ? "function_call_outputs_required" : "tool_outputs_required",
      "Submit results for every pending tool call");
}
ObjectReply LiveResponsesTurn::RequestContinuation(std::string event) {
  if (failed_ || consumed_ || continuation_ || event.size() > 256 ||
      (!finished_ && calls_.empty()))
    return Error(409, "response_in_progress",
                 "Response continuation is unavailable");
  if (WaitingForResults()) return Missing();
  continuation_ = std::move(event);
  return {};
}
ObjectReply LiveResponsesTurn::Finish(const Json& response) {
  if (failed_ || finished_ || terminal_ != "response.completed" ||
      response.GetString("id") != response_id_ ||
      response.GetString("status") != "completed") {
    Abort();
    return Broken();
  }
  const auto* output = response.GetArray("output");
  if (!output || output->array.size() != items_.size()) {
    Abort();
    return Broken();
  }
  for (size_t i = 0; i < output->array.size(); ++i) {
    const auto it = items_.find(i);
    if (it == items_.end() ||
        OutputJson(it->second) != OutputJson(output->array[i])) {
      Abort();
      return Broken();
    }
  }
  finished_ = true;
  return {};
}
void LiveResponsesTurn::Abort() noexcept {
  failed_ = true;
  continuation_.reset();
}
std::optional<std::string> LiveResponsesTurn::TakeContinuation() {
  if (!finished_ || failed_ || consumed_ || !continuation_ ||
      WaitingForResults())
    return {};
  consumed_ = true;
  return std::exchange(continuation_, std::nullopt);
}
ObjectReply LiveResponsesTurn::AppendHistory(Json* input) const {
  if (!input || !input->IsArray() || failed_ || !finished_ ||
      WaitingForResults())
    return Broken();
  if (input->array.size() + items_.size() + calls_.size() > kItems)
    return Full();
  size_t existing = 0;
  if (!MeasureRealtimeStorage(*input, kBytes - bytes_, &existing))
    return Full();
  auto next = *input;
  for (const auto& [index, item] : items_) next.array.push_back(item);
  for (const auto& [index, item] : items_) {
    if (const auto it = calls_.find(Owner(item, false));
        it != calls_.end() && it->second.index == index)
      next.array.push_back(it->second.result);
  }
  if (!MeasureRealtimeStorage(next, kBytes, &existing)) return Full();
  *input = std::move(next);
  return {};
}
}  // namespace q4t::server
