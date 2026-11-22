#include "chat_server_internal.h"

#include <poll.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <future>

#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/live_media_call.h"
#include "q4t/server/live_media_process.h"
#include "q4t/server/live_recording_store.h"
#include "q4t/server/live_responses_settings.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

struct ConnectionSlot {
  std::atomic<int>* counter = nullptr;
  ~ConnectionSlot() {
    if (counter) counter->fetch_sub(1);
  }
};

struct MediaOwner {
  explicit MediaOwner(HttpBodyBudget& budget) : memory(budget) {}
  ConnectionSlot slot;
  ClientLease permit;
  HttpBodyLease memory;
  LiveMediaProcess process;
  std::shared_ptr<LiveSideband> sideband;
  std::unique_ptr<LiveMediaCall> call;
  ~MediaOwner() {
    if (sideband) sideband->Finish(false);
  }

  void Run(std::stop_token stop,
           const std::function<ClientIdentity()>& current) {
    while (!stop.stop_requested()) {
      const auto client = current();
      if (!client.Allowed()) return;
      for (int i = 0; i < 16; ++i) {
        char type;
        std::string_view payload;
        const auto read = process.Receive(&type, &payload);
        if (read == LiveMediaProcess::Read::kPending) break;
        if (read == LiveMediaProcess::Read::kClosed ||
            (type == 'D' && client.Scoped() &&
             payload.size() > client.Limits()->max_body_bytes) ||
            !call->Receive(type, payload))
          return;
      }
      if (!call->Tick(Clock::now())) return;
      if (call->Finished()) {
        const bool drained = process.Drain(stop);
        if (sideband) sideband->Finish(drained);
        return;
      }
      pollfd event{process.Fd(), POLLIN, 0};
      if (poll(&event, 1, 5) < 0 && errno != EINTR) return;
    }
  }
};
}  // namespace

void ChatServer::HandleLiveSipReject(int fd, std::string_view id,
                                     const std::string& body) {
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Scoped() || !clients_.OwnsIdentity(http->Client())) {
    detail::SendError(fd, 404, "Call not found", {}, "not_found");
    return;
  }
  Json request;
  if (!io::ParseJson(body, &request, true, {8, 32, 4096, true})) {
    detail::SendError(fd, 400, "Invalid SIP rejection JSON", {}, "invalid_request");
    return;
  }
  ChatPlanError error;
  if (auto status = ValidateLiveWireShape(request, LiveShape::kSessionReject, &error);
      !status) {
    detail::SendError(fd, error.http_status, status.message(), {}, error.code, error.param);
    return;
  }
  const auto reply = sip_service_.Reject(http->Client().Id(), id,
      request.GetInt("status_code"), std::chrono::steady_clock::now() + std::chrono::seconds(5));
  if (!reply.ok()) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {}, reply.ClientCode());
    return;
  }
  detail::SendSimple(fd, 200, "OK", "", "application/json");
}

