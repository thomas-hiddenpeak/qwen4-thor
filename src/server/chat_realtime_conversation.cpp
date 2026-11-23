#include "chat_server_internal.h"
#include "q4t/server/websocket_protocol.h"

#include <algorithm>

#include "chat_model_step.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/realtime_chat_execution.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_conversation_pump.h"
#include "q4t/server/realtime_file_inputs.h"
#include "q4t/server/realtime_mcp_history.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using namespace object_detail;
ObjectReply Failure(const Status& status, const ChatPlanError& error) {
  auto result = Error(error.http_status, error.code, status.message());
  result.value = Object({{"param", String(error.param)}});
  return result;
}
}  // namespace

RealtimeConversationCallbacks ChatServer::MakeRealtimeCallbacks(
    const ClientIdentity& client) {
  const bool native_input = !asr_model_dir_.empty();
  const auto prepare = [this, client, native_input](const Json& current_settings, const Json& response,
                           const Json& items, const PromptSnapshot* prompt,
                           const RealtimeMcpContext* mcp,
                           bool definitions_only) {
    const auto current = clients_.RecoveryIdentity(client.Index());
    if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
        current.Id() != client.Id())
      return Error(401, "client_authentication_failed",
                   "Client no longer authorized");
    const auto scope = current.Scoped() ? "client:" + std::string(current.Id())
                                        : "local:anonymous";
    if (prompt && prompt->scope != scope)
      return Error(401, "prompt_scope_mismatch",
                   "Prompt belongs to another client");
    auto planning = PlanningConfig(-1);
    planning.client_limits = current.Limits();
    RealtimeChatPlan plan;
    ChatPlanError failure;
    Json history;
    auto reply = PreviewRealtimeMcpHistory(items, &history);
    if (!reply.ok() || reply.commit_unknown) return reply;
    const auto status =
        PlanRealtimeChat(current_settings, response, items, planning, &plan,
                         &failure, prompt, native_input, mcp,
                         definitions_only, &history);
    return status ? ObjectReply{} : Failure(status, failure);
  };
  RealtimeConversationCallbacks callbacks;
  callbacks.traces = [this] { return realtime_traces_.Get(); };
  callbacks.trace_scope = client.Scoped()
                              ? "client:" + std::string(client.Id())
                              : "local:anonymous";

  if (native_input) {
    callbacks.input = PrepareRealtimeInput(client);
    callbacks.max_audio_seconds = asr_limits_.max_audio_seconds;
  }
  callbacks.interrupted = [this] { return stop_requested_.load(); };
  callbacks.validate_prompt = [prepare](const RealtimePromptSelection& selected) {
    return prepare(selected.settings, Object(), Array(),
                   selected.prompt ? &*selected.prompt : nullptr,
                   selected.mcp.get(), true);
  };
  callbacks.prepare = [prepare](const RealtimeConversationJob& job) {
    return prepare(job.settings, job.response, job.snapshot.items,
                   job.prompt ? &*job.prompt : nullptr, job.mcp.get(),
                   job.mcp_definitions_only);
  };
  callbacks.mcp_import.emplace();
  callbacks.mcp_import->import =
      [this, client](const ResponsesMcpDefinition& definition, std::string id,
          Clock::time_point deadline, const std::function<bool()>& cancelled,
          RealtimeMcpCatalog* catalog) {
        const auto current = clients_.RecoveryIdentity(client.Index());
        if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
            current.Id() != client.Id())
          return Error(401, "client_authentication_failed",
                       "Client no longer authorized");
        if (!objects_)
          return Error(503, "mcp_state_unavailable", "MCP storage unavailable");
        const StateAccess access{
            current.Scoped() ? "client:" + std::string(current.Id())
                             : "local:anonymous",
            deadline,
            [&] {
              return cancelled() || stop_requested_.load() ||
                     !clients_.OwnsIdentity(client);
            }};
        return ImportRealtimeMcpCatalog(*objects_, PlanningConfig(-1),
                                         definition, access, std::move(id),
                                         catalog);
      };
  callbacks.mcp_call.emplace();
  callbacks.mcp_call->run =
      [this, client](const RealtimeMcpCallJob& job, Clock::time_point deadline,
          const std::function<bool()>& cancelled, RealtimeMcpExecution* result) {
        const bool cleanup = job.action == RealtimeMcpAction::kAbandon;
        const auto current = clients_.RecoveryIdentity(client.Index());
        if (!cleanup && (!clients_.OwnsIdentity(client) || !current.Allowed() ||
                         current.Id() != client.Id()))
          return Error(401, "client_authentication_failed",
                       "Client no longer authorized");
        if (!objects_)
          return Error(503, "mcp_state_unavailable", "MCP storage unavailable");
        // Cleanup keeps the original scope after key revocation/disconnect.
        // The native helper proves process/response ownership and cannot send
        // a remote call. Ordinary operations always recheck current identity.
        const StateAccess access{
            client.Scoped() ? "client:" + std::string(client.Id())
                            : "local:anonymous",
            deadline,
            [&] {
              return !cleanup && (cancelled() || stop_requested_.load() ||
                                   !clients_.OwnsIdentity(client));
            }};
        auto planning = PlanningConfig(-1);
        switch (job.action) {
          case RealtimeMcpAction::kPropose:
            return ProposeRealtimeMcpCall(*objects_, planning, job.catalog,
                                          job.generated, job.response_id, access);
          case RealtimeMcpAction::kDecide:
            return DecideRealtimeMcpCall(
                *objects_, planning, job.catalog.definition, job.generated,
                job.decision, job.response_id, access);
          case RealtimeMcpAction::kExecute:
            return ExecuteRealtimeMcpCall(
                *objects_, planning, job.catalog.definition, job.native_call,
                job.response_id, access, result);
          case RealtimeMcpAction::kAbandon:
            return AbandonRealtimeMcpCall(
                *objects_, planning, job.catalog.definition, job.generated,
                job.response_id, access);
        }
        return Error(500, "realtime_mcp_action_invalid", "Invalid MCP action");
      };
  callbacks.resolve_prompt = [this, client](const Json& reference, size_t budget,
                                 const std::function<bool()>& cancelled,
                                 PromptSnapshot* snapshot) {
    const auto current = clients_.RecoveryIdentity(client.Index());
    if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
        current.Id() != client.Id())
      return Error(401, "client_authentication_failed",
                   "Client no longer authorized");
    // The socket reader remains independent. Do not hold the session owner
    // through unbounded object I/O or inherit a model-length deadline here.
    const auto deadline =
        Clock::now() + std::chrono::milliseconds(request_deadline_ms_);
    const StateAccess access{current.Scoped()
                                 ? "client:" + std::string(current.Id())
                                 : "local:anonymous",
                             deadline, cancelled};
    PromptSnapshot selected;
    auto reply = ResolveRealtimePrompt(objects_.get(), reference, access,
                                       budget, &selected);
    if (!reply.ok() || reply.commit_unknown) return reply;
    const auto latest = clients_.RecoveryIdentity(client.Index());
    if (!clients_.OwnsIdentity(client) || !latest.Allowed() ||
        latest.Id() != current.Id())
      return Error(401, "client_authentication_failed",
                   "Client no longer authorized");
    *snapshot = std::move(selected);
    return reply;
  };
  callbacks.acquire = [this, client](const std::string& id) {
    return requests_.Register(
        id, "", Clock::now() + std::chrono::milliseconds(request_deadline_ms_),
        client.Id());
  };
  callbacks.release = [this](const auto& control) { requests_.Remove(control); };
  callbacks.execute = [this, client](const auto& job, const auto& control,
                          const auto& emit, const auto& speech) {
    return RunRealtimeConversation(client, job, control, emit, speech);
  };
  return callbacks;
}

