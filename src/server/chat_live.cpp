#include "chat_server_internal.h"
#include "q4t/server/websocket_protocol.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/live_native_backend.h"
#include "q4t/server/live_responses_settings.h"
#include "q4t/server/live_session_pump.h"
#include "q4t/server/live_sideband.h"
#include "q4t/server/vector_stores.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using namespace object_detail;
ObjectReply Failure(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
}  // namespace

ObjectReply ChatServer::LoadLiveFork(int fd, const ClientIdentity& client,
                                     std::string_view source,
                                     std::shared_ptr<const Json>* state) {
  if (source.empty()) return {};
  if (!live_recordings_ || !objects_)
    return Error(404, "object_not_found", "Recording not found");
  if (!client.Scoped())
    return Error(401, "client_authentication_failed",
                 "Authentication required");
  const BinaryAccess access{
      "client:" + std::string(client.Id()),
      Clock::now() + std::chrono::milliseconds(request_deadline_ms_),
      [this, fd, client] {
        return stop_requested_.load() || !clients_.OwnsIdentity(client) ||
               detail::ClientConnectionFailed(fd);
      }};
  auto reply = ReadLiveForkState(*objects_, access, std::string(source));
  if (!reply.ok()) return reply;
  *state = std::make_shared<const Json>(std::move(reply.value));
  return {};
}

