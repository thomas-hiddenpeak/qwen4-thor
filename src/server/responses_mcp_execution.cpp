#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server {
using namespace object_detail;

ObjectReply ResponsesExecution::TrackMcpCall(const std::string& id) {
  if (!cleanup_)
    return require_cleanup_ ? Error(503, "mcp_reconciliation_unavailable",
                                    "MCP lifecycle owner unavailable")
                            : ObjectReply{};
  if (!cleanup_ticket_) {
    auto reserved = cleanup_->Reserve(plan_.scope, plan_.id, &cleanup_ticket_);
    if (!reserved.ok()) return reserved;
  }
  return cleanup_->TrackMcp(cleanup_ticket_, cipher_config_.model,
                            plan_.execution_owner, id);
}

ObjectReply ResponsesExecution::CloseMcpExecution(const StateAccess& access) {
  if (!mcp_state_) return {};
  auto items = *output_.Snapshot().GetArray("output");
  // Network/grammar execution has drained. Close only this exact Response's
  // invocation; never rewrite already-published stream events. A subsequent
  // authenticated continuation upgrades no-result fragments from this record.
  return mcp_state_->Reconcile(&items, plan_, access);
}

Status ResponsesExecution::AppendMcp(const ResponseDelta& delta, bool* handled,
                                     std::string* wire) {
  *handled = false;
  if (delta.kind != ResponseDelta::Kind::kTool) return {};
  auto found = mcp_pending_.find(delta.call.index);
  if (delta.tool_start) {
    const auto binding = std::find_if(
        mcp_bindings_.begin(), mcp_bindings_.end(),
        [&](const auto& value) { return value.model_name == delta.call.name; });
    if (binding == mcp_bindings_.end()) {
      if (found != mcp_pending_.end())
        return Status::Fail("MCP call index changed identity");
      return {};
    }
    *handled = true;
    wire->clear();
    if (!mcp_state_ || !binding->direct || found != mcp_pending_.end() ||
        delta.call.custom || delta.call.id.empty() ||
        delta.call.arguments.size() > ChatResponseParser::kMaxToolBytes ||
        delta.call.arguments.size() >
            ResponsesOutput::kStorageLimit - mcp_argument_bytes_)
      return Status::Fail("Invalid MCP call or argument capacity");
    NativeMcpCall pending;
    pending.call = delta.call;
    pending.binding = binding - mcp_bindings_.begin();
    if (!binding->approval) {
      pending.public_id = detail::NewRequestId("mcp_");
      if (pending.public_id.empty()) return Status::Fail("MCP ID unavailable");
      const auto item = Object({{"id", String(pending.public_id)},
                                {"type", String("mcp_call")},
                                {"server_label", String(binding->server_label)},
                                {"name", String(binding->name)},
                                {"arguments", String("")},
                                {"approval_request_id", Json()},
                                {"output", Json()},
                                {"error", Json()},
                                {"status", String("in_progress")}});
      auto status = output_.StartMcpItem(item, &pending.output_index, wire);
      if (!status) return status;
      if (!delta.call.arguments.empty()) {
        std::string part;
        status = output_.AppendMcpArguments(pending.output_index,
                                            delta.call.arguments, &part);
        if (!status) return status;
        *wire += part;
      }
    }
    mcp_argument_bytes_ += delta.call.arguments.size();
    mcp_pending_.emplace(delta.call.index, std::move(pending));
    return RecordOutput({}, wire);
  }
  if (found == mcp_pending_.end()) return {};
  *handled = true;
  wire->clear();
  auto& pending = found->second;
  if (pending.dispatched || delta.call.custom ||
      (!delta.call.id.empty() && delta.call.id != pending.call.id) ||
      (!delta.call.name.empty() && delta.call.name != pending.call.name) ||
      delta.call.arguments.size() >
          ChatResponseParser::kMaxToolBytes - pending.call.arguments.size() ||
      delta.call.arguments.size() >
          ResponsesOutput::kStorageLimit - mcp_argument_bytes_)
    return Status::Fail("MCP call changed or argument capacity exhausted");
  pending.call.arguments += delta.call.arguments;
  mcp_argument_bytes_ += delta.call.arguments.size();
  if (pending.output_index != ResponsesOutput::kMaxItems)
    return RecordOutput(output_.AppendMcpArguments(pending.output_index,
                                                   delta.call.arguments, wire),
                        wire);
  return {};
}