void ChatServer::HandleRealtimeConversation(int fd, std::string_view path,
                                            const HttpRequestHeaders& headers,
                                            std::string_view initial,
                                            std::string_view accept) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  const auto client = http->Client();
  auto config = PlanningConfig(fd);
  Json settings;
  ChatPlanError error;
  auto checked = ParseRealtimeChatTarget(path, config, &settings, &error);
  RealtimeChatPlan prepared;
  if (checked)
    checked = PlanRealtimeChat(settings, Object(), Array(), config, &prepared,
                               &error);
  if (!checked) {
    detail::SendError(fd, error.http_status, checked.message(), {}, error.code,
                      error.param);
    return;
  }
  if (RejectUnhealthy(fd) ||
      !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
    return;
  const bool native_input = !asr_model_dir_.empty();
  const auto host_bytes =
      kRealtimeConversationHostBytes +
      (native_input ? kRealtimeConversationInputHostBytes : 0);
  HttpBodyLease memory(realtime_host_budget_);
  if (!memory.Acquire(host_bytes) ||
      !runtime::ReadSystemMemory().CanAllocate(host_bytes + (512ULL << 20))) {
    detail::SendError(fd, 503, "Realtime host memory unavailable", {},
                      "memory_pressure");
    return;
  }
  const size_t message_bytes =
      client.Scoped() ? std::min<size_t>(kWebSocketMessageLimit,
                                         client.Limits()->max_body_bytes)
                      : kWebSocketMessageLimit;
  if (native_input) {
    // Manual input is usable even if the optional VAD assets are unavailable.
    // VAD is selected explicitly and validated by the actual shared backend.
    Set(&Field(settings, "audio"), "input",
        Object({{"format", Object({{"type", String("audio/pcm")},
                                   {"rate", Number(24000)}})},
                {"transcription", Json()},
                {"turn_detection", Json()}}));
  }
  auto callbacks = MakeRealtimeCallbacks(client);
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 64,
                         kWebSocketMaximumLimit);
  RealtimeConversationPump session(
      detail::NewRequestId("sess_"), detail::NewRequestId("conv_"),
      std::move(settings), output, std::move(callbacks), message_bytes);
  if (!session.Start()) {
    detail::SendError(fd, 503, "Realtime session initialization failed", {},
                      "realtime_session_unavailable");
    return;
  }
  const auto protocol =
      SelectWebSocketProtocol(path, headers.websocket_protocols);
  if (!http->BeginWebSocket(accept, protocol)) return;
  WebSocketSessionCallbacks connection;
  connection.message = [&](auto opcode, auto message) {
    const auto current = clients_.RecoveryIdentity(client.Index());
    if (current.Scoped() && message.size() > current.Limits()->max_body_bytes)
      return false;
    return session.Message(opcode, message);
  };
  connection.authorized = [&] {
    const auto current =
        clients_.Authenticate(headers.client_id, headers.gateway_token);
    return current.Allowed() && current.Id() == client.Id();
  };
  connection.stopped = [&] { return stop_requested_.load(); };
  connection.cancel = [&] { session.Stop(); };
  connection.tick = [&] { return session.Alive(); };
  connection.message_ready = [&](size_t bytes) {
    return session.CanAccept(bytes);
  };
  WebSocketSessionLimits limits;
  limits.message_bytes = message_bytes;
  RunWebSocketSession(fd, initial, output, connection, limits);
  session.Join();
}

