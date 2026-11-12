#include "q4t/server/responses_execution.h"

#include "object_protocol.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply FileState() {
  return Error(500, "response_file_state_invalid",
               "Invalid response file preparation state");
}
ObjectReply Active(const StateAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "File preparation cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "File preparation expired");
  return {};
}
}  // namespace

ObjectReply ResponsesExecution::BuildPreparedGeneration(
    const ChatPlanConfig& config) {
  if (PendingFileSheets() || generation_ready_) return FileState();
  auto reply = Active(access_);
  if (!reply.ok()) return reply;
  if (files_) {
    reply = files_->Render(file_history_, &generation_history_);
    if (!reply.ok()) return reply;
    // Authorization/order/schema were checked against the original input.
    // Only tool result values are replaced by immutable prepared content.
    // The VM cannot fetch URLs or open files on its own.
    for (auto& program : programs_)
      for (auto& result : Field(program.worker_input, "results").array) {
        const auto& call = program.calls.at(result.GetInt("index"));
        for (const auto& item : generation_history_.array) {
          if (item.GetString("call_id") != call.GetString("call_id") ||
              !item.Has("output"))
            continue;
          auto value = *item.Find("output");
          if (call.GetString("type") == "function_call" && value.IsString()) {
            Json decoded;
            if (io::ParseJson(value.str, &decoded, true, kSchemaJsonLimits))
              value = std::move(decoded);
          }
          Set(&result, "value", std::move(value));
          break;
        }
      }
  }
  ChatPlanError error;
  auto status = PrepareResponsesGeneration(envelope_, generation_history_,
                                           config, &generation_, &error);
  if (!status) {
    reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  generation_ready_ = true;
  return {};
}

ObjectReply ResponsesExecution::PersistPreparedInputs() {
  if (!files_ || (!plan_.store && plan_.conversation_id.empty())) return {};
  if (commit_unknown_) return FileState();
  auto reply =
      files_->Persist({access_.scope, access_.deadline, access_.cancelled});
  if (!reply.ok()) {
    if (reply.commit_unknown) {
      commit_unknown_ = true;
      files_->RetainUncertainHolds();
    }
    return reply;
  }
  plan_.input_snapshots = files_->Bindings();
  return {};
}

void ResponsesExecution::ReleasePreparedInputs() noexcept {
  if (!files_ || commit_unknown_ ||
      (!plan_.store && !plan_.finished && !plan_.conversation_id.empty()))
    return;
  try {
    // The durable record now owns its references. Releasing preparation holds
    // is auxiliary cleanup; a failed release expires and cannot fail service.
    files_->Release(
        {access_.scope,
         std::chrono::steady_clock::now() + std::chrono::milliseconds(250),
         {}});
  } catch (...) {
  }
}

ObjectReply ResponsesExecution::NextFileSheet(const ChatPlanConfig& config,
                                              FileSheetStep* step) {
  if (!prepared_ || !PendingFileSheets() || plan_.finished || commit_unknown_)
    return FileState();
  auto reply = Active(access_);
  return reply.ok() ? files_->NextSheet(config, step) : reply;
}

ObjectReply ResponsesExecution::CompleteFileSheet(
    const FileSheetStep& step, std::string_view output,
    std::string_view finish_reason, uint64_t input_tokens,
    uint64_t output_tokens, const ChatPlanConfig& config) {
  if (!prepared_ || !PendingFileSheets() || plan_.finished || commit_unknown_)
    return FileState();
  auto reply = Active(access_);
  if (!reply.ok()) return reply;
  reply = files_->CompleteSheet(step, output, finish_reason, input_tokens,
                                output_tokens);
  if (!reply.ok()) return reply;
  if (!PendingFileSheets()) {
    reply = BuildPreparedGeneration(config);
    if (!reply.ok()) return reply;
  }
  if (begun_) {
    reply = PersistPreparedInputs();
    if (reply.ok()) reply = Checkpoint();
    if (!reply.ok()) {
      if (reply.commit_unknown) files_->RetainUncertainHolds();
      return reply;
    }
    ReleasePreparedInputs();
  }
  return {};
}
}  // namespace q4t::server
