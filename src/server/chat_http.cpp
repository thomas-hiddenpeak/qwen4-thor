// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"
#include "object_protocol.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/realtime_client_ticket.h"
#include "q4t/server/realtime_trace_api.h"
#include "q4t/server/mcp_transport.h"

#include <new>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <thread>
#include "q4t/server/http_request.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"
#include "q4t/server/client_request.h"
#include "q4t/server/conversation_api.h"
#include "q4t/server/assistant_registry.h"
#include "q4t/server/agent_registration.h"
#include "q4t/server/agent_vault.h"
#include "q4t/server/agent_environment.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/files_http.h"
#include "q4t/server/agent_automation.h"
#include "q4t/server/evals.h"
#include "q4t/server/chatkit.h"
#include "chatkit_attachments.h"
#include "q4t/server/safety_records.h"
#include "q4t/server/video_characters.h"
#include "q4t/server/webhook.h"
#include "q4t/server/live_recording_store.h"
#include "q4t/server/batch_api.h"
#include "q4t/server/vector_store_api.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/output_schema.h"

namespace q4t::server {
using detail::SendError;
using detail::SendSimple;

namespace detail {
bool RequestCancelled(RequestControl* control, int fd) {
  if (control->Cancelled()) return true;
  if (ClientConnectionFailed(fd))
    control->Cancel(RequestControl::State::kDisconnect);
  return control->Cancelled();
}

bool ValidRequestKey(const std::string& key) { return ValidCancelToken(key); }

}  // namespace detail

void ChatServer::FinishConnection(int fd, bool operations) noexcept {
  DrainHttpClose(fd);
  {
    const std::lock_guard<std::mutex> lock(connections_mu_);
    connections_.erase(fd);
    ::close(fd);
  }
  (operations ? operations_conns_ : public_conns_).fetch_sub(1);
  // The last access to server-owned state publishes all handler cleanup.
  active_conns_.fetch_sub(1, std::memory_order_release);
}

void ChatServer::RequestStop() {
  static_assert(std::atomic<bool>::is_always_lock_free);
  stop_requested_.store(true);
  background_.PreventStart();
  realtime_traces_.PreventStart();
  environment_preparer_.PreventStart();
  eval_dispatcher_.PreventStart();
  webhook_dispatcher_.PreventStart();
  batches_.PreventStart();
  vector_jobs_.PreventStart();
  background_scanner_.PreventStart();
}

void ChatServer::StartConnection(int fd, bool operations) {
  bool counted = false, registered = false;
  ScopeExit rollback_connection([&] {
    if (registered) {
      FinishConnection(fd, operations);
    } else {
      DrainHttpClose(fd);
      ::close(fd);
      if (counted) {
        (operations ? operations_conns_ : public_conns_).fetch_sub(1);
        active_conns_.fetch_sub(1, std::memory_order_release);
      }
    }
  });
  int nodelay = 1;
  ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
#ifdef Q4T_SERVE_FAULT_INJECTION
  if (std::getenv("Q4T_SERVE_TEST_SNDBUF")) {
    const int small_buffer = 1024;
    ::setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &small_buffer,
                 sizeof(small_buffer));
  }
#endif
  const timeval timeout{30, 0};
  ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  auto& handlers = operations ? operations_conns_ : public_conns_;
  const int limit =
      operations
          ? kOperationsConnections
          : conn_cap_ + kControlConnections + kObservationConnections +
                kRealtimeConnections + kSidebandConnections +
                (mcp_tunnels_enabled_
                     ? kTunnelPollConnections + kTunnelResponseConnections
                     : 0);
  auto& rejected = operations ? metrics_.operations_connection_rejections
                              : metrics_.connection_rejections;
  if (!AcquireHttpConnection(handlers, limit)) {
    rejected[static_cast<size_t>(HttpConnectionClass::kUnclassified)].fetch_add(
        1);
    detail::HttpResponseContext context(fd, &metrics_.http_transport);
    context.RecordReceiveFailure(HttpReceiveFailure::kConnectionCapacity, 503);
    SendError(
        fd, 503, "connection capacity exhausted",
        {std::chrono::steady_clock::now() + std::chrono::milliseconds(250)},
        operations ? "operations_capacity_exhausted" : "capacity_exhausted");
    // No handler ownership was acquired for the rejected connection.
    return;
  }
  active_conns_.fetch_add(1, std::memory_order_relaxed);
  counted = true;
  {
    const std::lock_guard<std::mutex> lock(connections_mu_);
    connections_.insert(fd);
    registered = true;
  }
  detail::ServeFaultPoint("connection.before_thread");
  std::thread handler([this, fd, operations]() {
    ScopeExit connection_cleanup([&] { FinishConnection(fd, operations); });
    try {
      HandleClient(fd, operations);
    } catch (const std::bad_alloc&) {
      detail::SendExceptionResponse(fd, detail::ResponseState::kUnsent,
                                    detail::ExceptionKind::kHostMemory);
    } catch (const std::exception& error) {
      std::fprintf(stderr, "[q4t] HTTP handler exception: %s\n", error.what());
      detail::SendExceptionResponse(fd, detail::ResponseState::kUnsent);
    } catch (...) {
      std::fprintf(stderr, "[q4t] unknown HTTP handler exception\n");
      detail::SendExceptionResponse(fd, detail::ResponseState::kUnsent);
    }
  });
  rollback_connection.Release();  // The constructed thread now owns fd.
  try {
    handler.detach();
  } catch (...) {
    // Never destroy a still-joinable std::thread during unwinding.
    RequestStop();
    {
      const std::lock_guard<std::mutex> lock(connections_mu_);
      if (connections_.contains(fd)) ::shutdown(fd, SHUT_RDWR);
    }
    handler.join();
    throw;
  }
}