LiveSessionCallbacks ChatServer::MakeLiveCallbacks(
    const ClientIdentity& client, const std::string& id, size_t message_bytes,
    LiveTransport transport, std::shared_ptr<LiveSideband> sideband,
    std::shared_ptr<const Json> fork_state) {
  // Keep CPU asset preparation outside both socket and session event loops.
  const auto input = PrepareRealtimeInput(client);
  const auto current_planning = [this, client](ChatPlanConfig* planning,
                                               ClientIdentity* identity) {
    const auto current = clients_.RecoveryIdentity(client.Index());
    if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
        current.Id() != client.Id())
      return Error(401, "client_authentication_failed",
                   "Client no longer authorized");
    *planning = PlanningConfig(-1);
    *identity = current;
    planning->client_limits = identity->Limits();
    return ObjectReply{};
  };
  const auto count = [this, client](std::string_view text, bool literal,
                                    size_t* tokens, ChatPlanError* error) {
    if (!tokens || !error)
      return Status::Fail("Invalid bounded token count owner");
    *error = {};
    if (text.size() > (4ULL << 20)) {
      *error = {"live_input_capacity", "content", 413};
      return Status::Fail("Native token count input exceeds its byte limit");
    }
    const auto deadline = Clock::now() + std::chrono::seconds(3);
    const auto cancelled = [&] {
      return stop_requested_.load() || Clock::now() >= deadline ||
             !clients_.OwnsIdentity(client);
    };
    auto lock = LockUnlessCancelled(tok_mu_, cancelled);
    if (!lock.owns_lock() || !tok_) {
      *error = {"live_tokenizer_unavailable", "content", 503};
      return Status::Fail("Native token count could not acquire tokenizer");
    }
    std::vector<uint32_t> ids;
    const auto status = tok_->Encode(
        text, &ids,
        {.cancelled = cancelled, .recognize_special_tokens = !literal});
    if (!status || cancelled()) {
      *error = {"live_tokenizer_unavailable", "content", 503};
      return Status::Fail("Native token count did not complete");
    }
    *tokens = ids.size();
    return Status{};
  };
  LiveSessionCallbacks callbacks;
  callbacks.fork_state = fork_state;
  callbacks.configuration.transport = transport;
  callbacks.configuration.default_instructions =
      "You are a helpful voice assistant.";
  callbacks.configuration.default_voice = String("vivian");
  callbacks.configuration.count_text = [count](auto text, auto* tokens,
                                               auto* error) {
    return count(text, true, tokens, error);
  };
  callbacks.configuration.count_history = [current_planning, count](
                                              const Json& initial_items,
                                              size_t* tokens,
                                              ChatPlanError* error) {
    ChatPlanConfig planning;
    ClientIdentity identity;
    const auto current = current_planning(&planning, &identity);
    if (!current.ok()) {
      *error = {current.code, "input", current.http_status};
      return Status::Fail(current.message);
    }
    planning.count_input_only = true;
    Json mapped;
    auto status = MapLiveStartupHistory(initial_items, &mapped, error);
    if (!status) return status;
    auto settings = DefaultRealtimeChatSettings(planning, {});
    Set(&settings, "instructions", String(""));
    RealtimeChatPlan plan;
    status =
        PlanRealtimeChat(settings, Object(), mapped, planning, &plan, error);
    if (!status) return status;
    const auto& candidate = plan.generation;
    const auto& request = candidate.chat.Request(candidate.generation_request);
    std::string prompt;
    std::vector<VisionItem> media;
    status = PrepareChatPrompt(
        request, candidate.chat.constraints, &prompt, &media, &error->param,
        &error->code, nullptr, true, candidate.chat.async_tool_call_ids,
        candidate.chat.responses_tool_media, candidate.chat.HeaderTools(),
        candidate.chat.tool_call_formats, candidate.chat.prepared_source_data,
        candidate.chat.compress_images);
    if (!status) return status;
    if (!media.empty()) {
      *error = {"invalid_request", "session.input", 400};
      return Status::Fail("Live startup history must be text");
    }
    return count(prompt, false, tokens, error);
  };
  callbacks.configuration.validate = [this, current_planning, transport](
                                         const Json& configuration,
                                         ChatPlanError* error) {
    ChatPlanConfig planning;
    ClientIdentity identity;
    auto reply = current_planning(&planning, &identity);
    if (!reply.ok()) {
      *error = {reply.code, "session", reply.http_status};
      return Status::Fail(reply.message);
    }
    NativeLivePlan plan;
    return PlanNativeLiveClient(configuration, planning, &plan, error, true,
                                true, live_recordings_ && identity.Scoped(), transport);
  };
  callbacks.validate_responses = [this, current_planning, client](
                                     const Json& settings,
                                     std::stop_token stop) {
    if (stop.stop_requested())
      return Error(409, "cancelled", "Live settings validation cancelled");
    ChatPlanConfig planning;
    ClientIdentity identity;
    auto reply = current_planning(&planning, &identity);
    if (!reply.ok()) return reply;
    ChatPlanError error;
    const auto scope = identity.Scoped()
                           ? "client:" + std::string(identity.Id())
                           : "local:anonymous";
    const BinaryAccess access{
        scope, Clock::now() + std::chrono::seconds(3), [&] {
          return stop.stop_requested() || stop_requested_.load() ||
                 !clients_.OwnsIdentity(client);
        }};
    const auto status = CheckLiveResponsesSettings(
        settings, planning, scope, &error, [&](const std::string& id) {
          if (!objects_)
            return Error(503, "state_unavailable", "Vector store unavailable");
          return VectorStores(*objects_).Retrieve(access, id);
        });
    return status ? ObjectReply{} : Failure(status, error);
  };
  const auto counter = callbacks.configuration.count_text;
  callbacks.create = [this, client, input, current_planning, counter, id,
                      message_bytes, transport,
                      fork_state = std::move(fork_state),
                      sideband = std::move(sideband)](
                         const Json& configuration, const LiveEventSink& emit,
                         std::unique_ptr<ILiveSessionBackend>* backend) {
    ChatPlanConfig planning;
    ClientIdentity identity;
    auto reply = current_planning(&planning, &identity);
    if (!reply.ok()) return reply;
    NativeLivePlan plan;
    ChatPlanError error;
    const auto status =
        PlanNativeLiveClient(configuration, planning, &plan, &error, true, true,
                             live_recordings_ && identity.Scoped(), transport);
    if (!status) return Failure(status, error);
    LiveFrontendNative native;
    native.input = input;
    native.emit = emit;
    if (sideband)
      native.record = [sideband](LiveAudioChannel channel, uint64_t ticks,
                                 std::string_view bytes) {
        if (channel == LiveAudioChannel::kOutput) sideband->OutputAudio(ticks, bytes);
      };
    native.prepare = [current_planning](const RealtimeConversationJob& job) {
      ChatPlanConfig current;
      ClientIdentity identity;
      auto reply = current_planning(&current, &identity);
      if (!reply.ok()) return reply;
      RealtimeChatPlan prepared;
      ChatPlanError failure;
      const auto checked =
          PlanRealtimeChat(job.settings, job.response, job.snapshot.items,
                           current, &prepared, &failure, nullptr, true);
      return checked ? ObjectReply{} : Failure(checked, failure);
    };
    native.acquire = [this, client](const std::string& request) {
      return requests_.Register(
          request, "",
          Clock::now() + std::chrono::milliseconds(request_deadline_ms_),
          client.Id());
    };
    native.release = [this](const auto& control) { requests_.Remove(control); };
    native.execute = [this, client](const auto& job, const auto& control,
                                    const auto& delta, const auto& speech) {
      return RunRealtimeConversation(client, job, control, delta, speech);
    };
    LiveResponsesNative responses;
    responses.acquire = native.acquire;
    responses.release = native.release;
    responses.execute = [this, client](
                            const Json& settings, Json history,
                            const std::shared_ptr<RequestControl>& control,
                            NativeResponsesStream::Sink sink) {
      const auto current = clients_.RecoveryIdentity(client.Index());
      LiveResponsesResult failure;
      if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
          current.Id() != client.Id()) {
        failure.reply = Error(401, "client_authentication_failed",
                              "Client no longer authorized");
        return failure;
      }
      ClientLease permit;
      if (!permit.Acquire(clients_, current, ClientLane::kData)) {
        failure.reply = Error(429, "client_capacity_exhausted",
                              "Client request capacity exhausted");
        return failure;
      }
      return ExecuteLiveResponses(
          settings, std::move(history), current, permit, control, body_budget_,
          std::move(sink), [&](const std::string& body) {
            HandleChat(-1, body, GenerationApi::kResponse, control);
          });
    };
    LiveDecisionNative decisions;
    decisions.acquire = native.acquire;
    decisions.release = native.release;
    decisions.execute = [this, client](
                            const Json& request,
                            const std::shared_ptr<RequestControl>& control) {
      const auto current = clients_.RecoveryIdentity(client.Index());
      if (!clients_.OwnsIdentity(client) || !current.Allowed() ||
          current.Id() != client.Id())
        return Error(401, "client_authentication_failed",
                     "Client no longer authorized");
      ClientLease permit;
      if (!permit.Acquire(clients_, current, ClientLane::kData))
        return Error(429, "client_capacity_exhausted",
                     "Client request capacity exhausted");
      return ExecuteLiveDecision(request, current, permit, control,
                                 body_budget_, [&](const std::string& body) {
                                   HandleClassification(
                                       -1, body, ClassificationApi::kDecisions,
                                       control);
                                 });
    };
    std::unique_ptr<LiveRecordingCapture> recording;
    if (configuration.GetBool("store")) {
      auto recording_memory =
          std::make_unique<HttpBodyLease>(realtime_host_budget_);
      if (!recording_memory->Acquire(kLiveRecordingHostBytes) ||
          !runtime::ReadSystemMemory().CanAllocate(kLiveRecordingHostBytes +
                                                   (512ULL << 20)))
        return Error(503, "memory_pressure",
                     "Live recording memory unavailable");
      const auto scope = "client:" + std::string(identity.Id());
      recording = std::make_unique<LiveRecordingCapture>(
          *objects_, scope, id, plan.format,
          [this, client] {
            return stop_requested_.load() || !clients_.OwnsIdentity(client);
          },
          std::move(recording_memory));
    }
    auto selected = std::make_unique<NativeLiveClientBackend>(
        id, std::move(native), LiveClientContextCallbacks{counter, emit, {}},
        asr_limits_.max_audio_seconds, message_bytes, std::move(responses),
        std::move(decisions), std::move(recording));
    auto* owner = selected.get();
    *backend =
        std::move(selected);  // Cleanup is owned even if preparation fails.
    return owner->Prepare(configuration, std::move(plan), fork_state.get());
  };
  callbacks.interrupted = [this] { return stop_requested_.load(); };
  return callbacks;
}

