#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <limits>

#include "object_protocol.h"
#include "q4t/server/containers.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_python.h"

namespace q4t::server {
using namespace object_detail;

Status ResponsesExecution::AppendPython(const ResponseDelta& delta,
                                        bool* handled, std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  const auto& chunk = delta.call;
  auto found = python_pending_.find(chunk.index);
  if (delta.tool_start &&
      (python_container_.IsNull() || chunk.name != kPythonToolName))
    return {};
  if (!delta.tool_start && found == python_pending_.end()) return {};
  size_t pending_bytes = chunk.arguments.size();
  for (const auto& entry : python_pending_)
    pending_bytes += entry.second.arguments.size();
  if (pending_bytes > ChatResponseParser::kMaxToolBytes)
    return Status::Fail("Pending interpreter arguments exceed capacity");
  if (delta.tool_start) {
    if (found != python_pending_.end() || chunk.custom || chunk.id.empty() ||
        python_pending_.size() >= kMaxNativeToolCalls ||
        chunk.arguments.size() > ChatResponseParser::kMaxToolBytes)
      return Status::Fail("Invalid Python call identity or capacity");
    python_pending_.emplace(chunk.index, chunk);
  } else if (found != python_pending_.end()) {
    auto& call = found->second;
    if (chunk.custom || (!chunk.id.empty() && chunk.id != call.id) ||
        (!chunk.name.empty() && chunk.name != call.name) ||
        chunk.arguments.size() >
            ChatResponseParser::kMaxToolBytes - call.arguments.size())
      return Status::Fail("Python call changed identity or exceeded capacity");
    call.arguments += chunk.arguments;
  } else {
    return {};
  }
  *handled = true;
  wire->clear();
  return {};
}

Status ResponsesOutput::StartPython(std::string container,
                                    const std::string& code, size_t* index,
                                    std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems)
    return Status::Fail("Inactive interpreter output");
  auto item = Object({{"type", String("code_interpreter_call")},
                      {"id", String(detail::NewRequestId("ci_"))},
                      {"status", String("in_progress")},
                      {"container_id", String(std::move(container))},
                      {"code", io::Json()},
                      {"outputs", io::Json()}});
  ChatPlanError error;
  auto valid = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                          &error, "code_interpreter_call");
  if (valid)
    valid =
        Charge(OutputJson(item).size() + code.size() + 8 * sizeof(io::Json));
  if (!valid) return valid;
  *index = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(std::numeric_limits<size_t>::max());
  Event("response.output_item.added",
        Object({{"output_index", Number(*index)},
                {"item", Items().array[*index]}}),
        wire);
  Event("response.code_interpreter_call.in_progress", Location(*index), wire);
  auto fields = Location(*index);
  Set(&fields, "delta", String(code));
  Event("response.code_interpreter_call_code.delta", std::move(fields), wire);
  auto& active = Items().array[*index];
  Set(&active, "code", String(code));
  fields = Location(*index);
  Set(&fields, "code", String(code));
  Event("response.code_interpreter_call_code.done", std::move(fields), wire);
  Set(&active, "status", String("interpreting"));
  Event("response.code_interpreter_call.interpreting", Location(*index), wire);
  return {};
}
Status ResponsesOutput::EndPython(size_t index, const std::string& logs,
                                  bool failed, const io::Json* generated,
                                  std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "code_interpreter_call" ||
      Items().array[index].GetString("status") != "interpreting")
    return Status::Fail("Invalid interpreter output transition");
  auto item = Items().array[index];
  Set(&item, "outputs",
      Array({Object({{"type", String("logs")}, {"logs", String(logs)}})}));
  Set(&item, "status", String(failed ? "failed" : "completed"));
  ChatPlanError error;
  auto valid = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                          &error, "code_interpreter_call");
  if (valid) valid = Charge(logs.size() + 4 * sizeof(io::Json));
  if (!valid) return valid;
  python_files_.clear();
  python_item_ = index;
  if (generated) {
    for (const auto& file : generated->array) {
      if (file.GetString("container_id") != item.GetString("container_id"))
        return Status::Fail(
            "Interpreter file belongs to a different container");
      auto charged = Charge(OutputJson(file).size() + 10 * sizeof(io::Json));
      if (!charged) return charged;
      python_files_.emplace(file.GetString("path"), file);
    }
  }
  Items().array[index] = std::move(item);
  if (!failed)
    Event("response.code_interpreter_call.completed", Location(index), wire);
  CloseItem(index, false, wire);
  return {};
}

