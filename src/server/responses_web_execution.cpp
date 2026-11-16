#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;
Status ResponsesExecution::AppendWebSearch(const ResponseDelta& delta,
                                           bool* handled, std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  auto found = web_pending_.find(delta.call.index);
  if (delta.tool_start) {
    const auto binding = std::find_if(
        web_bindings_.begin(), web_bindings_.end(),
        [&](const auto& value) { return value.model_name == delta.call.name; });
    if (binding == web_bindings_.end()) {
      if (found != web_pending_.end())
        return Status::Fail("Web call changed identity");
      return {};
    }
    *handled = true;
    wire->clear();
    if (found != web_pending_.end() || delta.call.custom ||
        delta.call.id.empty() ||
        delta.call.arguments.size() > ChatResponseParser::kMaxToolBytes ||
        delta.call.arguments.size() >
            ResponsesOutput::kStorageLimit - web_argument_bytes_)
      return Status::Fail("Invalid web call identity or argument capacity");
    NativeFileCall pending;
    pending.binding = binding - web_bindings_.begin();
    pending.call = delta.call;
    pending.output_index = ResponsesOutput::kMaxItems;
    web_argument_bytes_ += delta.call.arguments.size();
    web_pending_.emplace(delta.call.index, std::move(pending));
    return {};
  }
  if (found == web_pending_.end()) return {};
  *handled = true;
  wire->clear();
  auto& call = found->second.call;
  if (delta.call.custom ||
      (!delta.call.id.empty() && delta.call.id != call.id) ||
      (!delta.call.name.empty() && delta.call.name != call.name) ||
      delta.call.arguments.size() >
          ChatResponseParser::kMaxToolBytes - call.arguments.size() ||
      delta.call.arguments.size() >
          ResponsesOutput::kStorageLimit - web_argument_bytes_)
    return Status::Fail(
        "Web call changed identity or exceeded argument capacity");
  call.arguments += delta.call.arguments;
  web_argument_bytes_ += delta.call.arguments.size();
  return {};
}

ObjectReply ResponsesExecution::RunWebSearches(
    size_t completed_calls,
    const std::function<bool(const std::string&)>& publish) {
  if (!begun_ || plan_.finished || commit_unknown_ || !PendingWebSearch())
    return Error(500, "response_state_invalid", "No executable web calls");
  std::vector<WebAction> actions;
  for (const auto& [index, pending] : web_pending_) {
    if (pending.dispatched)
      return Error(409, "web_search_already_dispatched",
                   "A web call cannot be replayed");
    if (index >= completed_calls)
      return Error(502, "web_search_incomplete",
                   "Unvalidated web arguments cannot execute");
    Json arguments;
    WebAction action;
    auto status = io::ParseJson(pending.call.arguments, &arguments, true,
                                kSchemaJsonLimits);
    if (status) status = PlanResponsesWebAction(arguments, &action);
    if (!status)
      return Error(502, "web_search_arguments_invalid", status.message());
    actions.push_back(std::move(action));
  }
  const auto limit = static_cast<uint64_t>(
      envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  const size_t executing = std::min<uint64_t>(
      actions.size(), limit - std::min<uint64_t>(limit, native_tool_calls_));
  if (executing > kMaxNativeToolCalls - native_tool_calls_)
    return Error(503, "web_search_resource_exhausted",
                 "Native tool budget exhausted");
  const auto emit = [&](Status status, std::string* wire) -> ObjectReply {
    status = RecordOutput(status, wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    if (publish && !publish(*wire))
      return Error(499, "request_cancelled", "Web subscriber cancelled");
    return {};
  };
  size_t current = 0;
  for (auto& [index, pending] : web_pending_) {
    (void)index;
    pending.dispatched = true;
    std::string wire;
    auto result =
        emit(output_.StartWebSearch(ResponsesWebAction(actions[current]),
                                    &pending.output_index, &wire),
             &wire);
    if (!result.ok()) return result;
    if (current >= executing) {
      result =
          emit(output_.EndWebSearch(pending.output_index, false, &wire), &wire);
      if (!result.ok()) return result;
      ++current;
      continue;
    }
    ++native_tool_calls_;
    result = emit(output_.SearchWeb(pending.output_index, &wire), &wire);
    if (!result.ok()) return result;
    WebExecutionResult acquired;
    if (!web_policy_)
      result = Error(503, "web_search_not_configured",
                     "No web search source configured");
    else
      result = ExecuteWebAction(
          objects_, {access_.scope, access_.deadline, access_.cancelled},
          *web_policy_, web_bindings_[pending.binding].options,
          actions[current], web_sources_, &web_budget_, &acquired);
    if (!result.ok()) {
      const auto failed =
          emit(output_.EndWebSearch(pending.output_index, true, &wire), &wire);
      return failed.ok() ? result : failed;
    }
    auto known = web_sources_;
    for (auto& page : acquired.sources) {
      const auto prior = std::find_if(
          known.begin(), known.end(),
          [&](const auto& value) { return value.url == page.url; });
      if (prior == known.end())
        known.push_back(page);
      else
        *prior = page;
    }
    size_t retained = 0;
    for (const auto& page : known) {
      retained += page.text.size() + page.url.size() + page.title.size();
      for (const auto& link : page.links) retained += link.size();
      for (const auto& image : page.images)
        retained += image.url.size() + image.caption.size();
    }
    if (known.size() > 256 || retained > (8U << 20))
      return Error(413, "web_context_capacity",
                   "Request web working set exceeded");
    web_sources_ = std::move(known);
    auto status = output_.CompleteWebSearch(
        pending.output_index,
        ResponsesWebAction(actions[current], acquired.sources), &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    result = BindWebContext(
        output_.Snapshot().GetArray("output")->array[pending.output_index],
        acquired, &plan_.web_content);
    if (!result.ok()) return result;
    envelope_.web_content = plan_.web_content;
    result = emit({}, &wire);
    if (!result.ok()) return result;
    ++current;
  }
  web_pending_.clear();
  web_argument_bytes_ = 0;
  return {};
}

Status ResponsesExecution::CiteWebSearchSources(std::string* wire) {
  if (!begun_ || plan_.finished || commit_unknown_) {
    wire->clear();
    return Status::Fail("Response cannot accept web citations");
  }
  return RecordOutput(output_.CiteWebSearchSources(plan_.web_content, wire),
                      wire);
}
}  // namespace q4t::server
