#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;
namespace {
ObjectReply PlanError(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
Json Queries(const VectorSearchPlan& plan) {
  auto queries = Array();
  for (const auto& text : plan.queries) queries.array.push_back(String(text));
  return queries;
}
}  // namespace

Status ResponsesExecution::AppendFileSearch(const ResponseDelta& delta,
                                            bool* handled, std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  auto found = file_search_pending_.find(delta.call.index);
  if (delta.tool_start) {
    const auto binding = std::find_if(
        file_search_bindings_.begin(), file_search_bindings_.end(),
        [&](const auto& value) { return value.model_name == delta.call.name; });
    if (binding == file_search_bindings_.end()) {
      if (found != file_search_pending_.end())
        return Status::Fail("Native file search index changed identity");
      return {};
    }
    *handled = true;
    wire->clear();
    if (found != file_search_pending_.end() || delta.call.custom ||
        delta.call.id.empty())
      return Status::Fail("Invalid native file search call identity");
    if (delta.call.arguments.size() > ChatResponseParser::kMaxToolBytes ||
        delta.call.arguments.size() >
            ResponsesOutput::kStorageLimit - file_search_argument_bytes_)
      return Status::Fail("Native file search arguments exceed memory budget");
    NativeFileCall pending;
    pending.binding = binding - file_search_bindings_.begin();
    pending.call = delta.call;
    auto status = output_.StartFileSearch(&pending.output_index, wire);
    if (!status) return status;
    file_search_argument_bytes_ += delta.call.arguments.size();
    file_search_pending_.emplace(delta.call.index, std::move(pending));
    return RecordOutput(status, wire);
  }
  if (found == file_search_pending_.end()) return {};
  *handled = true;
  wire->clear();
  auto& pending = found->second.call;
  if (delta.call.custom ||
      (!delta.call.id.empty() && delta.call.id != pending.id) ||
      (!delta.call.name.empty() && delta.call.name != pending.name) ||
      delta.call.arguments.size() >
          ChatResponseParser::kMaxToolBytes - pending.arguments.size() ||
      delta.call.arguments.size() >
          ResponsesOutput::kStorageLimit - file_search_argument_bytes_)
    return Status::Fail("Native file search call changed or exceeded budget");
  pending.arguments += delta.call.arguments;
  file_search_argument_bytes_ += delta.call.arguments.size();
  return {};
}

ObjectReply ResponsesExecution::RunFileSearches(
    size_t completed_calls, const VectorSearchCallbacks& callbacks,
    VectorSearchUsage* usage, bool* continue_answer,
    const std::function<bool(const std::string&)>& publish) {
  *usage = {};
  *continue_answer = false;
  if (!begun_ || plan_.finished || commit_unknown_ || !PendingFileSearch())
    return Error(500, "response_state_invalid", "No executable file search");
  std::vector<VectorSearchPlan> plans;
  for (const auto& [index, pending] : file_search_pending_) {
    if (pending.dispatched)
      return Error(409, "file_search_already_dispatched",
                   "A dispatched native tool call cannot be replayed");
    if (index >= completed_calls)
      return Error(502, "file_search_incomplete",
                   "Unvalidated tool arguments must not execute");
    Json arguments;
    auto status = io::ParseJson(pending.call.arguments, &arguments, true,
                                kSchemaJsonLimits);
    ChatPlanError error;
    VectorSearchPlan search;
    if (status)
      status = PlanResponsesFileSearchCall(
          file_search_bindings_[pending.binding].tool, arguments, &search,
          &error);
    if (!status)
      return Error(502, "file_search_arguments_invalid", status.message());
    plans.push_back(std::move(search));
  }
  const auto user_limit = static_cast<uint64_t>(
      envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  const size_t executing = std::min<uint64_t>(
      plans.size(),
      user_limit - std::min<uint64_t>(user_limit, file_search_calls_));
  if (executing > kMaxNativeToolCalls - file_search_calls_)
    return Error(503, "file_search_resource_exhausted",
                 "Native tool execution work budget exhausted");
  const auto emit = [&](Status status, std::string* wire) -> ObjectReply {
    status = RecordOutput(status, wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    if (publish && !publish(*wire))
      return Error(499, "request_cancelled",
                   "Tool output subscriber cancelled");
    return {};
  };
  size_t current = 0;
  for (auto& [index, pending] : file_search_pending_) {
    (void)index;
    pending.dispatched = true;
    std::string wire;
    ObjectReply result;
    if (current >= executing) {
      result = emit(output_.SkipFileSearch(pending.output_index,
                                           Queries(plans[current]), &wire),
                    &wire);
      if (!result.ok()) return result;
      ++current;
      continue;
    }
    result = emit(output_.SearchFileSearch(pending.output_index,
                                           Queries(plans[current]), &wire),
                  &wire);
    if (!result.ok()) return result;
    const auto& binding = file_search_bindings_[pending.binding];
    VectorSearchUsage actual;
    ScopeExit receipt([&] {
      usage->admitted_input += actual.admitted_input;
      usage->committed_input += actual.committed_input;
      usage->unknown |= actual.unknown;
    });
    ++file_search_calls_;  // Never replay a call after its execution begins.
    result = ExecuteVectorSearchPlan(
        objects_, {access_.scope, access_.deadline, access_.cancelled},
        binding.tool.store_ids, plans[current], model::EmbeddingConfig::kHidden,
        callbacks, &actual);
    if (!result.ok()) {
      const auto failed =
          emit(output_.FailFileSearch(pending.output_index, &wire), &wire);
      return failed.ok() ? result : failed;
    }
    Json results;
    auto status = ResponsesFileSearchResults(result.value, &results);
    if (!status)
      return Error(502, "file_search_result_invalid", status.message());
    result =
        emit(output_.CompleteFileSearch(pending.output_index, results, &wire),
             &wire);
    if (!result.ok()) return result;
    ++current;
  }
  *continue_answer = completed_calls == file_search_pending_.size();
  file_search_pending_.clear();
  file_search_argument_bytes_ = 0;
  return {};
}

ObjectReply ResponsesExecution::ContinueFileSearch(
    int64_t output_used, const ChatPlanConfig& config,
    const ResponsesContextCheck& check, ResponsesGenerationPlan* next) {
  *next = {};
  const auto limit = envelope_.options.GetInt("max_output_tokens");
  if (!begun_ || plan_.finished || commit_unknown_ || PendingFileSearch() ||
      output_used < 0 || output_used >= limit)
    return Error(500, "response_state_invalid", "Invalid tool continuation");
  auto envelope = envelope_;
  Set(&envelope.options, "max_output_tokens", Number(limit - output_used));
  auto choice = envelope.request.Find("tool_choice")
                    ? *envelope.request.Find("tool_choice")
                    : String("auto");
  const bool exhausted =
      file_search_calls_ >= static_cast<uint64_t>(envelope.options.GetInt(
                                "max_tool_calls", INT64_MAX));
  if (choice.IsString() && choice.str == "required") choice = String("auto");
  if (choice.GetString("type") == "file_search")
    choice =
        Object({{"type", String("allowed_tools")},
                {"mode", String("auto")},
                {"tools", Array({Object({{"type", String("file_search")}})})}});
  if (choice.GetString("type") == "allowed_tools") {
    Set(&choice, "mode", String("auto"));
    if (exhausted) {
      auto& selected = Field(choice, "tools").array;
      std::erase_if(selected, [](const Json& item) {
        return item.GetString("type") == "file_search";
      });
      if (selected.empty()) choice = String("none");
    }
  } else if (exhausted) {
    auto selected = Array();
    for (const auto& definition : generation_.effective_tools.array)
      if (definition.GetString("type") != "file_search")
        selected.array.push_back(Object({{"type", *definition.Find("type")},
                                         {"name", *definition.Find("name")}}));
    choice = selected.array.empty() ? String("none")
                                    : Object({{"type", String("allowed_tools")},
                                              {"mode", String("auto")},
                                              {"tools", std::move(selected)}});
  }
  Set(&envelope.request, "tool_choice", std::move(choice));
  auto history = generation_history_;
  const auto& output = output_.Snapshot().GetArray("output")->array;
  history.array.insert(history.array.end(), output.begin(), output.end());
  ChatPlanError error;
  auto status =
      PrepareResponsesGeneration(envelope, history, config, next, &error);
  if (status)
    status = FitResponsesContext(envelope, config, check, next, &error);
  return status ? ObjectReply{} : PlanError(status, error);
}

ObjectReply ResponsesExecution::ContinueAsyncTools(
    size_t completed_calls, int64_t output_used, const ChatPlanConfig& config,
    const ResponsesContextCheck& check, ResponsesGenerationPlan* next,
    bool* continue_answer, std::string* wire) {
  *next = {};
  *continue_answer = false;
  wire->clear();
  const auto limit = envelope_.options.GetInt("max_output_tokens");
  if (!begun_ || plan_.finished || commit_unknown_ || PendingFileSearch() ||
      output_used < 0 || output_used >= limit)
    return Error(500, "response_state_invalid", "Invalid async continuation");
  if (async_continued_) return {};
  auto candidate = output_;
  std::string events;
  auto status =
      candidate.CompleteAsyncCalls(completed_calls, continue_answer, &events);
  if (!status) return Error(502, "async_tool_incomplete", status.message());
  if (!*continue_answer) return {};
  auto envelope = envelope_;
  Set(&envelope.options, "max_output_tokens", Number(limit - output_used));
  // One bounded independent-answer step, with no replay/new client dispatch.
  // Retain definitions for historical custom-tool rendering and constraints.
  Set(&envelope.request, "tool_choice", String("none"));
  const std::string instructions =
      "The asynchronous tool calls in this response have been handed to the "
      "client. Their results have not arrived. Continue any independent "
      "part of the user's task now. Do not invent tool results or repeat "
      "calls. If the remaining work depends on results, state briefly that "
      "they are pending.";
  auto history = generation_history_;
  const auto& output = candidate.Snapshot().GetArray("output")->array;
  history.array.insert(history.array.end(), output.begin(), output.end());
  // Resume is a real server scheduling decision after dispatch, not another
  // user message or an invented tool result. Keep it local to this model step.
  history.array.push_back(Object(
      {{"role", String("developer")}, {"content", String(instructions)}}));
  ChatPlanError error;
  status = PrepareResponsesGeneration(envelope, history, config, next, &error);
  if (status)
    status = FitResponsesContext(envelope, config, check, next, &error);
  if (!status) return PlanError(status, error);
  // Persist call completion before handing its done event to a subscriber.
  // A crash resumes no generated step and replays no client operation.
  output_ = std::move(candidate);
  status = RecordOutput({}, &events);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  auto saved = Checkpoint();
  if (!saved.ok()) return saved;
  async_continued_ = true;
  *wire = std::move(events);
  return {};
}
}  // namespace q4t::server