Status ChatServer::Run() {
  // Take startup ownership even when stopped before Run. The local owner also
  // covers exceptions before publication and destruction after an early exit.
  HttpListeners listeners = std::move(startup_listeners_);
  const int listen_fd = listeners.Public();
  const int operations_fd = listeners.Operations();
  ScopeExit close_listener([&] {
    listen_fd_.store(-1);
    operations_listen_fd_.store(-1);
    listeners.Close();
  });
  if (stop_requested_.load())
    return serving_failed_.load() ? Status::Fail("serve failed") : Status();
  if (!model_ready_.load() || listen_fd < 0 ||
      (operations_port_ != 0 && operations_fd < 0))
    return Status::Fail("server startup not complete");
  sip_service_.Start(sip_trunks_file_, [this](std::string_view scope) {
    const auto policy = clients_.Snapshot();
    return !stop_requested_.load() && policy &&
           std::any_of(policy->clients.begin(), policy->clients.end(),
                       [&](const auto& client) { return client.id == scope; });
  }, sip_trunks_sha256_, live_worker_pins_.sip);
  detail::ServeFaultPoint("connection.before_publish");
  operations_listen_fd_.store(operations_fd);
  listen_fd_.store(listen_fd);
  std::fprintf(stderr, "[q4t] serving on port %d (host %s, model %s)\n", port_,
               host_.c_str(), model_name_.c_str());
  if (operations_port_)
    std::fprintf(stderr, "[q4t] operations on port %d (host 127.0.0.1)\n",
                 operations_port_);
  try {
    auto loop = progress_.Acquire(0);
    loop.Handle().Stage(ProgressStage::kControl);
    ProgressContext progress_context(loop.Handle());
    bool draining = false;
    auto drain_until = std::chrono::steady_clock::time_point::max();
    for (;;) {
      ProgressExecution loop_work(ProgressWork::kServiceLoop);
      detail::PollRequestWorkers();
      PollMcpSessions();
      RecoverApplicationJobs();
      if (stop_requested_.load() && !draining) {
        draining = true;
        drain_until =
            std::chrono::steady_clock::now() + std::chrono::seconds(2);
        requests_.CancelAll();
        if (mcp_tunnels_) mcp_tunnels_->Stop();
        environment_preparer_.RequestStop();
        eval_dispatcher_.RequestStop();
        webhook_dispatcher_.RequestStop();
        batches_.RequestStop();
        vector_jobs_.RequestStop();
        background_.RequestStop();
        {
          const std::lock_guard<std::mutex> lock(seq_mu_);
          seq_stopping_ = true;
        }
        seq_cv_.notify_all();
        sched_cv_.notify_all();
      }
      if (draining && (active_conns_.load() == 0 ||
                       std::chrono::steady_clock::now() >= drain_until))
        break;
      // Service the private listener first, once per round, without draining
      // either accept backlog indefinitely. All overload writes are bounded.
      std::array<pollfd, 2> listeners{
          {{operations_fd, POLLIN, 0}, {listen_fd, POLLIN, 0}}};
      const int ready = ::poll(listeners.data(), listeners.size(), 25);
      if (ready == 0 || (ready < 0 && errno == EINTR)) {
        ProgressExecution::Completed();
        continue;
      }
      if (ready < 0) {
        serving_failed_.store(true);
        break;
      }
      for (size_t index = 0; index < listeners.size(); ++index) {
        const auto& listener = listeners[index];
        if (listener.revents & (POLLERR | POLLHUP | POLLNVAL)) {
          serving_failed_.store(true);
          break;
        }
        if (!(listener.revents & POLLIN)) continue;
        const int fd = ::accept4(listener.fd, nullptr, nullptr, SOCK_CLOEXEC);
        if (fd < 0) {
          if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
            continue;
          std::fprintf(stderr, "[q4t] accept failed: %s\n",
                       std::strerror(errno));
          serving_failed_.store(true);
          break;
        }
        StartConnection(fd, index == 0);
      }
      if (serving_failed_.load()) break;
      ProgressExecution::Completed();
    }
  } catch (const std::exception& error) {
    std::fprintf(stderr, "[q4t] accept/thread exception: %s\n", error.what());
    serving_failed_.store(true);
  } catch (...) {
    std::fprintf(stderr, "[q4t] unknown accept/thread exception\n");
    serving_failed_.store(true);
  }
  RequestStop();
  std::fprintf(stderr, "[q4t] shutting down: draining in-flight requests...\n");
  requests_.CancelAll();
  environment_preparer_.RequestStop();
  eval_dispatcher_.RequestStop();
  webhook_dispatcher_.RequestStop();
  batches_.RequestStop();
  vector_jobs_.RequestStop();
  background_.RequestStop();
  {
    const std::lock_guard<std::mutex> lock(connections_mu_);
    for (int fd : connections_) ::shutdown(fd, SHUT_RDWR);
  }
  StopScheduler();
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    seq_stopping_ = true;
  }
  seq_cv_.notify_all();
  environment_preparer_.Join();
  eval_dispatcher_.Join();
  webhook_dispatcher_.Join();
  batches_.Join();
  vector_jobs_.Join();
  background_.Join();
  while (active_conns_.load(std::memory_order_acquire) > 0)
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  JoinLiveMedia();
  sip_service_.Stop();
  realtime_traces_.RequestStop();
  realtime_traces_.Join();
  close_listener.Run();
  std::fprintf(stderr, "[q4t] shutdown complete (0 in-flight remaining)\n");
  return serving_failed_.load() ? Status::Fail("serve failed") : Status();
}

void ChatServer::HandleClient(int fd, bool operations) {
  HttpRequestState state;
  for (size_t request = 0; request < 100 && !stop_requested_.load(); ++request) {
    if (request && !AwaitHttpRequest(fd, &state)) return;
    if (!HandleRequest(fd, operations, &state, request + 1 < 100)) return;
  }
}