ObjectReply ResponsesExecution::ExecuteMcpCall(
    const Json& call, const ResponsesMcpDefinition& requested, size_t index,
    const std::function<bool(const std::string&)>& publish) {
  auto definition = requested;
  auto authorization = AuthorizeAgentMcp(&definition);
  if (!authorization.ok()) return authorization;
  auto resolved = ResolveMcpDeploymentTarget(cipher_config_.mcp_policy.get(),
                                             access_.scope, &definition);
  if (!resolved.ok()) return resolved;
  // Do not even initialize an anonymous remote session when an owned call
  // requires credentials that were deliberately omitted from durable state.
  auto owned = mcp_state_->Inspect(call.GetString("id"), access_);
  if (!owned.ok()) return owned;
  if (owned.value.GetBool("credentials_required") &&
      !ResponsesMcpHasCredentials(definition))
    return Error(400, "mcp_credentials_required",
                 "Resupply request credentials for this MCP server");
  auto status = output_.CallingMcp(index);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  auto saved = Checkpoint();
  if (!saved.ok() || saved.commit_unknown) return saved;
  Json arguments;
  if (!Parse(call.GetString("arguments"), &arguments))
    return Error(500, "mcp_state_invalid", "Owned call arguments invalid");
  const auto id = call.GetString("id");
  const auto catalog_id = owned.value.GetString("catalog_id");
  auto private_session = mcp_state_->Session(catalog_id, definition, access_);
  if (!private_session.ok()) return private_session;
  auto revision = private_session.value.GetInt("revision");
  McpSession session;
  session.state = *private_session.value.Find("session");
  Json published_session = session.state;
  session.checkpoint = [&](const Json& value) {
    auto saved = mcp_state_->BindSession(catalog_id, definition, revision,
                                         value, access_);
    if (saved.ok() && !saved.commit_unknown) {
      revision = saved.value.GetInt("revision");
      published_session = value;
    }
    return saved;
  };
  McpTransportReceipt receipt;
  const auto invoke = [&] {
    return InvokeMcp(
        {access_.scope, access_.deadline, access_.cancelled},
        definition.credentials, mcp_fetch_, "tools/call",
        Object({{"name", *call.Find("name")}, {"arguments", arguments}}),
        [&] {
          return mcp_state_->Permit(id, definition, access_, &session.state);
        },
        &receipt, {}, &session, cipher_config_.mcp_tunnels);
  };
  auto result = invoke();
  if (result.code == "mcp_session_binding_changed" && !receipt.call_permitted) {
    // The catalog/target was authenticated above. Newly supplied credentials
    // or deployment policy need a fresh session, never the previous token.
    // This preflight error performed no remote I/O or permission mutation.
    session.state = Json();
    result = invoke();
  }
  if (result.commit_unknown) return BindMutationResult(std::move(result));
  // The network request owns this single dispatch identity. A timeout or
  // disconnected subscriber cannot prevent bounded durable result closure.
  const StateAccess cleanup{
      access_.scope,
      std::chrono::steady_clock::now() + std::chrono::seconds(2),
      {}};
  auto completed =
      mcp_state_->Finish(id, receipt, result, cleanup,
                         receipt.call_permitted ? &session.state : nullptr);
  if (!completed.ok() || completed.commit_unknown)
    return BindMutationResult(std::move(completed));
  std::string wire;
  status = RecordOutput(output_.CompleteMcpItem(index, completed.value, &wire),
                        &wire);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  if (publish && !publish(wire))
    return Error(499, "request_cancelled", "MCP output subscriber cancelled");
  if (session.state.Has("protocol") &&
      OutputJson(published_session) != OutputJson(session.state)) {
    // Persist session loss only after the actual tool outcome is durable.
    // Storage failure here must never turn the known result into a replay.
    auto saved_session = mcp_state_->BindSession(
        catalog_id, definition, revision, session.state, cleanup);
    if (!saved_session.ok() || saved_session.commit_unknown)
      return BindMutationResult(std::move(saved_session));
  }
  return {};
}

