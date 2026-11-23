#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/mcp_policy.h"

namespace q4t::server {
using namespace object_detail;

ResponsesMediaBudget ResponsesExecution::OtherMcpMediaBudget() const {
  ResponsesMediaBudget used{program_media_usage_, program_media_retained_,
                            program_media_rendered_, 0};
  used.rendered += OutputJson(generation_history_).size();
  if (files_) {
    const ResponsesMediaBudget input{files_->Usage(), files_->RetainedBytes(),
                                     0, files_->PendingOutputBudget()};
    AddResponsesMediaBudget(input, &used);
  }
  return used;
}

uint64_t ResponsesExecution::FileOutputBudget() const {
  uint64_t pending = files_ ? files_->PendingOutputBudget() : 0;
  if (mcp_media_) pending += mcp_media_->PendingOutputBudget();
  if (HasMcp() && !envelope_.cache.prewarm &&
      !cipher_config_.count_input_only) {
    const auto limit = static_cast<uint64_t>(
        program_file_options_.total_summary_output_tokens);
    const auto used = FileUsage()->model_output_tokens;
    pending = std::max(pending, limit - std::min(limit, used));
  }
  return pending;
}

const FileExpansionUsage* ResponsesExecution::FileUsage() const {
  auto used = OtherMcpMediaBudget();
  if (mcp_media_) AddResponsesMediaBudget(mcp_media_->Budget(), &used);
  file_usage_ = used.usage;
  return &file_usage_;
}

ObjectReply ResponsesExecution::ResolveMcpResource(const std::string& id,
                                                   const std::string& uri) {
  if (!mcp_state_)
    return Error(503, "mcp_preparation_invalid", "Missing invocation owner");
  auto existing = mcp_state_->Resource(id, uri, access_);
  if (existing.ok()) {
    const auto state = existing.value.GetString("state");
    if (state != "ready" && state != "dispatching") return {};
    // A live competitor cannot be taken over. Dead ownership closes unknown
    // work without a new dispatch; the frozen result is then safe to consume.
    auto recovered = mcp_state_->Recover(id, access_);
    if (!recovered.ok() || recovered.commit_unknown)
      return BindMutationResult(std::move(recovered));
    existing = mcp_state_->Resource(id, uri, access_);
    if (!existing.ok()) return existing;
    if (existing.value.GetString("state") != "ready" &&
        existing.value.GetString("state") != "dispatching")
      return {};
    return Error(409, "mcp_resource_pending", "Resource owner is still active");
  }
  if (existing.code != "mcp_resource_unprepared") return existing;
  auto owned = mcp_state_->Inspect(id, access_);
  if (!owned.ok()) return owned;
  ResponsesMcpDefinition original;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(*owned.value.Find("declaration"), &original,
                                  &error))
    return Error(503, "mcp_state_invalid", "Invalid resource authority");
  BindResponsesMcpCredentials(&original, mcp_credentials_);
  const auto current = std::find_if(
      mcp_definitions_.begin(), mcp_definitions_.end(), [&](const auto& value) {
        return value.declaration.GetString("server_label") ==
               original.declaration.GetString("server_label");
      });
  auto definition = current == mcp_definitions_.end() ? original : *current;
  auto authorization = AuthorizeAgentMcp(&definition);
  if (!authorization.ok()) return authorization;
  auto result = ResolveMcpDeploymentTarget(cipher_config_.mcp_policy.get(),
                                           access_.scope, &definition);
  if (!result.ok()) return result;
  result = TrackMcpCall(id);
  if (!result.ok()) return result;
  result = mcp_state_->ProposeResource(id, uri, definition, access_);
  if (!result.ok() || result.commit_unknown)
    return BindMutationResult(std::move(result));
  McpTransportReceipt receipt;
  McpSession session;
  session.allow_new = false;
  if (const auto* original_session = owned.value.Find("session"))
    session.state = *original_session;
  result = InvokeMcp(
      {access_.scope, access_.deadline, access_.cancelled},
      definition.credentials, mcp_fetch_, "resources/read",
      Object({{"uri", String(uri)}}),
      [&] {
        return mcp_state_->PermitResource(id, uri, definition, access_,
                                          &session.state);
      },
      &receipt, {}, &session, cipher_config_.mcp_tunnels);
  if (result.commit_unknown) return BindMutationResult(std::move(result));
  const StateAccess cleanup{
      access_.scope,
      std::chrono::steady_clock::now() + std::chrono::seconds(2),
      {}};
  auto completed = mcp_state_->FinishResource(
      id, uri, receipt, result, cleanup,
      session.state.Has("protocol") ? &session.state : nullptr);
  if (!completed.ok() || completed.commit_unknown)
    return BindMutationResult(std::move(completed));
  if (session.state.Has("protocol")) {
    const auto catalog_id = owned.value.GetString("catalog_id");
    const auto current = mcp_state_->Session(catalog_id, definition, cleanup);
    if (!current.ok()) return current;
    const auto* bound = current.value.Find("session");
    // A newer catalog session must not be replaced by an older call's read.
    if (bound && bound->GetString("id") == session.state.GetString("id")) {
      auto saved = mcp_state_->BindSession(catalog_id, definition,
                                           current.value.GetInt("revision"),
                                           session.state, cleanup);
      if (!saved.ok() || saved.commit_unknown)
        return BindMutationResult(std::move(saved));
    }
  }
  return completed;
}