RealtimeGenerationResult ChatServer::RunRealtimeConversation(
    const ClientIdentity& original, const RealtimeConversationJob& job,
    const std::shared_ptr<RequestControl>& control,
    const RealtimeDeltaSink& emit, const RealtimeSpeechSink& speech) {
  RealtimeGenerationResult result;
  const auto client = clients_.RecoveryIdentity(original.Index());
  if (!clients_.OwnsIdentity(original) || !client.Allowed() ||
      client.Id() != original.Id()) {
    result.reply = Error(401, "client_authentication_failed",
                         "Client no longer authorized");
    return result;
  }
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (job.prompt && job.prompt->scope != scope) {
    result.reply =
        Error(401, "prompt_scope_mismatch", "Prompt belongs to another client");
    return result;
  }
  ClientLease permit;
  if (!permit.Acquire(clients_, client, ClientLane::kData)) {
    result.reply = Error(429, "client_capacity_exhausted",
                         "Client request capacity exhausted");
    return result;
  }
  auto config = PlanningConfig(-1);
  config.client_limits = client.Limits();
  RealtimeChatPlan plan;
  ChatPlanError error;
  Status status;
  RequestDiagnostics diagnostic(stderr, control->id());
  diagnostic.Bind(control->id());
  diagnostic.Stage("context_prepare");
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return control->Cancelled();
  };
  // One authenticated parent owns preparation usage even when a child throws
  // or is cancelled. The primary Chat capture is nested and merged once below.
  detail::HttpResponseContext preparation_context(
      detail::HttpResponseContext::Detached{-1});
  preparation_context.SetClient(client, &permit);
  preparation_context.SetTraceId(control->id());
  preparation_context.Usage().Enable();
  ClientRateLease preparation_rate;
  std::optional<ModelPreparation> accounting;
  RealtimeInvocation invocation(control);
  const auto run = [&]() -> RealtimeGenerationResult {
    if (!invocation.Ready(control)) {
      result.reply =
          Error(409,
                control->Cancelled() ? control->Reason()
                                     : "realtime_execution_already_claimed",
                "Response execution unavailable");
      return result;
    }
    if (job.mcp_definitions_only) {
      result.reply = Error(500, "realtime_mcp_preview_invalid",
                           "A validation preview cannot execute");
      return result;
    }
    Json mcp_history;
    const StateAccess access{scope, control->deadline(), cancelled};
    result.reply = ResolveRealtimeMcpHistory(objects_.get(), config,
                                              job.snapshot, access,
                                              &mcp_history);
    if (!result.reply.ok() || result.reply.commit_unknown) return result;
    auto prompt = job.prompt;
    if (prompt && prompt->request.GetArray("input")) {
      auto& prefix = Field(prompt->request, "input");
      for (auto& item : prefix.array) {
        if (!item.GetString("type").starts_with("mcp_")) continue;
        if (!objects_) {
          result.reply = Error(503, "mcp_state_unavailable",
                               "MCP prompt history storage unavailable");
          return result;
        }
        result.reply = ResponsesMcpState(*objects_, config).Authenticate(item,
                                                                        access);
        if (!result.reply.ok() || result.reply.commit_unknown) return result;
        item = std::move(result.reply.value);
      }
    }
    status = PlanRealtimeChat(job.settings, job.response, job.snapshot.items,
                              config, &plan, &error,
                              prompt ? &*prompt : nullptr, job.native_input,
                              job.mcp.get(), false, &mcp_history);
    if (status && !plan.input_ready) {
      diagnostic.Stage("input_prepare");
      RealtimeFileInputs files;
      auto reply = files.Prepare(plan, config, objects_.get(),
                                 {scope, control->deadline(), cancelled});
      if (reply.commit_unknown) {
        if (reply.code.empty()) reply.code = "object_commit_unknown";
        result.reply = std::move(reply);
        return result;
      }
      if (reply.ok() && files.Files()->PendingSheets()) {
        accounting.emplace(clients_, client, preparation_rate,
                           plan.envelope.options.GetInt("max_output_tokens"), 0,
                           files.Files()->PendingOutputBudget());
        while (reply.ok() && files.Files()->PendingSheets()) {
          FileSheetStep sheet;
          reply = files.Files()->NextSheet(config, &sheet);
          if (!reply.ok()) break;
          InternalModelStep work{
              sheet.request,
              config,
              *accounting,
              [&] {
                if (cancelled() || !invocation.Ready(control))
                  return Error(409, control->Reason(),
                               "Input preparation cancelled");
                if (!clients_.OwnsIdentity(client))
                  return Error(401, "client_authentication_failed",
                               "Client no longer authorized");
                return ObjectReply{};
              },
              {},
              false,
              {},
              {},
              0,
              0};
          reply = RunInternalModelStep(-1, &work, control);
          if (reply.ok())
            reply = files.Files()->CompleteSheet(
                sheet, work.content, work.finish_reason, work.input_tokens,
                work.output_tokens);
        }
      }
      if (reply.ok()) reply = files.Finish(config, &plan);
      if (!reply.ok()) {
        result.reply = std::move(reply);
        return result;
      }
    }
    if (status)
      status = FitRealtimeChatContext(
          config,
          [&](const ResponsesGenerationPlan& candidate, size_t* count,
              ChatPlanError* failure) -> Status {
            const auto& request =
                candidate.chat.Request(candidate.generation_request);
            std::string prompt;
            std::vector<VisionItem> media;
            auto measured = PrepareChatPrompt(
                request, candidate.chat.constraints, &prompt, &media,
                &failure->param, &failure->code, nullptr, false,
                candidate.chat.async_tool_call_ids,
                candidate.chat.responses_tool_media,
                candidate.chat.HeaderTools(), candidate.chat.tool_call_formats,
                candidate.chat.prepared_source_data);
            if (!measured) return measured;
            std::vector<uint32_t> tokens;
            {
              auto lock = diagnostic.Lock(
                  tok_mu_, RequestDiagnostics::Wait::kTokenizer, cancelled);
              if (!lock.owns_lock()) {
                *failure = {control->Reason(), "input", 409};
                return Status::Fail("Context measurement cancelled");
              }
              measured =
                  tok_->Encode(prompt, &tokens, {.cancelled = cancelled});
            }
            if (measured && !media.empty()) {
              vision::MediaPlan expanded;
              ChatPromptBudget budget;
              const auto& model = model_.Get().cfg;
              auto context = config;
              context.client_limits = nullptr;
              measured = ExpandChatMedia(
                  request, media, model.image_token_id, model.video_token_id,
                  proc_cfg_, video_proc_cfg_, vision_device_budget_, context,
                  &tokens, &expanded, &budget, failure);
            }
            if (cancelled()) {
              *failure = {control->Reason(), "input", 409};
              return Status::Fail("Context measurement cancelled");
            }
            if (measured) *count = tokens.size();
            return measured;
          },
          &plan, &error);
    if (!status) {
      diagnostic.Outcome(control->Cancelled() ? "cancelled" : "error",
                         error.code);
      result.reply = Failure(status, error);
      return result;
    }
    diagnostic.Stage("execute");
    std::optional<RealtimeSpeechExecution> delivery;
    ActualUsageCounter speech_usage;
    NativeChatSpeech native_speech;
    if (plan.speech.enabled) {
      delivery.emplace(plan.speech, speech, cancelled);
      if (!delivery->Valid()) {
        result.reply = Error(503, "realtime_speech_owner_unavailable",
                             "Native speech delivery unavailable");
        return result;
      }
      // Add native output only after file/context preparation has preserved
      // its existing private proofs. Public JSON cannot create these observers.
      plan.generation.chat.audio_output = plan.speech.chat;
      speech_usage.Enable();
      native_speech.usage = &speech_usage;
      native_speech.append = [&](std::string_view pcm) {
        return delivery->Append(pcm);
      };
      native_speech.complete = [&](std::string_view transcript,
                                   const SpeechEncodingReceipt& receipt,
                                   const ActualUsage& usage) {
        return delivery->Finish(transcript, receipt, usage);
      };
    }
    result = ExecuteRealtimeChat(
        plan.generation.generation_request, client, permit, control, emit,
        [&](NativeChatGeneration& native) {
          HandleChat(-1, {}, GenerationApi::kChat, control, nullptr, nullptr,
                     &native);
        },
        body_budget_,
        {&plan.generation.chat, accounting ? &*accounting : nullptr,
         delivery ? &native_speech : nullptr},
        &invocation);
    if (result.usage && delivery) {
      if (const auto observed = speech_usage.Total())
        result.usage->output_audio = observed->output;
    }
    preparation_context.Usage().Merge(
        result.usage ? std::optional<ActualUsage>(result.usage->total)
                     : std::nullopt);
    if (result.reply.ok() && plan.generation.truncated_items &&
        job.in_conversation && job.snapshot.default_context)
      result.retained_input_indices = RealtimeRetainedHistory(plan);
    return result;
  };
  try {
    result = run();
  } catch (const std::bad_alloc&) {
    result.reply = Error(503, "memory_pressure",
                         "Realtime input preparation memory unavailable");
  } catch (...) {
    result.reply = Error(500, "realtime_input_preparation_failed",
                         "Realtime input preparation failed");
  }
  // This also settles completed preparation when context fitting or primary
  // admission failed. No response terminal is published before settlement.
  preparation_rate.Settle();
  const auto audio_tokens =
      result.usage ? result.usage->output_audio : std::nullopt;
  result.usage.reset();
  if (auto observed = preparation_context.Usage().Total()) {
    RealtimeResponseUsage usage;
    usage.total = *observed;
    if (audio_tokens && *audio_tokens <= observed->output) {
      usage.output_audio = audio_tokens;
      usage.output_text = observed->output - *audio_tokens;
    }
    result.usage = usage;
  }
  if (!result.reply.ok()) result.state = RealtimeResponseEnd::State::kFailed;
  diagnostic.Outcome(control->Cancelled() ? "cancelled"
                     : result.reply.ok()  ? "success"
                                          : "error",
                     control->Cancelled() ? control->Reason()
                     : result.reply.ok()  ? "completed"
                                          : result.reply.ClientCode());
  return result;
}
}  // namespace q4t::server