void ChatServer::HandleLive(int fd, std::string_view path,
                            const HttpRequestHeaders& headers,
                            std::string_view initial, std::string_view accept) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  const auto source = LiveForkId(path);
  if (path != "/v1/live/sessions" && source.empty()) {
    detail::SendError(fd, 400, "Primary Live startup uses session.start", {},
                      "invalid_request", "path");
    return;
  }
  if (asr_model_dir_.empty() || tts_model_dir_.empty()) {
    detail::SendError(fd, 503, "Native Live requires ASR and speech assets", {},
                      "live_processor_unavailable");
    return;
  }
  if (RejectUnhealthy(fd) ||
      !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
    return;
  const auto client = http->Client();
  const size_t host_bytes = kRealtimeConversationHostBytes +
                            kRealtimeConversationInputHostBytes +
                            (source.empty() ? 0 : 3 * kLiveForkStateBytes);
  HttpBodyLease memory(realtime_host_budget_);
  if (!memory.Acquire(host_bytes) ||
      !runtime::ReadSystemMemory().CanAllocate(host_bytes + (512ULL << 20))) {
    detail::SendError(fd, 503, "Live host memory unavailable", {},
                      "memory_pressure");
    return;
  }
  const size_t message_bytes =
      client.Scoped() ? std::min<size_t>(kWebSocketMessageLimit,
                                         client.Limits()->max_body_bytes)
                      : kWebSocketMessageLimit;
  const auto id = detail::NewRequestId("live_");
  std::shared_ptr<const Json> state;
  if (auto reply = LoadLiveFork(fd, client, source, &state); !reply.ok()) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode());
    return;
  }
  auto callbacks =
      MakeLiveCallbacks(client, id, message_bytes, LiveTransport::kWebSocket,
                        {}, std::move(state));
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 64,
                         kWebSocketMaximumLimit);
  LiveSessionLimits session_limits;
  session_limits.message_bytes = message_bytes;
  // A 59 minute native lifetime leaves room for its 45s drain and 15s output
  // deadline before the outer transport's independent one-hour hard bound.
  session_limits.lifetime = std::chrono::seconds(3540);
  // A full 30s fixture may follow an interrupted borrower's bounded cleanup.
  session_limits.drain = std::chrono::seconds(45);
  const auto expires =
      uint64_t(std::chrono::duration_cast<std::chrono::seconds>(
                   std::chrono::system_clock::now().time_since_epoch())
                   .count()) +
      3540;
  LiveSessionPump session(id, expires, output, std::move(callbacks),
                          session_limits);
  if (!session.Start()) {
    detail::SendError(fd, 503, "Native Live session initialization failed", {},
                      "live_session_unavailable");
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
  connection.stopped = [this] { return stop_requested_.load(); };
  connection.cancel = [&] { session.Stop(); };
  connection.tick = [&] { return session.Alive(); };
  connection.completed = [&] { return session.Finalized(); };
  connection.message_ready = [&](size_t bytes) {
    return session.CanAccept(bytes);
  };
  WebSocketSessionLimits limits;
  limits.message_bytes = message_bytes;
  RunWebSocketSession(fd, initial, output, connection, limits);
  session.Join();
}
}  // namespace q4t::server
