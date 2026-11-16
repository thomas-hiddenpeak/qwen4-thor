#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;

ObjectReply ResponsesExecution::PrepareProgramMedia(Json* items) {
  if (!HasFileInputs(*items)) return {};
  auto used = OtherMcpMediaBudget();
  if (mcp_media_) AddResponsesMediaBudget(mcp_media_->Budget(), &used);
  const auto options =
      RemainingResponsesMediaOptions(program_file_options_, used);
  FileExpansion media(&objects_, options);
  const BinaryAccess access{access_.scope, access_.deadline, access_.cancelled};
  auto result = media.Prepare(*items, FileInputProfile::kResponsesStored,
                              Array(), access);
  if (!result.ok()) return result;
  Json rendered;
  result = media.Render(*items, &rendered);
  if (!result.ok()) return result;
  const auto& usage = media.Usage();
  const ResponsesMediaBudget current{usage, media.RetainedBytes(),
                                     OutputJson(rendered).size(), 0};
  AddResponsesMediaBudget(current, &used);
  result = CheckResponsesMediaBudget(program_file_options_, used);
  if (!result.ok()) return result;
  ResponsesMediaBudget programs{program_media_usage_, program_media_retained_,
                                program_media_rendered_, 0};
  AddResponsesMediaBudget(current, &programs);
  program_media_usage_ = programs.usage;
  program_media_retained_ = programs.retained;
  program_media_rendered_ = programs.rendered;
  // The published result contains frozen data URLs. Storage and stateless
  // replay therefore retain the same bytes without a second external read.
  *items = std::move(rendered);
  envelope_.prepared_file_media = true;
  return {};
}

bool ResponsesExecution::PendingProgramResume() const {
  return !program_resumed_ && std::any_of(programs_.begin(), programs_.end(),
                                          [](const auto& program) {
                                            return program.terminal.IsNull();
                                          });
}

Status ResponsesExecution::AppendProgram(const ResponseDelta& delta,
                                         bool* handled, std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  auto found = program_pending_.find(delta.call.index);
  if (delta.tool_start) {
    if (delta.call.name != kResponsesProgramModelName) {
      if (found != program_pending_.end())
        return Status::Fail("Program call changed identity");
      return {};
    }
    *handled = true;
    wire->clear();
    if (!program_enabled_ || found != program_pending_.end() ||
        delta.call.custom || delta.call.id.empty() ||
        program_pending_.size() >= kMaxNativeToolCalls ||
        delta.call.arguments.size() > ChatResponseParser::kMaxToolBytes ||
        delta.call.arguments.size() >
            ResponsesOutput::kStorageLimit - program_argument_bytes_)
      return Status::Fail("Invalid program call or resource limit");
    program_argument_bytes_ += delta.call.arguments.size();
    program_pending_.emplace(delta.call.index, delta.call);
    return {};
  }
  if (found == program_pending_.end()) return {};
  *handled = true;
  wire->clear();
  auto& call = found->second;
  if (delta.call.custom ||
      (!delta.call.id.empty() && delta.call.id != call.id) ||
      (!delta.call.name.empty() && delta.call.name != call.name) ||
      delta.call.arguments.size() >
          ChatResponseParser::kMaxToolBytes - call.arguments.size() ||
      delta.call.arguments.size() >
          ResponsesOutput::kStorageLimit - program_argument_bytes_)
    return Status::Fail("Program call changed or exceeded budget");
  call.arguments += delta.call.arguments;
  program_argument_bytes_ += delta.call.arguments.size();
  return {};
}

