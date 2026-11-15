#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;

ObjectReply ResponsesExecution::PrepareProgramMedia(Json* items) {
  if (!HasFileInputs(*items)) return {};
  auto options = program_file_options_;
  const auto previous = files_ ? files_->Usage() : FileExpansionUsage{};
  const auto remaining = [](auto limit, uint64_t used) {
    return limit - std::min<uint64_t>(limit, used);
  };
  options.max_source_bytes =
      remaining(options.max_source_bytes,
                previous.source_bytes + program_media_usage_.source_bytes);
  options.max_files =
      remaining(options.max_files, previous.files + program_media_usage_.files);
  options.extraction.pdf.max_image_bytes =
      remaining(options.extraction.pdf.max_image_bytes,
                previous.image_bytes + program_media_usage_.image_bytes);
  options.extraction.pdf.max_pixels =
      remaining(options.extraction.pdf.max_pixels,
                previous.pixels + program_media_usage_.pixels);
  options.max_retained_bytes = remaining(
      options.max_retained_bytes,
      (files_ ? files_->RetainedBytes() : 0) + program_media_retained_);
  options.max_rendered_bytes = remaining(
      options.max_rendered_bytes,
      OutputJson(generation_history_).size() + program_media_rendered_);
  if (!options.max_source_bytes || !options.max_files ||
      !options.extraction.pdf.max_image_bytes ||
      !options.extraction.pdf.max_pixels || !options.max_retained_bytes ||
      !options.max_rendered_bytes)
    return Error(413, "file_input_capacity_exceeded",
                 "Program media exhausted the shared preparation budget");
  FileExpansion media(&objects_, options);
  const BinaryAccess access{access_.scope, access_.deadline, access_.cancelled};
  auto result = media.Prepare(*items, FileInputProfile::kResponsesStored,
                              Array(), access);
  if (!result.ok()) return result;
  Json rendered;
  result = media.Render(*items, &rendered);
  if (!result.ok()) return result;
  const auto& usage = media.Usage();
  program_media_usage_.source_bytes += usage.source_bytes;
  program_media_usage_.image_bytes += usage.image_bytes;
  program_media_usage_.pixels += usage.pixels;
  program_media_usage_.files += usage.files;
  program_media_usage_.images += usage.images;
  program_media_retained_ += media.RetainedBytes();
  program_media_rendered_ += OutputJson(rendered).size();
  // The published result contains frozen data URLs. Storage and stateless
  // replay therefore retain the same bytes without a second external read.
  *items = std::move(rendered);
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
  const auto execute = [&](const ResponsesProgramState& program,
                           bool fresh) -> ObjectReply {
    if (!program.terminal.IsNull()) return {};
    if (access_.cancelled && access_.cancelled())
      return Error(499, "request_cancelled", "Program cancelled");
    if (std::chrono::steady_clock::now() >= access_.deadline)
      return Error(408, "request_deadline_exceeded", "Program expired");
    auto emitted = Array();
    const auto limit = static_cast<uint64_t>(
        envelope_.options.GetInt("max_tool_calls", INT64_MAX));
    if (native_tool_calls_ >= limit ||
        native_tool_calls_ >= kMaxNativeToolCalls) {
      emitted.array.push_back(Object(
          {{"type", String("program_output")},
           {"id",
            String("prog_out_" +
                   Digest(program.program.GetString("call_id")).substr(0, 56))},
           {"call_id", *program.program.Find("call_id")},
           {"result", String("{\"error\":\"program_call_budget_exhausted\"}")},
           {"status", String("incomplete")}}));
    } else {
      ++native_tool_calls_;
      auto advanced = AdvanceResponsesProgram(program, access_, &emitted);
      if (!advanced.ok()) return advanced;
    }
    const bool terminal = std::any_of(
        emitted.array.begin(), emitted.array.end(), [](const auto& item) {
          return item.GetString("type") == "program_output";
        });
    *client_wait |= !terminal;
    if (!terminal) {
      for (size_t i = 0; i < program.calls.size(); ++i) {
        const auto& results = program.worker_input.GetArray("results")->array;
        const bool returned = std::any_of(
            results.begin(), results.end(), [&](const auto& result) {
              return result.GetInt("index") == static_cast<int64_t>(i);
            });
        if (!returned) asynchronous &= program.calls[i].GetBool("async");
      }
      for (const auto& item : emitted.array)
        if (item.GetString("type") == "function_call" ||
            item.GetString("type") == "custom_tool_call")
          asynchronous &= item.GetBool("async");
    }
    if (fresh) emitted.array.insert(emitted.array.begin(), program.program);
    if (emitted.array.empty()) return {};
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
        access_, &item, envelope_.options.GetBool("parallel_tool_calls", true));
    if (!result.ok()) return result;
    std::vector<ResponsesProgramState> opened;
    result = OpenResponsesPrograms(cipher_, Array({item}), cipher_config_,
                                   access_, &opened);
    if (!result.ok()) return result;
    result = execute(opened.front(), true);
    if (!result.ok()) return result;
  }
  program_client_wait_ = *client_wait;
  program_async_wait_ = *client_wait && asynchronous;
  return {};
}
}  // namespace q4t::server