bool ChatServer::HandleRequest(int fd, bool operations, HttpRequestState* state,
                              bool allow_keep_alive) {
  RealtimeSecretGrant grant;
  ClientLease client_lease;  // Outlives response and heartbeat cleanup.
  detail::HttpResponseContext context(fd, &metrics_.http_transport);
  bool dispatch_started = false;
  try {
    std::string method, path, body;
    const auto resolve_realtime = [&](std::string_view key) {
      const auto audience = (IsRealtimeTranslationTarget(path) || IsTranslationCall(path))
                                ? RealtimeSecretAudience::kTranslation
                                : RealtimeSecretAudience::kRealtime;
      return realtime_secrets_.Resolve(key, &grant, RealtimeSecretTime::Now(), audience);
    };
    HttpBodyLease lease(body_budget_);
    HttpRequestHeaders headers;
    std::string websocket_accept;
    HttpBodyStream body_stream;
    std::unique_ptr<FileHttpRequest> file_request;
    ObjectReply file_preparation;
    std::atomic<int>* admission = nullptr;
    ScopeExit release_admission([&] {
      if (admission) admission->fetch_sub(1);
    });
    int failure_status = 400;
    const auto admit = [&] {
      if (!headers.request_id.empty()) context.SetTraceId(headers.request_id);
      NormalizeResponsesPostTarget(method, path);
      context.SetAssistantsBeta(headers.assistants_beta);
      context.SetAgentsBeta(headers.agents_beta);
      context.SetChatKitBeta(headers.chatkit_beta);
      context.SetAgentEventKey(std::move(headers.agent_event_key));
      context.SetResponsesMultiAgent(headers.responses_multi_agent &&
                                     IsResponsesTarget(path));
      if (IsResponsesTarget(path))
        context.SetLegacyCacheRetention(!headers.legacy_cache_sdk_version.empty());
      const auto kind = ClassifyHttpConnection(method, path);
      if (operations && ((kind != HttpConnectionClass::kControl &&
                          kind != HttpConnectionClass::kObservation) ||
                         IsAgentEventSubmission(method, path))) {
        failure_status = 404;
        headers.error_code = "route_not_found";
        headers.failure = HttpReceiveFailure::kRoute;
        return false;
      }
      const bool chatkit_client = IsChatKitClient(method, path);
      const bool transfer = !ParseChatKitTransfer(method, path).file.empty();
      const bool preview = !ParseChatKitPreview(method, path).file.empty();
      const auto mark = path.find('?');
      std::string preview_ticket;
      if (preview && mark != path.npos) {
        preview_ticket =
            path.substr(mark + 1 + std::string_view("ticket=").size());
        path.resize(mark);
        if (headers.authorization.empty())
          headers.authorization = "Bearer " + preview_ticket;
      }
      if (headers.authorization.starts_with("Q4T-ChatKit ")) {
        std::string bearer;
        if (!chatkit_client || headers.client_id.empty() ||
            !clients_.Authenticate(headers.client_id, headers.gateway_token)
                 .Allowed() ||
            !ResolveForwardedChatKit(
                headers.authorization, headers.client_id, headers.gateway_token,
                std::time(nullptr), &bearer,
                preview    ? ClientTicketAudience::kChatKitPreview
                : transfer ? ClientTicketAudience::kChatKitUpload
                           : ClientTicketAudience::kChatKit)) {
          failure_status = 401;
          headers.error_code = "invalid_client_secret";
          headers.failure = HttpReceiveFailure::kAdmission;
          return false;
        }
        headers.chatkit_issuer = headers.authorization.substr(std::string_view("Q4T-ChatKit ").size(), 64);
        headers.authorization = std::move(bearer);
      }
      const auto credential = BearerCredential(headers.authorization);
      if ((!preview_ticket.empty() && credential != preview_ticket) ||
          credential.starts_with("ckpv_") != preview ||
          credential.starts_with("ckup_") != transfer ||
          credential.starts_with("cksec_") !=
              (chatkit_client && !transfer && !preview)) {
        failure_status = 401;
        headers.error_code = "invalid_client_secret";
        headers.failure = HttpReceiveFailure::kAdmission;
        return false;
      }
      if (credential.starts_with("ek_")) {
        const auto client =
            clients_.Authenticate(headers.client_id, headers.gateway_token);
        if (!RealtimeGrantTarget(method, path) || !client.Allowed() ||
            !resolve_realtime(credential).ok() ||
            grant.owner != client.Id()) {
          failure_status = 401;
          headers.error_code = "invalid_api_key";
          headers.failure = HttpReceiveFailure::kAdmission;
          return false;
        }
        context.SetRealtimeStartup(&grant.session, grant.legacy_temperature);
      }
      ClientIdentity identity;
      if (!AdmitClientRequest(method, path, &headers, clients_, &client_lease,
                              &identity, &failure_status))
        return false;
      context.SetClient(identity, &client_lease);
      if (method == "POST" && IsRealtimeCredentialTarget(path))
        headers.body_limit = std::min<uint64_t>(headers.body_limit,
                                                kWebSocketMessageLimit);
      if (IsRealtimeTarget(path) && !IsLiveMediaCreate(method, path)) {
        if (!headers.origin.empty()) {
          // The optional ingress validates browser origins before forwarding.
          // Direct backend access has no configured browser-origin policy.
          failure_status = 403;
          headers.error_code = "origin_forbidden";
          headers.failure = HttpReceiveFailure::kAdmission;
          return false;
        }
        const auto upgrade = CheckWebSocketUpgrade(
            {method, headers.connection, headers.upgrade,
             headers.websocket_version, headers.websocket_key, headers.http11,
             headers.chunked, headers.content_length}, &websocket_accept);
        if (upgrade != WebSocketUpgradeResult::kOk) {
          failure_status = upgrade == WebSocketUpgradeResult::kCryptoUnavailable ||
                                   upgrade == WebSocketUpgradeResult::kCryptoFailure
                               ? 503 : 400;
          headers.error_code = "websocket_upgrade_invalid";
          headers.failure = HttpReceiveFailure::kAdmission;
          return false;
        }
      }
      if (IsTranscriptionTarget(path))
        headers.body_limit = std::min(headers.body_limit, kAsrFormBytes);
      if (IsSipRefer(method, path))
        headers.body_limit = std::min<uint64_t>(headers.body_limit, 65536);
      if (IsSipReject(method, path) || IsSipHangup(method, path))
        headers.body_limit = std::min<uint64_t>(headers.body_limit, 4096);
      if (IsCancellationRequest(method, path))
        lease.SelectBudget(cancel_body_budget_);
      if (IsMcpTunnelPoll(method, path) || IsMcpTunnelResponse(method, path))
        lease.SelectBudget(tunnel_body_budget_);
      if ((kind == HttpConnectionClass::kBusiness ||
           kind == HttpConnectionClass::kRealtime || kind == HttpConnectionClass::kSideband ||
           kind == HttpConnectionClass::kTunnelPoll ||
           kind == HttpConnectionClass::kTunnelResponse) &&
          stop_requested_.load()) {
        failure_status = 503;
        headers.error_code = "server_draining";
        headers.failure = HttpReceiveFailure::kDraining;
        return false;
      }
      auto& control = operations ? operations_control_conns_ : control_conns_;
      auto& observation =
          operations ? operations_observation_conns_ : observation_conns_;
      auto& counter =
          kind == HttpConnectionClass::kSideband ? sideband_conns_
          : kind == HttpConnectionClass::kRealtime     ? realtime_conns_
          : kind == HttpConnectionClass::kTunnelPoll ? tunnel_poll_conns_
          : kind == HttpConnectionClass::kTunnelResponse
              ? tunnel_response_conns_
          : kind == HttpConnectionClass::kControl     ? control
          : kind == HttpConnectionClass::kObservation ? observation
                                                      : business_conns_;
      const int cap =
          kind == HttpConnectionClass::kSideband ? kSidebandConnections
          : kind == HttpConnectionClass::kRealtime ? kRealtimeConnections
          : kind == HttpConnectionClass::kTunnelPoll
              ? (mcp_tunnels_enabled_ ? kTunnelPollConnections : 0)
          : kind == HttpConnectionClass::kTunnelResponse
              ? (mcp_tunnels_enabled_ ? kTunnelResponseConnections : 0)
          : kind == HttpConnectionClass::kControl     ? kControlConnections
          : kind == HttpConnectionClass::kObservation ? kObservationConnections
                                                      : conn_cap_;
      if (!AcquireHttpConnection(counter, cap)) {
        auto& rejected = operations ? metrics_.operations_connection_rejections
                                    : metrics_.connection_rejections;
        rejected[static_cast<size_t>(kind)].fetch_add(1);
        failure_status = 503;
        headers.error_code = kind == HttpConnectionClass::kObservation
                                 ? "observation_capacity_exhausted"
                                 : "capacity_exhausted";
        headers.failure = HttpReceiveFailure::kConnectionCapacity;
        return false;
      }
      admission = &counter;
      if (transfer || (method == "POST" && !IsCancellationRequest(method, path) &&
           (path == "/v1/chat/completions" || path == "/v1/chat/preflight" ||
            path == "/v1/completions" || path == "/v1/moderations" ||
            path == "/v1/decisions" || path == "/v1/embeddings" ||
            path == "/v1/audio/speech" || IsTranscriptionTarget(path) ||
            IsVoiceCreation(method, path) || IsResponsesTarget(path) ||
            IsAssistantsV2Target(path) || IsManagedAgentTarget(path) ||
            IsConversationTarget(path) || IsPromptTarget(path) ||
            IsChatKitClient(method, path) || IsChatKitTarget(path) || VideoCharacterMethodAllowed(method, path) || IsEvalTarget(path) || IsSafetyTarget(path) || IsWebhookTarget(path) || IsBatchTarget(path) || IsVectorStoreTarget(path) ||
            IsSkillTarget(path) || IsContainerTarget(path) || IsFileTarget(path) ||
            IsUploadTarget(path))) ||
          (method == "GET" &&
           (!LiveRecordingId(path).empty() ||
            ParseAgentSessionRoute(path).kind ==
                AgentSessionRouteKind::kArtifactContent ||
            ParseSkillRoute(path).kind == SkillRouteKind::kContent ||
            ParseSkillRoute(path).kind == SkillRouteKind::kVersionContent ||
            ParseContainerRoute(path).kind == ContainerRouteKind::kContent)) ||
          (method == "GET" && IsVectorStoreTarget(path) &&
           !IsVectorStoreObservation(method, path)) ||
          (method == "GET" && IsFileTarget(path) &&
           !IsFileMetadata(method, path))) {
        const auto memory = memory_admission_.Observe();
        if (!memory.Allowed()) {
          RecordMemoryRejection(fd, memory.state, MemoryCheckpoint::kHeaders);
          failure_status = 503;
          headers.error_code = memory.StateName();
          headers.failure = HttpReceiveFailure::kAdmission;
          return false;
        }
      }
      if (IsChatKitUpload(method, path) || IsSkillTarget(path) ||
          IsContainerTarget(path) || IsFileTarget(path) || IsUploadTarget(path)) {
        if (!objects_) {
          failure_status = 503;
          headers.error_code = "object_scope_unavailable";
          return false;
        }
        const auto scope = identity.Scoped()
            ? "client:" + std::string(identity.Id()) : "local:anonymous";
        BinaryAccess access{
            scope, std::chrono::steady_clock::now() +
                       std::chrono::milliseconds(request_deadline_ms_),
            [this, fd, cancellation = IsUploadCancellation(method, path)] {
              return (!cancellation && stop_requested_.load()) ||
                     detail::ClientConnectionFailed(fd);
            }};
        file_request = std::make_unique<FileHttpRequest>(*objects_, access);
        const auto ready =
            IsChatKitUpload(method, path)
                ? file_request->PrepareChatKit(credential, headers.content_type,
                                               &body_stream, ParseChatKitTransfer(method, path))
                : file_request->Prepare(method, path, headers.content_type,
                                        &body_stream, headers.accept);
        if (!ready.ok()) {
          file_preparation = ready;
          failure_status = ready.http_status;
          headers.error_code = ready.ClientCode();
          headers.error_param = ready.value.GetString("param");
          return false;
        }
        headers.body_limit = identity.Scoped()
            ? std::min<uint64_t>(headers.body_limit, file_request->WireLimit())
            : file_request->WireLimit();
      }
      return true;
    };
    if (!ReadHttpRequest(fd, &method, &path, &body, 30000, &lease,
                         &failure_status, &headers, admit, {}, state, {},
                         &body_stream)) {
      if (!headers.request_id.empty()) context.SetTraceId(headers.request_id);
      context.RecordReceiveFailure(headers.failure, failure_status,
                                   headers.system_error);
      const char* reason = failure_status == 408   ? "Request Timeout"
                           : failure_status == 503 ? "Service Unavailable"
                           : failure_status == 401 ? "Unauthorized"
                           : failure_status == 403 ? "Forbidden"
                           : failure_status == 404 ? "Not Found"
                           : failure_status == 413 ? "Content Too Large"
                           : failure_status == 417 ? "Expectation Failed"
                           : failure_status == 429 ? "Too Many Requests"
                                                   : "Bad Request";
      const auto* file_error = !file_preparation.ok() ? &file_preparation
          : file_request ? file_request->UploadFailure() : nullptr;
      if (file_error)
        SendError(fd, file_error->http_status, file_error->ClientMessage(), {},
                    file_error->ClientCode(), file_error->value.GetString("param"),
                    file_error->value.GetString("allow"));
      else
        SendError(fd, failure_status, reason, {}, headers.error_code,
                    headers.error_param);
      return false;
    }
    if (context.RealtimeStartup()) {
      const auto reply =
          resolve_realtime(BearerCredential(headers.authorization));
      if (!reply.ok()) {
        SendError(fd, reply.http_status, reply.ClientMessage(), {}, reply.ClientCode());
        return false;
      }
    }
    if (IsRealtimeCredentialTarget(path) || IsSipReject(method, path) || IsSipHangup(method, path) || IsSipRefer(method, path) || IsLiveMediaCreate(method, path) || IsStoredChatTarget(path) || path == "/v1/chat/preflight" ||
        path == "/v1/completions" || path == "/v1/moderations" || path == "/v1/decisions" ||
        path == "/v1/embeddings" || path == "/v1/audio/speech" ||
        IsResponsesTarget(path) || path == "/v1/requests/cancel" ||
        IsAssistantsV2Target(path) || IsManagedAgentTarget(path) || IsConversationTarget(path) || IsPromptTarget(path) ||
        IsChatKitClient(method, path) || IsChatKitTarget(path) || VideoCharacterMethodAllowed(method, path) || IsEvalTarget(path) || IsSafetyTarget(path) || IsWebhookTarget(path) || IsBatchTarget(path) || IsVectorStoreTarget(path) ||
        IsMcpTunnelTarget(path)) {
      if (method == "POST" && !VideoCharacterMethodAllowed(method, path) &&
          !IsChatKitUpload(method, path) && !IsRealtimeOffer(path) && !((IsRealtimeCredentialTarget(path) || path == "/v1/vaults" || path == "/v1/threads" || (method == "POST" && ParseAssistantRunRoute(path).kind == AssistantRunRouteKind::kCancel) ||
                                 IsOptionalResourceUpdate(path) || IsAutomationAction(path) ||
                                 EvalOperation(method, path).kind == EvalRouteKind::kCancel ||
                                 ParseChatKitRoute(path).kind == ChatKitRouteKind::kCancel ||
                                 ParseWebhookRoute(path).kind == WebhookRouteKind::kRotate ||
                                 path == kEnvironmentTemplatesPath || IsResponseCancellation(method, path) ||
                                 IsSipHangup(method, path) ||
                                 IsResponsesTokenCount(path) ||
                                 IsBatchCancellation(method, path) ||
                                 IsVectorStoreCancellation(method, path) ||
                                 (IsSipReject(method, path) &&
                                  SipRejectId(path).starts_with("rtc_"))) &&
                                body.empty() && headers.content_type.empty())) {
        std::string type = headers.content_type;
        for (char& c : type)
          if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
        const auto semi = type.find(';');
        std::string media = type.substr(0, semi);
        while (!media.empty() && (media.back() == ' ' || media.back() == '\t'))
          media.pop_back();
        std::string charset = semi == type.npos ? "" : type.substr(semi + 1);
        while (!charset.empty() &&
               (charset.front() == ' ' || charset.front() == '\t'))
          charset.erase(0, 1);
        if (media != "application/json" ||
            (!charset.empty() && charset != "charset=utf-8" &&
             charset != "charset=\"utf-8\"")) {
          context.RecordReceiveFailure(HttpReceiveFailure::kMedia, 415);
          SendError(fd, 415, "Content-Type must be application/json with UTF-8",
                    {}, "unsupported_media_type", "Content-Type");
          return false;
        }
      }
    }
    const auto t0 = std::chrono::steady_clock::now();
    context.SetKeepAlive(headers.keep_alive && allow_keep_alive &&
                            !stop_requested_.load(), headers.http11);
    dispatch_started = true;
    if (IsSipReject(method, path))
      HandleSipReject(fd, SipRejectId(path), body);
    else if (IsSipHangup(method, path))
      HandleSipControl(fd, SipControlId(path, "/hangup"), body, false);
    else if (IsSipRefer(method, path))
      HandleSipControl(fd, SipControlId(path, "/refer"), body, true);
    else if (method == "POST" && !SipControlId(path, "/accept").empty())
      HandleLiveMediaCreate(fd, {}, body, admission, SipControlId(path, "/accept"));
    else if (path == "/v1/realtime/translations/client_secrets" && method == "POST")
      IssueRealtimeSecret(fd, headers, body, RealtimeCredentialApi::kTranslation);
    else if (path == "/v1/realtime/client_secrets" && method == "POST")
      IssueRealtimeSecret(fd, headers, body);
    else if (path == "/v1/realtime/transcription_sessions" && method == "POST")
      IssueRealtimeSecret(fd, headers, body, RealtimeCredentialApi::kTranscription);
    else if (path == "/v1/realtime/sessions" && method == "POST")
      IssueRealtimeSecret(fd, headers, body, RealtimeCredentialApi::kConversation);
    else if (method == "POST" && IsRealtimeOffer(path)) {
      HandleLiveMediaCreate(fd, {}, body, admission, {}, headers.content_type,
                            IsTranslationCall(path));
    } else if (IsLiveMediaCreate(method, path))
      HandleLiveMediaCreate(fd, LiveForkId(path), body, admission);
    else if (IsRealtimeTarget(path))
      HandleRealtime(fd, path, headers, state->pending, websocket_accept);
    else if (file_request)
      file_request->Serve(fd);
    else if (IsVoiceTarget(path)) {
      HandleVoices(fd, method, path, headers.content_type, body);
    } else if (IsTranscriptionTarget(path)) {
      if (method != "POST") {
        context.RecordReceiveFailure(HttpReceiveFailure::kMethod, 405);
        SendError(fd, 405, "method not allowed", {}, "method_not_allowed",
                    "method", "POST");
      } else if (path.find('?') != path.npos) {
        SendError(fd, 400, "Transcription does not accept query parameters", {},
                    "invalid_request", "query");
      } else {
        HandleTranscription(fd, headers.content_type, body,
                            path == "/v1/audio/translations");
      }
    } else if (IsMcpTunnelTarget(path)) {
      const auto& client = context.Client();
      const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                         : "local:anonymous";
      BinaryAccess access{scope,
                          std::chrono::steady_clock::now() +
                              std::chrono::milliseconds(request_deadline_ms_),
                          [this, fd] {
                            return stop_requested_.load() ||
                                   detail::ClientConnectionFailed(fd);
                          }};
      const auto reply =
          mcp_tunnels_ && client.Allowed()
              ? HandleMcpTunnelRequest(*mcp_tunnels_, access, method, path,
                                       body, headers.tunnel_instance,
                                       headers.tunnel_shard_token)
              : object_detail::Error(503, "mcp_tunnel_unavailable",
                                     "Tunnel broker unavailable");
      if (reply.ok())
        SendSimple(fd, reply.http_status,
                   reply.http_status == 204 ? "No Content" : "OK",
                   reply.http_status == 204 ? "" : OutputJson(reply.value),
                   "application/json", {access.deadline});
      else
        SendError(fd, reply.http_status, reply.ClientMessage(),
                  {access.deadline}, reply.ClientCode(), {},
                  reply.value.GetString("allow"));
    } else
      Dispatch(fd, method, path, body, headers);
    static const bool access_log = std::getenv("Q4T_ACCESS_LOG") != nullptr;
    if (access_log) {
      const double ms = std::chrono::duration<double, std::milli>(
                            std::chrono::steady_clock::now() - t0)
                            .count();
      std::fprintf(stderr, "[q4t][http] trace_id=%s elapsed_ms=%.1f\n",
                   context.TraceId().c_str(), ms);
    }
    return context.FinishResponse() && !stop_requested_.load();
  } catch (const std::bad_alloc&) {
    if (!dispatch_started)
      context.RecordReceiveFailure(HttpReceiveFailure::kException, 503);
    detail::SendExceptionResponse(fd, *context.State(),
                                  detail::ExceptionKind::kHostMemory);
  } catch (...) {
    if (!dispatch_started)
      context.RecordReceiveFailure(HttpReceiveFailure::kException, 500);
    detail::SendExceptionResponse(fd, *context.State());
  }
  return false;
}