ObjectReply ResponsesExecution::RunPrograms(
    size_t completed_calls, bool* client_wait,
    const std::function<bool(const std::string&)>& publish) {
  *client_wait = false;
  if (!begun_ || plan_.finished || commit_unknown_ ||
      (!PendingPrograms() && !PendingProgramResume()))
    return Error(500, "response_state_invalid", "No executable program");
  auto pending = std::exchange(program_pending_, {});
  program_argument_bytes_ = 0;
  program_client_wait_ = false;
  program_async_wait_ = false;
  bool asynchronous = true;
  const auto append = [&](const ResponsesProgramState& program,
                          Json emitted) -> ObjectReply {
    auto media = PrepareProgramMedia(&emitted);
    if (!media.ok()) return media;
    auto contracts = plan_.tool_contracts;
    auto bound = BindResponsesProgramContracts(program, emitted, &contracts);
    if (!bound.ok()) return bound;
    auto candidate = output_;
    std::string wire;
    auto status = candidate.AppendProgramItems(emitted, &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    output_ = std::move(candidate);
    plan_.tool_contracts = std::move(contracts);
    status = RecordOutput({}, &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    auto saved = Checkpoint();
    if (!saved.ok()) return saved;
    if (publish && !publish(wire))
      return Error(499, "request_cancelled", "Program subscriber disconnected");
    return {};
  };
  const auto reopen = [&](const std::string& call_id,
                          ResponsesProgramState* program) -> ObjectReply {
    auto history = generation_history_;
    for (const auto& item : output_.Snapshot().GetArray("output")->array)
      history.array.push_back(item);
    std::vector<ResponsesProgramState> opened;
    auto result = OpenResponsesPrograms(cipher_, history, cipher_config_,
                                        access_, &opened, mcp_state_.get());
    if (!result.ok()) return result;
    const auto found =
        std::find_if(opened.begin(), opened.end(), [&](const auto& value) {
          return value.program.GetString("call_id") == call_id;
        });
    if (found == opened.end())
      return Error(500, "program_state_invalid",
                   "Program continuation missing");
    *program = std::move(*found);
    return {};
  };
  const auto execute = [&](ResponsesProgramState program,
                           bool fresh) -> ObjectReply {
    if (!fresh) {
      // Approval execution precedes program continuation. Reopen its newly
      // authenticated result instead of replaying the pre-approval snapshot.
      auto result = reopen(program.program.GetString("call_id"), &program);
      if (!result.ok()) return result;
    }
    if (!program.terminal.IsNull()) return {};
    const auto limit = static_cast<uint64_t>(
        envelope_.options.GetInt("max_tool_calls", INT64_MAX));
    if (native_tool_calls_ >= limit ||
        native_tool_calls_ >= kMaxNativeToolCalls) {
      auto ended = Array({Object(
          {{"type", String("program_output")},
           {"id",
            String("prog_out_" +
                   Digest(program.program.GetString("call_id")).substr(0, 56))},
           {"call_id", *program.program.Find("call_id")},
           {"result", String("{\"error\":\"program_call_budget_exhausted\"}")},
           {"status", String("incomplete")}})});
      if (fresh) ended.array.insert(ended.array.begin(), program.program);
      return append(program, std::move(ended));
    }
    ++native_tool_calls_;
    for (size_t step = 0; step <= kMaxNativeToolCalls; ++step) {
      if (access_.cancelled && access_.cancelled())
        return Error(499, "request_cancelled", "Program cancelled");
      if (std::chrono::steady_clock::now() >= access_.deadline)
        return Error(408, "request_deadline_exceeded", "Program expired");
      auto emitted = Array();
      auto result = AdvanceResponsesProgram(program, access_, &emitted);
      if (!result.ok()) return result;
      const auto terminal = std::find_if(
          emitted.array.begin(), emitted.array.end(), [](const auto& item) {
            return item.GetString("type") == "program_output";
          });
      const bool aborted = terminal != emitted.array.end() &&
                           terminal->GetString("status") == "incomplete";
      const bool finished = terminal != emitted.array.end();
      const auto before =
          program.worker_input.GetArray("results")->array.size();
      if (fresh) emitted.array.insert(emitted.array.begin(), program.program);
      // Prepare each complete public batch before publishing any of it. A
      // pure program producing invalid media must not leak its code/item.
      auto public_items = Array();
      for (const auto& item : emitted.array) {
        if (item.GetString("type") == kResponsesProgramMcpIntent) {
          // An aborted VM cannot authorize new external side effects.
          if (aborted) continue;
          if (!public_items.array.empty()) {
            result = append(program, std::move(public_items));
            if (!result.ok()) return result;
            public_items = Array();
          }
          result = ExecuteProgramMcp(program, item, publish);
          if (!result.ok()) return result;
        } else {
          public_items.array.push_back(item);
        }
      }
      if (!public_items.array.empty()) {
        result = append(program, std::move(public_items));
        if (!result.ok()) return result;
      }
      fresh = false;
      if (finished) return {};
      result = reopen(program.program.GetString("call_id"), &program);
      if (!result.ok()) return result;
      const auto& results = program.worker_input.GetArray("results")->array;
      if (results.size() > before) continue;
      *client_wait = true;
      for (size_t i = 0; i < program.calls.size(); ++i) {
        const bool returned =
            std::any_of(results.begin(), results.end(), [&](const auto& value) {
              return value.GetInt("index") == static_cast<int64_t>(i);
            });
        if (!returned) asynchronous &= program.calls[i].GetBool("async");
      }
      return {};
    }
    return Error(503, "program_execution_capacity",
                 "Program continuation limit");
  };
  if (PendingProgramResume()) {
    // A request performs each local continuation once. Even an unknown
    // publication receipt cannot cause a speculative second execution.
    program_resumed_ = true;
    for (const auto& program : programs_) {
      auto result = execute(program, false);
      if (!result.ok()) return result;
    }
  }
  for (const auto& [index, call] : pending) {
    if (index >= completed_calls)
      return Error(502, "program_incomplete", "Truncated code cannot execute");
    Json arguments;
    if (!io::ParseJson(call.arguments, &arguments, true, kSchemaJsonLimits) ||
        !arguments.IsObject() || arguments.object.size() != 1 ||
        !arguments.Find("code") || !arguments.Find("code")->IsString())
      return Error(502, "program_arguments_invalid", "Invalid generated code");
    Json item;
    auto result = CreateResponsesProgram(
        cipher_, arguments.GetString("code"), program_tools_, cipher_config_,
        access_, &item, envelope_.options.GetBool("parallel_tool_calls", true),
        &mcp_bindings_);
    if (!result.ok()) return result;
    std::vector<ResponsesProgramState> opened;
    result = OpenResponsesPrograms(cipher_, Array({item}), cipher_config_,
                                   access_, &opened, mcp_state_.get());
    if (!result.ok()) return result;
    result = execute(opened.front(), true);
    if (!result.ok()) return result;
  }
  program_client_wait_ = *client_wait;
  program_async_wait_ = *client_wait && asynchronous;
  return {};
}
}  // namespace q4t::server
