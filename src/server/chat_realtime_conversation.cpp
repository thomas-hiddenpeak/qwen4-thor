#include "chat_server_internal.h"

#include <algorithm>

#include "chat_model_step.h"
#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/realtime_chat_execution.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_conversation_pump.h"
#include "q4t/server/realtime_file_inputs.h"
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
  HttpBodyLease memory(realtime_host_budget_);
  if (!memory.Acquire(kRealtimeConversationHostBytes) ||
      !runtime::ReadSystemMemory().CanAllocate(kRealtimeConversationHostBytes +
                                               (512ULL << 20))) {
    detail::SendError(fd, 503, "Realtime host memory unavailable", {},
                      "memory_pressure");
    return;
  }
  const size_t message_bytes =
      client.Scoped() ? std::min<size_t>(kWebSocketMessageLimit,
                                         client.Limits()->max_body_bytes)
                      : kWebSocketMessageLimit;
  const auto prepare = [&](const Json& current_settings, const Json& response,
                           const Json& items, const PromptSnapshot* prompt) {
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
    const auto status = PlanRealtimeChat(current_settings, response, items,
                                         planning, &plan, &failure, prompt);
    return status ? ObjectReply{} : Failure(status, failure);
  };
  RealtimeConversationCallbacks callbacks;
  callbacks.interrupted = [&] { return stop_requested_.load(); };
  callbacks.validate_prompt = [&](const RealtimePromptSelection& selected) {
    return prepare(selected.settings, Object(), Array(),
                   selected.prompt ? &*selected.prompt : nullptr);
  };
  callbacks.prepare = [&](const RealtimeConversationJob& job) {
    return prepare(job.settings, job.response, job.snapshot.items,
                   job.prompt ? &*job.prompt : nullptr);
  };
  callbacks.resolve_prompt = [&](const Json& reference, size_t budget,
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
  callbacks.acquire = [&](const std::string& id) {
    return requests_.Register(
        id, "", Clock::now() + std::chrono::milliseconds(request_deadline_ms_),
        client.Id());
  };
  callbacks.release = [&](const auto& control) { requests_.Remove(control); };
  callbacks.execute = [&](const auto& job, const auto& control,
                          const auto& emit) {
    return RunRealtimeConversation(client, job, control, emit);
  };
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
  std::string protocol;
  if (std::ranges::find(headers.websocket_protocols, "realtime") !=
      headers.websocket_protocols.end())
    protocol = "realtime";
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
    const RealtimeDeltaSink& emit) {
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
  auto status =
      PlanRealtimeChat(job.settings, job.response, job.snapshot.items, config,
                       &plan, &error, job.prompt ? &*job.prompt : nullptr);
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
    result = ExecuteRealtimeChat(
        plan.generation.generation_request, client, permit, control, emit,
        [&](NativeChatGeneration& native) {
          HandleChat(-1, {}, GenerationApi::kChat, control, nullptr, nullptr,
                     &native);
        },
        body_budget_,
        {&plan.generation.chat, accounting ? &*accounting : nullptr},
        &invocation);
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
  result.usage.reset();
  if (auto observed = preparation_context.Usage().Total()) {
    RealtimeResponseUsage usage;
    usage.total = *observed;
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