void ChatServer::Dispatch(int fd, const std::string& method,
                          const std::string& path, const std::string& body,
                          const HttpRequestHeaders& headers) {
  const auto rejected = [&](HttpReceiveFailure reason, int status) {
    if (auto* context = detail::HttpResponseContext::Current(fd))
      context->RecordReceiveFailure(reason, status);
  };
  if (const auto id = LiveRecordingId(path); !id.empty()) {
    if (method != "GET") {
      SendError(fd, 405, "Recording downloads require GET", {},
                "method_not_allowed", {}, "GET");
      return;
    }
    if (!body.empty()) {
      SendError(fd, 400, "Recording download takes no body", {},
                "invalid_request");
      return;
    }
    if (!live_recordings_ || !objects_) {
      SendError(fd, 404, "Recording not found", {}, "object_not_found");
      return;
    }
    const auto* context = detail::HttpResponseContext::Current(fd);
    if (!context || !context->Client().Allowed() ||
        !context->Client().Scoped()) {
      SendError(fd, 401, "Authentication required", {},
                "client_authentication_failed");
      return;
    }
    HttpBodyLease working(body_budget_);
    if (!working.Acquire(kLiveRecordingHostBytes)) {
      SendError(fd, 503, "Recording download capacity exhausted", {},
                "capacity_exhausted");
      return;
    }
    const auto client = context->Client();
    BinaryAccess access{"client:" + std::string(client.Id()),
                        std::chrono::steady_clock::now() +
                            std::chrono::milliseconds(request_deadline_ms_),
                        [this, fd, client] {
                          return stop_requested_.load() ||
                                 !clients_.OwnsIdentity(client) ||
                                 detail::ClientConnectionFailed(fd);
                        }};
    SendLiveRecording(fd, *objects_, access, std::string(id));
    return;
  }
  if (IsRealtimeTraceTarget(path)) {
    const auto* context = detail::HttpResponseContext::Current(fd);
    if (!context || !context->Client().Allowed() || !realtime_traces_) {
      SendError(fd, 503, "Authenticated tracing is unavailable", {},
                "realtime_trace_unavailable");
      return;
    }
    const auto& client = context->Client();
    StateAccess access{
        client.Scoped() ? "client:" + std::string(client.Id()) : "local:anonymous",
        std::chrono::steady_clock::now() + std::chrono::seconds(5),
        [this, fd] { return stop_requested_.load() ||
                           detail::ClientConnectionFailed(fd); }};
    const auto reply = HandleRealtimeTraceRequest(
        *realtime_traces_, method, path, body, access);
    if (reply.ok())
      SendSimple(fd, 200, "OK", OutputJson(reply.value), "application/json",
                 {access.deadline});
    else
      SendError(fd, reply.http_status, reply.ClientMessage(), {access.deadline},
                reply.ClientCode(), reply.value.GetString("param"),
                reply.value.GetString("allow"));
    return;
  }
  if (IsResponsesTokenCount(path)) {
    if (method != "POST") {
      rejected(HttpReceiveFailure::kMethod, 405);
      SendError(fd, 405, "method not allowed", {}, "method_not_allowed",
                "method", "POST");
    } else if (path != "/v1/responses/input_tokens") {
      SendError(fd, 400, "Input count does not accept query parameters", {},
                "invalid_request", "query");
    } else {
      HandleResponsesTokenCount(fd, body);
    }
    return;
  }
  if (IsResponsesCompaction(path)) {
    if (method != "POST") {
      rejected(HttpReceiveFailure::kMethod, 405);
      SendError(fd, 405, "method not allowed", {}, "method_not_allowed",
                "method", "POST");
    } else if (path != "/v1/responses/compact") {
      SendError(fd, 400, "Compact does not accept query parameters", {},
                "invalid_request", "query");
    } else {
      HandleChat(fd, body, GenerationApi::kCompact);
    }
    return;
  }
  if (ParseVectorStoreRoute(path).kind == VectorStoreRouteKind::kSearch) {
    HandleVectorSearch(fd, method, path, body);
    return;
  }
  if ((IsStoredChatTarget(path) &&
       !(method == "POST" &&
         path.substr(0, path.find('?')) == "/v1/chat/completions")) ||
      IsAssistantsV2Target(path) || IsManagedAgentTarget(path) || IsConversationTarget(path) || IsPromptTarget(path) ||
      IsChatKitClient(method, path) || IsChatKitTarget(path) || VideoCharacterMethodAllowed(method, path) || IsEvalTarget(path) || IsSafetyTarget(path) || IsWebhookTarget(path) || IsBatchTarget(path) || IsVectorStoreTarget(path) ||
      (IsResponsesTarget(path) &&
       path.substr(0, path.find('?')) != "/v1/responses")) {
    const auto* context = detail::HttpResponseContext::Current(fd);
    if (!objects_ || !context || !context->Client().Allowed()) {
      SendError(fd, 503, "Authenticated application state unavailable", {},
                "object_scope_unavailable");
      return;
    }
    if (IsAssistantsV2Target(path) && !context->AssistantsBeta()) {
      SendError(fd, 400, "Assistants require OpenAI-Beta assistants=v2", {},
                "beta_required", "OpenAI-Beta");
      return;
    }
    if (IsChatKitTarget(path) && !context->ChatKitBeta()) {
      SendError(fd, 400, "ChatKit requires OpenAI-Beta chatkit_beta=v1",
                {}, "beta_required", "OpenAI-Beta");
      return;
    }
    if (IsManagedAgentTarget(path) && !context->AgentsBeta()) {
      SendError(fd, 400, "Agents require OpenAI-Beta agents=v1",
                {}, "beta_required", "OpenAI-Beta");
      return;
    }
    const auto& client = context->Client();
    const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                       : "local:anonymous";
    StateAccess access{
        scope,
        std::chrono::steady_clock::now() +
            std::chrono::milliseconds(request_deadline_ms_),
        [this, fd, cancellation = IsCancellationRequest(method, path)] {
          return (!cancellation && stop_requested_.load()) ||
                 detail::ClientConnectionFailed(fd);
        }};
    const bool create_eval_run = method == "POST" &&
        ParseEvalRoute(path).kind == EvalRouteKind::kRuns;
    if (create_eval_run && !eval_dispatcher_) {
      SendError(fd, 503, "Evaluation worker is unavailable", {},
                "eval_worker_unavailable");
      return;
    }
    if (const auto preview = ParseChatKitPreview(method, path);
        !preview.file.empty()) {
      if (!body.empty()) {
        SendError(fd, 400, "Attachment previews take no body", {},
                  "invalid_request");
        return;
      }
      std::string png;
      const auto available = chatkit_detail::ReadAttachmentPreview(
          *objects_, access, preview.session, preview.file,
          BearerCredential(headers.authorization), &png);
      if (!available.ok()) {
        SendError(fd, available.http_status, available.ClientMessage(), {},
                  available.ClientCode(), {}, {}, true);
        return;
      }
      detail::SendSimple(fd, 200, "OK", png, "image/png", {access.deadline}, {},
                         {}, true);
      return;
    }
    if (IsChatKitClient(method, path)) {
      const char* configured = std::getenv("Q4T_CHATKIT_PUBLIC_BASE");
      const auto origin = EvalReportBase(headers, client.Scoped(),
          "127.0.0.1:" + std::to_string(port_), configured ? configured : "");
      ServeChatKitClient(fd, *objects_, background_.Get(), PlanningConfig(fd), access,
                         BearerCredential(headers.authorization), body,
                         {headers.gateway_token, headers.chatkit_issuer}, origin,
                         [this, fd](const io::Json& params) {
                           HandleTranscription(fd, {}, {}, false, &params);
                         });
      return;
    }
    if (IsResponsesTarget(path)) {
      ServeResponsesState(fd, *objects_, method, path, body, access,
                          background_.Get());
      return;
    }
    if (ParseAssistantRunRoute(path).kind != AssistantRunRouteKind::kNone) {
      ServeAssistantRun(fd, *objects_, background_.Get(), method, path, body,
                        access, PlanningConfig(fd), !embedding_model_dir_.empty());
      return;
    }
    if (ParseAgentSessionRoute(path).kind != AgentSessionRouteKind::kNone) {
      ServeAgentSession(fd, *objects_, clients_, background_.Get(), method, path, body,
                        PlanningConfig(fd), access);
      return;
    }
    std::string report_base;
    const auto eval_route = EvalOperation(method, path);
    if (eval_route.kind == EvalRouteKind::kRuns || eval_route.kind == EvalRouteKind::kRun ||
        eval_route.kind == EvalRouteKind::kCancel) {
      const char* configured = std::getenv("Q4T_EVAL_PUBLIC_BASE");
      report_base = EvalReportBase(headers, client.Scoped(),
          "127.0.0.1:" + std::to_string(port_), configured ? configured : "");
      if (report_base.empty()) {
        SendError(fd, configured && *configured ? 503 : 400,
                  "Invalid evaluation report origin", {access.deadline},
                  "invalid_report_origin");
        return;
      }
    }
    ConversationCompaction compact;
    auto reply =
        IsVectorStoreTarget(path)
            ? HandleVectorStoreRequest(
                  *objects_, clients_, !embedding_model_dir_.empty(), method,
                  path, body, client,
                  {access.scope, access.deadline, access.cancelled})
        : IsEvalTarget(path)
            ? HandleEval(*objects_, access, method, path, body, background_.Get(),
                         report_base)
        : IsChatKitTarget(path)
            ? HandleChatKit(*objects_, access, method, path, body,
                {headers.gateway_token, headers.authorization.starts_with("Q4T-Issuer ")
                     ? std::string_view(headers.authorization).substr(11) : std::string_view{}})
        : VideoCharacterMethodAllowed(method, path)
            ? HandleVideoCharacter(*objects_, access, method, path,
                                   headers.content_type, body)
        : IsSafetyTarget(path)
            ? GetSafetyRecord(*objects_, access, method, path, body)
        : IsWebhookTarget(path)
            ? HandleWebhook(*objects_, access, webhook_network_.get(), method, path, body)
        : IsBatchTarget(path)
            ? HandleBatchRequest(
                  *objects_, batches_.Get(), method, path, body, client,
                  {access.scope, access.deadline, access.cancelled})
        : ParseHostedRoute(path).kind != HostedRouteKind::kNone
            ? HandleHostedEnvironment(*objects_, method, path, body, access,
                                      context->AgentEventKey(),
                                      environment_preparer_.Get() != nullptr)
        : IsEnvironmentTemplateTarget(path)
            ? HandleEnvironmentTemplate(*objects_, method, path, body, access)
        : IsVaultTarget(path)
            ? HandleAgentVault(*objects_, method, path, body, access)
        : ParseAssistantThreadRoute(path).kind != AssistantThreadRouteKind::kNone
            ? HandleAssistantThreads(*objects_, method, path, body, access,
                                     !embedding_model_dir_.empty())
        : IsAssistantTarget(path)
            ? HandleAssistantRegistry(*objects_, method, path, body,
                                      PlanningConfig(fd), access,
                                      !embedding_model_dir_.empty())
        : ParseAutomationRoute(path).kind != AutomationRouteKind::kNone
            ? HandleAgentAutomation(*objects_, background_.Get(),
                                    PlanningConfig(fd), access, method, path, body)
        : IsAgentRegistrationTarget(path)
            ? HandleAgentRegistration(*objects_, method, path, body,
                                      PlanningConfig(fd), access)
        : IsPromptTarget(path)
            ? HandlePromptRequest(*objects_, method, path, body,
                                  PlanningConfig(fd), access)
        : IsStoredChatTarget(path)
            ? HandleStoredChatRequest(*objects_, method, path, body, access)
            : HandleConversationRequest(*objects_, method, path, body, access,
                                        &compact);
    if (reply.ok() && compact.input.IsArray()) {
      auto request = object_detail::Object({
          {"model", object_detail::String(model_name_)},
          {"input", std::move(compact.input)}});
      HandleChat(fd, OutputJson(request), GenerationApi::kCompact, {},
                 nullptr, nullptr, nullptr, &compact);
      return;
    }
    if (eval_dispatcher_ && reply.ok() &&
        (eval_route.kind == EvalRouteKind::kCancel ||
         (method == "DELETE" && (eval_route.kind == EvalRouteKind::kRun ||
                                 eval_route.kind == EvalRouteKind::kEval)))) {
      eval_dispatcher_->CancelEmbedding(scope, eval_route.eval, eval_route.run);
    }
    if (create_eval_run && (reply.ok() || reply.commit_unknown))
      eval_dispatcher_->Wake();
    if (reply.ok() && eval_route.kind == EvalRouteKind::kReport)
      SendSimple(fd, 200, "OK", reply.value.str, "text/html; charset=utf-8",
                 {access.deadline}, {}, {}, true);
    else if (reply.ok())
      SendSimple(fd, reply.http_status, reply.http_status == 201 ? "Created" : "OK",
                 OutputJson(reply.value), "application/json",
                 {access.deadline});
    else
      SendError(fd, reply.http_status, reply.ClientMessage(),
                {access.deadline}, reply.ClientCode(),
                reply.value.GetString("param"),
                reply.value.GetString("allow"));
    return;
  }
  if (path.find('?') != path.npos) {
    rejected(HttpReceiveFailure::kQuery, 400);
    SendError(fd, 400, "query parameters are not supported", {},
              "unsupported_query", "path");
    return;
  }
  std::string_view expected;
  if (IsModelDetailPath(path)) {
    if (method != "GET" && method != "DELETE") {
      rejected(HttpReceiveFailure::kMethod, 405);
      SendError(fd, 405, "method not allowed", {}, "method_not_allowed",
                "method", "GET, DELETE");
    } else if (!body.empty()) {
      rejected(HttpReceiveFailure::kUnexpectedBody, 400);
      SendError(fd, 400, "model query/delete body is not supported", {},
                "unexpected_body", "body");
    } else {
      HandleModel(fd, method, path);
    }
    return;
  }
  if (path == "/healthz" || path == "/livez" || path == "/readyz" ||
      path == "/v1/models" || path == "/v1/capabilities" ||
      path == "/v1/config" || path == "/metrics")
    expected = "GET";
  else if (path == "/v1/chat/completions" || path == "/v1/chat/preflight" ||
           path == "/v1/completions" || path == "/v1/responses" ||
           path == "/v1/moderations" || path == "/v1/decisions" || path == "/v1/embeddings" ||
           path == "/v1/audio/speech" || path == "/v1/requests/cancel")
    expected = "POST";
  if (!expected.empty() && method != expected) {
    rejected(HttpReceiveFailure::kMethod, 405);
    SendError(fd, 405, "method not allowed", {}, "method_not_allowed", "method",
              expected);
    return;
  }
  if (expected == "GET" && !body.empty()) {
    rejected(HttpReceiveFailure::kUnexpectedBody, 400);
    SendError(fd, 400, "GET body is not supported", {}, "unexpected_body",
              "body");
    return;
  }
  if (path == "/livez" && method == "GET") {
    HandleReadiness(fd, true);
    return;
  }
  if (path == "/readyz" && method == "GET") {
    HandleReadiness(fd, false);
    return;
  }
  if (path == "/healthz" && method == "GET") {
    HandleHealth(fd);
    return;
  }
  if (path == "/v1/models" && method == "GET") {
    HandleModels(fd);
    return;
  }
  if (path == "/v1/capabilities" && method == "GET") {
    HandleCapabilities(fd);
    return;
  }
  if (path == "/v1/config" && method == "GET") {
    HandleConfig(fd);
    return;
  }
  if (path == "/v1/chat/preflight" && method == "POST") {
    HandlePreflight(fd, body);
    return;
  }
  if (path == "/metrics" && method == "GET") {
    HandleMetrics(fd);
    return;
  }
  if (path == "/v1/requests/cancel" && method == "POST") {
    io::Json req;
    if (!io::ParseJson(body, &req, true, kRequestJsonLimits).ok() ||
        !ValidateCancelContract(req)) {
      SendError(fd, 400, "invalid cancellation request", {},
                "invalid_cancellation");
      return;
    }
    const auto* context = detail::HttpResponseContext::Current(fd);
    const auto client = context ? context->Client().Id() : std::string_view();
    if (!requests_.Cancel(req.GetString("request_id"),
                          req.GetString("cancel_token"), client)) {
      SendError(fd, 404, "no cancellable request with these credentials", {},
                "request_not_found");
      return;
    }
    seq_cv_.notify_all();
    sched_cv_.notify_one();
    SendSimple(fd, 202, "Accepted", "{\"cancel_requested\":true}",
               "application/json");
    return;
  }
  if (path == "/v1/chat/completions" && method == "POST") {
    HandleChat(fd, body);
    return;
  }
  if (path == "/v1/completions" && method == "POST") {
    HandleChat(fd, body, GenerationApi::kCompletion);
    return;
  }
  if (path == "/v1/audio/speech" && method == "POST") {
    HandleSpeech(fd, body, headers.accept);
    return;
  }
  if (path == "/v1/embeddings" && method == "POST") {
    HandleEmbeddings(fd, body);
    return;
  }
  if (path == "/v1/decisions" && method == "POST") {
    HandleClassification(fd, body, ClassificationApi::kDecisions);
    return;
  }
  if (path == "/v1/moderations" && method == "POST") {
    HandleClassification(fd, body, ClassificationApi::kModerations);
    return;
  }
  if (path == "/v1/responses" && method == "POST") {
    HandleChat(fd, body, GenerationApi::kResponse);
    return;
  }
  rejected(HttpReceiveFailure::kRoute, 404);
  SendError(fd, 404, "route or method not found");
}

void ChatServer::HandleReadiness(int fd, bool liveness) {
  const auto memory = memory_admission_.Observe();
  const auto health = ObserveHealth(memory.state);
  const bool success = liveness ? health.live : health.Ready();
  SendSimple(fd, success ? 200 : 503, success ? "OK" : "Service Unavailable",
             health.ReadinessJson(), "application/json");
}

}  // namespace q4t::server