ObjectReply ResponsesExecution::ExecuteProgramMcp(
    const ResponsesProgramState& program, const Json& intent,
    const std::function<bool(const std::string&)>& publish) {
  if (!mcp_state_ || intent.GetString("type") != kResponsesProgramMcpIntent)
    return Error(500, "mcp_state_invalid", "Missing MCP program executor");
  const auto& origin = *intent.Find("origin");
  const auto& call = *origin.Find("call");
  const auto tool =
      std::find_if(program.tools.array.begin(), program.tools.array.end(),
                   [&](const auto& value) {
                     return value.GetString("key") == call.GetString("key");
                   });
  if (tool == program.tools.array.end() || tool->GetString("type") != "mcp")
    return Error(403, "mcp_tool_denied",
                 "MCP tool is outside program authority");
  const auto& source = *tool->Find("mcp");
  ResponsesMcpDefinition original;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(*source.Find("declaration"), &original,
                                  &error))
    return Error(500, "mcp_state_invalid", "Invalid sealed MCP declaration");
  BindResponsesMcpCredentials(&original, mcp_credentials_);
  auto resolved = ResolveMcpDeploymentTarget(cipher_config_.mcp_policy.get(),
                                             access_.scope, &original);
  if (!resolved.ok()) return resolved;
  const auto current = std::find_if(
      mcp_definitions_.begin(), mcp_definitions_.end(), [&](const auto& value) {
        return value.declaration.GetString("server_label") ==
               source.GetString("server_label");
      });
  auto definition = current == mcp_definitions_.end() ? original : *current;
  resolved = ResolveMcpDeploymentTarget(cipher_config_.mcp_policy.get(),
                                        access_.scope, &definition);
  if (!resolved.ok()) return resolved;
  auto catalog =
      mcp_state_->CatalogById(source.GetString("catalog_id"), access_);
  if (!catalog.ok()) return catalog;
  auto authority = mcp_state_->OpenCatalog(catalog.value, original, access_);
  if (!authority.ok()) return authority;
  const auto& tools = authority.value.GetArray("tools")->array;
  const auto native =
      std::find_if(tools.begin(), tools.end(), [&](const auto& value) {
        return value.GetString("name") == tool->GetString("name");
      });
  if (native == tools.end() ||
      !ResponsesToolAllows(original.declaration, "programmatic") ||
      !ResponsesMcpToolAllowed(original.declaration, *native))
    return Error(403, "mcp_tool_denied", "Sealed program denies this MCP tool");
  // The invocation snapshot may tighten a catalog's approval policy. A later
  // request can tighten it again, but cannot weaken either original grant.
  if (ResponsesMcpApprovalRequired(original.declaration, *native))
    Set(&definition.declaration, "require_approval", String("always"));
  const auto limit = static_cast<uint64_t>(
      envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  if (native_tool_calls_ >= limit || native_tool_calls_ >= kMaxNativeToolCalls)
    return Error(503, "mcp_execution_capacity", "Native tool budget exhausted");
  Json arguments;
  if (!Parse(call.GetString("arguments"), &arguments))
    return Error(502, "program_arguments_invalid",
                 "Invalid MCP program arguments");
  const auto id = intent.GetString("id");
  auto owned = mcp_state_->Inspect(id, access_);
  ObjectReply proposed;
  bool terminal = false;
  if (owned.ok()) {
    std::string expected, actual;
    const auto* old = owned.value.Find("program_origin");
    if (!old || !InputValueIdentity(*old, &expected) ||
        !InputValueIdentity(origin, &actual) || expected != actual ||
        owned.value.GetString("catalog_id") != source.GetString("catalog_id"))
      return Error(409, "invalid_program_state",
                   "MCP program dispatch changed");
    if (owned.value.GetString("state") == "approval") {
      const auto& previous = *owned.value.Find("call");
      proposed.value = Object({{"type", String("mcp_approval_request")},
                               {"id", *previous.Find("approval_request_id")},
                               {"name", *previous.Find("name")},
                               {"server_label", *previous.Find("server_label")},
                               {"arguments", *previous.Find("arguments")}});
    } else {
      // An omitted history item never grants a second dispatch, even when the
      // request is reconstructed from the same sealed program fingerprint.
      proposed = mcp_state_->Recover(id, access_);
      if (!proposed.ok() || proposed.commit_unknown)
        return BindMutationResult(std::move(proposed));
      terminal = true;
    }
  } else if (owned.http_status == 404) {
    auto tracked = TrackMcpCall(id);
    if (!tracked.ok()) return tracked;
    proposed =
        mcp_state_->Propose(catalog.value, definition, tool->GetString("name"),
                            std::move(arguments), "programmatic", access_, id,
                            call.GetString("arguments"), &origin);
    if (!proposed.ok() || proposed.commit_unknown)
      return BindMutationResult(std::move(proposed));
  } else {
    return owned;
  }
  ++native_tool_calls_;
  const bool approval =
      proposed.value.GetString("type") == "mcp_approval_request";
  auto starting = proposed.value;
  if (terminal) {
    Set(&starting, "status", String("in_progress"));
    Set(&starting, "output", Json());
    Set(&starting, "error", Json());
  }
  size_t index = 0;
  std::string wire, part;
  auto status = output_.StartMcpItem(starting, &index, &wire);
  if (status && !approval) status = output_.CompleteMcpArguments(index, &part);
  wire += part;
  if (status && terminal) {
    if (proposed.value.GetString("status") == "completed")
      status = output_.CallingMcp(index);
    if (status) status = output_.CompleteMcpItem(index, proposed.value, &part);
    wire += part;
  }
  status = RecordOutput(status, &wire);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  auto saved = Checkpoint();
  if (!saved.ok() || saved.commit_unknown) return saved;
  if (publish && !publish(wire))
    return Error(499, "request_cancelled", "MCP program subscriber cancelled");
  return approval || terminal
             ? ObjectReply{}
             : ExecuteMcpCall(proposed.value, definition, index, publish);
}