void ChatServer::HandleLiveMediaCreate(int fd, std::string_view source,
                                       const std::string& body,
                                       std::atomic<int>*& admission) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  const auto client = http->Client();
  const size_t message_bytes =
      client.Scoped() ? std::min<size_t>(kWebSocketMessageLimit,
                                         client.Limits()->max_body_bytes)
                      : kWebSocketMessageLimit;
  Json request;
  if (body.size() > message_bytes) {
    detail::SendError(fd, 413, "Live request exceeds its byte limit", {},
                      "client_body_limit_exceeded");
    return;
  }
  if (!io::ParseJson(body, &request, true,
                     {.max_depth = 64,
                      .max_values = 65536,
                      .max_string_bytes = message_bytes,
                      .reject_duplicate_keys = true})) {
    detail::SendError(fd, 400, "Invalid Live request JSON", {},
                      "invalid_request");
    return;
  }
  ChatPlanError error;
  if (auto status = ValidateLiveWireShape(
          request,
          source.empty() ? LiveShape::kMediaCreate : LiveShape::kSessionFork,
          &error);
      !status) {
    detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                      error.param);
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
  auto owner = std::make_shared<MediaOwner>(realtime_host_budget_);
  const size_t host_bytes =
      kRealtimeConversationHostBytes + kRealtimeConversationInputHostBytes +
      (16ULL << 20) + (source.empty() ? 0 : 3 * kLiveForkStateBytes);
  if (!owner->memory.Acquire(host_bytes) ||
      !runtime::ReadSystemMemory().CanAllocate(host_bytes + (512ULL << 20))) {
    detail::SendError(fd, 503, "Live host memory unavailable", {},
                      "memory_pressure");
    return;
  }
  const auto id = detail::NewRequestId("live_");
  const auto cancelled = [&] {
    return stop_requested_.load() || !clients_.OwnsIdentity(client) ||
           detail::ClientConnectionFailed(fd);
  };
  const auto failed = [fd](const ObjectReply& reply) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode(), reply.value.GetString("param"));
  };
  std::shared_ptr<const Json> state;
  if (auto reply = LoadLiveFork(fd, client, source, &state); !reply.ok()) {
    failed(reply);
    return;
  }
  std::string answer;
  if (const auto reply =
          owner->process.Negotiate(request.Find("transport")->GetString("sdp"),
                                   live_webrtc_address_, cancelled, &answer);
      !reply.ok()) {
    failed(reply);
    return;
  }
  LiveSessionLimits limits;
  limits.message_bytes = message_bytes;
  limits.lifetime = 3540s;
  limits.drain = 45s;
  const auto expires = std::chrono::duration_cast<std::chrono::seconds>(
                           (std::chrono::system_clock::now() + limits.lifetime)
                               .time_since_epoch())
                           .count();
  constexpr auto journal_budget =
      LiveSideband::kReplayBytes + LiveSideband::kCommandBytes + (8ULL << 20);
  if (client.Scoped()) {
    try {
      auto memory = std::make_unique<HttpBodyLease>(realtime_host_budget_);
      if (memory->Acquire(journal_budget))
        owner->sideband = std::make_shared<LiveSideband>(std::move(memory));
    } catch (const std::bad_alloc&) {
      // Only attaching a sideband requires a replay owner.
    }
  }
  owner->call = std::make_unique<LiveMediaCall>(
      id, expires, answer.find("\nm=application ") != answer.npos,
      MakeLiveCallbacks(client, id, message_bytes, LiveTransport::kWebRtc,
                        owner->sideband, std::move(state)),
      limits,
      [process = &owner->process](char type, auto data) {
        return process->Send(type, data);
      },
      owner->sideband);
  const auto* startup = request.Find("session");
  if (const auto reply = owner->call->Start(startup ? *startup : Object());
      !reply.ok()) {
    failed(reply);
    return;
  }
  const auto result = OutputJson(
      Object({{"session", Object({{"id", String(id)}})},
              {"transport", Object({{"type", String("webrtc")},
                                    {"sdp", String(std::move(answer))}})}}));
  std::promise<bool> published;
  {
    std::lock_guard lock(live_media_mu_);
    const auto slot = std::ranges::find_if(
        live_media_threads_, [](auto& entry) { return entry.finished.load(); });
    if (stop_requested_.load() || slot == live_media_threads_.end() ||
        admission != &realtime_conns_ ||
        !http->TransferClientLease(owner->permit)) {
      detail::SendError(fd, 503, "Live media owner unavailable", {},
                        "live_media_unavailable");
      return;
    }
    owner->slot.counter = std::exchange(admission, nullptr);
    if (slot->worker.joinable()) slot->worker.join();
    slot->client = client.Id();
    slot->session = id;
    slot->sideband = owner->sideband;
    slot->finished.store(false);
    try {
      slot->worker = std::jthread([this, slot, owner = std::move(owner),
                                   gate = published.get_future(),
                                   client](std::stop_token stop) mutable {
        try {
          if (gate.get())
            owner->Run(stop, [this, &client] {
              if (stop_requested_.load() || !clients_.OwnsIdentity(client))
                return ClientIdentity{};
              return clients_.RecoveryIdentity(client.Index());
            });
        } catch (...) {
          std::fputs("[live] media owner interrupted\n", stderr);
        }
        owner.reset();
        slot->finished.store(true);
      });
    } catch (...) {
      slot->finished.store(true);
      throw;
    }
  }
  published.set_value(detail::SendSimple(
      fd, 201, "Created", result, "application/json", {Clock::now() + 5s}));
}

void ChatServer::JoinLiveMedia() {
  std::lock_guard lock(live_media_mu_);
  for (auto& entry : live_media_threads_) entry.worker.request_stop();
  for (auto& entry : live_media_threads_)
    if (entry.worker.joinable()) entry.worker.join();
}
}  // namespace q4t::server