ObjectReply ResponsesExecution::PrepareMcpMedia(const ChatPlanConfig& config,
                                                const Json& history) {
  if (!mcp_state_) return {};
  if (!mcp_media_) mcp_media_ = std::make_unique<ResponsesMcpMedia>(objects_);
  auto result = mcp_media_->Prepare(*mcp_state_, history, program_file_options_,
                                    OtherMcpMediaBudget(), access_,
                                    require_frozen_inputs_ && !begun_,
                                    [&](const auto& id, const auto& uri) {
                                      return ResolveMcpResource(id, uri);
                                    });
  if (!result.ok() || result.commit_unknown)
    return BindMutationResult(std::move(result));
  result = mcp_media_->Persist(*mcp_state_, access_);
  if (!result.ok() || result.commit_unknown)
    return BindMutationResult(std::move(result));
  if (!mcp_media_->PendingSheets())
    return mcp_media_->Render(&envelope_.mcp_content);
  (void)config;
  return {};
}

ObjectReply ResponsesExecution::PrepareToolMedia(
    const ChatPlanConfig& config,
    const std::function<bool(const std::string&)>& publish) {
  if (!begun_ || plan_.finished || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid tool media owner");
  auto history = generation_history_;
  for (const auto& item : output_.Snapshot().GetArray("output")->array) {
    if (item.GetString("type").starts_with("mcp_") &&
        std::any_of(history.array.begin(), history.array.end(),
                    [&](const auto& old) {
                      return old.GetString("id") == item.GetString("id") &&
                             OutputJson(old) == OutputJson(item);
                    }))
      continue;
    history.array.push_back(item);
  }
  const auto catalogs = plan_.mcp_catalogs.array.size();
  auto result = PrepareMcp(config, &history);
  if (!result.ok()) return result;
  if (plan_.mcp_catalogs.array.size() > catalogs) {
    for (size_t i = catalogs; i < plan_.mcp_catalogs.array.size(); ++i)
      generation_history_.array.push_back(plan_.mcp_catalogs.array[i]);
    std::string wire;
    auto status =
        RecordOutput(BeginMcpCatalogs(&output_, &wire, catalogs), &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    result = Checkpoint();
    if (!result.ok()) return result;
    if (publish && !publish(wire))
      return Error(499, "request_cancelled",
                   "MCP listing subscriber cancelled");
  }
  return PrepareMcpMedia(config, history);
}
}  // namespace q4t::server