ObjectReply ResponsesExecution::RunMcpApprovals(
    const std::function<bool(const std::string&)>& publish) {
  if (!begun_ || plan_.finished || commit_unknown_ || !mcp_state_ ||
      !PendingMcpResume())
    return Error(500, "response_state_invalid",
                 "No pending MCP approval input");
  const auto user_limit = static_cast<uint64_t>(
      envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  if (mcp_resume_.array.size() > kMaxNativeToolCalls - native_tool_calls_ ||
      mcp_resume_.array.size() >
          user_limit - std::min<uint64_t>(user_limit, native_tool_calls_))
    return Error(503, "mcp_execution_capacity", "Native tool budget exhausted");
  for (const auto& approval : mcp_resume_.array) {
    const auto id = approval.GetString("approval_request_id");
    // Inspect does not grant a dispatch. Never consume its private dispatch
    // token here: only this request's successful Permit callback can do that.
    auto original = mcp_state_->Inspect(id, access_);
    if (!original.ok()) return original;
    if (original.value.GetString("state") == "dispatching") {
      auto recovered = mcp_state_->Recover(id, access_);
      if (!recovered.ok() || recovered.commit_unknown)
        return BindMutationResult(std::move(recovered));
      original = mcp_state_->Inspect(id, access_);
      if (!original.ok()) return original;
    }
    const auto label = original.value.Find("call")->GetString("server_label");
    const auto current = std::find_if(
        mcp_definitions_.begin(), mcp_definitions_.end(),
        [&](const auto& value) {
          return value.declaration.GetString("server_label") == label;
        });
    ResponsesMcpDefinition definition;
    if (current != mcp_definitions_.end()) {
      definition = *current;
    } else {
      // A sealed program can continue without redeclaring its tools. Its
      // origin was authenticated before planning; secrets are still supplied
      // solely by the current request and are never recovered from storage.
      ChatPlanError error;
      if (original.value.GetString("caller") != "programmatic" ||
          !original.value.Find("program_origin") ||
          !PlanResponsesMcpDefinition(*original.value.Find("declaration"),
                                      &definition, &error))
        return Error(400, "mcp_definition_required",
                     "Resupply the MCP server declaration for approval");
      BindResponsesMcpCredentials(&definition, mcp_credentials_);
    }
    const bool approved = approval.GetBool("approve");
    if (approved && !original.value.Has("terminal")) {
      auto resolved = ResolveMcpDeploymentTarget(
          cipher_config_.mcp_policy.get(), access_.scope, &definition);
      if (!resolved.ok()) return resolved;
    }
    if (approved && !original.value.Has("terminal") &&
        original.value.GetBool("credentials_required") &&
        !ResponsesMcpHasCredentials(definition))
      return Error(400, "mcp_credentials_required",
                   "Resupply request credentials before deciding this call");
    auto tracked = TrackMcpCall(original.value.Find("call")->GetString("id"));
    if (!tracked.ok()) return tracked;
    Json terminal;
    ObjectReply decision;
    if (original.value.GetString("state") == "approval") {
      decision = mcp_state_->Decide(id, approved, approval.GetString("reason"),
                                    definition, access_);
      if (!decision.ok() || decision.commit_unknown)
        return BindMutationResult(std::move(decision));
      if (!approved) terminal = decision.value;
    } else {
      const auto* previous = original.value.Find("decision");
      if (!previous || previous->GetBool("approve") != approved)
        return Error(409, "mcp_approval_already_decided",
                     "Approval decision differs");
      if (const auto* value = original.value.Find("terminal"))
        terminal = *value;
      else if (original.value.GetString("state") != "ready")
        return Error(409, "mcp_result_pending",
                     "An existing MCP dispatch cannot be repeated");
    }
    auto call = *original.value.Find("call");
    Set(&call, "type", String("mcp_call"));
    Set(&call, "status", String("in_progress"));
    Set(&call, "output", Json());
    Set(&call, "error", Json());
    size_t index = 0;
    std::string wire, part;
    auto status = output_.StartMcpItem(call, &index, &wire);
    if (status) status = output_.CompleteMcpArguments(index, &part);
    wire += part;
    status = RecordOutput(status, &wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    if (publish && !publish(wire))
      return Error(499, "request_cancelled", "Approval subscriber cancelled");
    ++native_tool_calls_;
    if (!terminal.IsNull()) {
      // A retried approval returns the durable result without repeating I/O.
      // Calling is only the output lifecycle state; it conveys no permission.
      if (terminal.GetString("status") == "completed") {
        status = output_.CallingMcp(index);
        if (!status)
          return Error(503, "response_storage_exhausted", status.message());
      }
      status =
          RecordOutput(output_.CompleteMcpItem(index, terminal, &wire), &wire);
      if (!status)
        return Error(503, "response_storage_exhausted", status.message());
      if (publish && !publish(wire))
        return Error(499, "request_cancelled", "Approval subscriber cancelled");
    } else {
      auto result = ExecuteMcpCall(call, definition, index, publish);
      if (!result.ok()) return result;
    }
  }
  mcp_resume_ = Array();
  return {};
}

ObjectReply ResponsesExecution::RunMcp(
    size_t completed_calls, bool* client_wait,
    const std::function<bool(const std::string&)>& publish) {
  *client_wait = false;
  if (!begun_ || plan_.finished || commit_unknown_ || !mcp_state_ ||
      !PendingMcp())
    return Error(500, "response_state_invalid", "No executable MCP call");
  std::vector<Json> arguments;
  for (const auto& [index, pending] : mcp_pending_) {
    if (pending.dispatched || index >= completed_calls)
      return Error(409, "mcp_call_incomplete",
                   "Incomplete or already dispatched MCP call");
    Json value;
    if (!Parse(pending.call.arguments, &value) || !value.IsObject())
      return Error(502, "mcp_arguments_invalid", "Expected object arguments");
    arguments.push_back(std::move(value));
  }
  const auto user_limit = static_cast<uint64_t>(
      envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  if (mcp_pending_.size() > kMaxNativeToolCalls - native_tool_calls_ ||
      mcp_pending_.size() >
          user_limit - std::min<uint64_t>(user_limit, native_tool_calls_))
    return Error(503, "mcp_execution_capacity", "Native tool budget exhausted");
  const auto emit = [&](Status status, std::string* wire) -> ObjectReply {
    status = RecordOutput(status, wire);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    if (publish && !publish(*wire))
      return Error(499, "request_cancelled", "MCP output subscriber cancelled");
    return {};
  };
  size_t current = 0;
  for (auto& [index, pending] : mcp_pending_) {
    (void)index;
    const auto& binding = mcp_bindings_.at(pending.binding);
    const auto definition =
        std::find_if(mcp_definitions_.begin(), mcp_definitions_.end(),
                     [&](const auto& value) {
                       return value.declaration.GetString("server_label") ==
                              binding.server_label;
                     });
    const auto catalog =
        std::find_if(generation_history_.array.begin(),
                     generation_history_.array.end(), [&](const auto& item) {
                       return item.GetString("id") == binding.catalog_id;
                     });
    if (definition == mcp_definitions_.end() ||
        catalog == generation_history_.array.end())
      return Error(500, "mcp_state_invalid",
                   "Missing request-owned MCP binding");
    auto resolved = ResolveMcpDeploymentTarget(cipher_config_.mcp_policy.get(),
                                               access_.scope, &*definition);
    if (!resolved.ok()) return resolved;
    if (pending.public_id.empty())
      pending.public_id = detail::NewRequestId("mcp_");
    if (pending.public_id.empty())
      return Error(503, "mcp_entropy_unavailable", "MCP ID unavailable");
    auto tracked = TrackMcpCall(pending.public_id);
    if (!tracked.ok()) return tracked;
    pending.dispatched = true;
    auto proposed = mcp_state_->Propose(
        *catalog, *definition, binding.name, arguments[current++], "direct",
        access_, pending.public_id, pending.call.arguments);
    if (!proposed.ok() || proposed.commit_unknown)
      return BindMutationResult(std::move(proposed));
    ++native_tool_calls_;
    std::string wire;
    if (proposed.value.GetString("type") == "mcp_approval_request") {
      auto result = emit(
          output_.StartMcpItem(proposed.value, &pending.output_index, &wire),
          &wire);
      if (!result.ok()) return result;
      *client_wait = true;
      continue;
    }
    auto result =
        emit(output_.CompleteMcpArguments(pending.output_index, &wire), &wire);
    if (!result.ok()) return result;
    result = ExecuteMcpCall(proposed.value, *definition, pending.output_index,
                            publish);
    if (!result.ok()) return result;
  }
  mcp_pending_.clear();
  mcp_argument_bytes_ = 0;
  return {};
}
}  // namespace q4t::server