Status ResponsesExecution::CitePythonFiles(std::string* wire) {
  if (!begun_ || plan_.finished || commit_unknown_)
    return Status::Fail("Response cannot accept interpreter file citations");
  ResponsesOutput::ContainerFileExport save;
  if (!plan_.assistant_run_id.empty()) {
    save = [&](const std::string& message, const Json& file) {
      auto result = ExportAssistantOutputFile(objects_, access_, message, file);
      return result.ok() ? Status{} : Status::Fail(result.ClientMessage());
    };
  }
  return RecordOutput(output_.CitePythonFiles(save, wire), wire);
}

ObjectReply ResponsesExecution::RunPython(
    size_t completed_calls,
    const std::function<bool(const std::string&)>& publish) {
  if (!begun_ || plan_.finished || commit_unknown_ || !PendingPython())
    return Error(500, "response_state_invalid",
                 "Invalid interpreter continuation");
  std::vector<std::string> programs;
  for (const auto& [index, call] : python_pending_) {
    Json args;
    if (index >= completed_calls ||
        !io::ParseJson(call.arguments, &args, true, kSchemaJsonLimits) ||
        !args.IsObject() || args.object.size() != 1 || !args.Find("code") ||
        !args.Find("code")->IsString() || args.GetString("code").empty() ||
        args.Find("code")->str.size() > (64 << 10))
      return Error(502, "code_interpreter_arguments_invalid",
                   "Expected complete Python source");
    programs.push_back(args.Find("code")->str);
  }
  python_pending_.clear();
  const size_t limit =
      std::min<uint64_t>(kMaxNativeToolCalls,
                         envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  if (programs.size() > limit - std::min(limit, native_tool_calls_))
    return Error(400, "native_tool_limit_exceeded",
                 "Native tool budget exhausted");
  if (python_container_.IsString())
    python_container_id_ = python_container_.str;
  else if (python_container_id_.empty()) {
    const auto* files = python_container_.GetArray("file_ids");
    const bool configured = (files && !files->array.empty()) ||
                            python_container_.Has("memory_limit") ||
                            python_container_.Has("network_policy");
    if (!configured) {
      for (auto it = generation_history_.array.rbegin();
           it != generation_history_.array.rend(); ++it) {
        if (it->GetString("type") != "code_interpreter_call") continue;
        const auto id = it->GetString("container_id");
        auto previous = HandleContainers(objects_, access_, "GET",
                                         "/v1/containers/" + id, "");
        if (previous.ok() && previous.value.GetString("status") == "running") {
          python_container_id_ = id;
          break;
        }
        if (!previous.ok() && previous.http_status != 404) return previous;
      }
    }
    if (python_container_id_.empty()) {
      auto create = python_container_;
      std::erase_if(create.object,
                    [](const auto& f) { return f.first == "type"; });
      Set(&create, "name", String("code-interpreter"));
      auto opened = HandleContainers(objects_, access_, "POST",
                                     "/v1/containers", OutputJson(create));
      if (!opened.ok()) return opened;
      python_container_id_ = opened.value.GetString("id");
    }
  }
  for (const auto& code : programs) {
    size_t index;
    std::string wire;
    const auto emit = [&](Status status) -> ObjectReply {
      status = RecordOutput(status, &wire);
      if (!status)
        return Error(500, "code_interpreter_output_invalid", status.message());
      if (publish && !publish(wire))
        return Error(499, "request_cancelled",
                     "Interpreter subscription closed");
      return {};
    };
    auto ready =
        emit(output_.StartPython(python_container_id_, code, &index, &wire));
    if (!ready.ok()) return ready;
    ++native_tool_calls_;
    auto run =
        ExecuteContainerPython(objects_, access_, python_container_id_, code);
    auto ended = emit(output_.EndPython(
        index, run.ok() ? run.value.GetString("logs") : run.ClientMessage(),
        !run.ok() || run.value.GetBool("error"),
        run.ok() ? run.value.GetArray("generated_files") : nullptr, &wire));
    if (!ended.ok()) return ended;
    if (!run.ok()) return run;
  }
  return {};
}
}  // namespace q4t::server
