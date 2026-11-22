#include "chat_server_internal.h"

#include <poll.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <future>

#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/live_media_call.h"
#include "q4t/server/native_crypto.h"
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
  std::unique_ptr<SipInvitation> invitation;
  LiveMediaProcess process;
  std::shared_ptr<LiveSideband> sideband;
  std::unique_ptr<LiveMediaCall> call;
  ~MediaOwner() {
    if (sideband) sideband->Finish(false);
  }

  void Run(std::stop_token stop,
           const std::function<ClientIdentity()>& current) {
    bool closing = false;
    while (!stop.stop_requested()) {
      const auto client = current();
      if (!client.Allowed()) return;
      if (invitation && invitation->Ended() && !closing) {
        if (invitation->Lost() || !call->EndTransport() || !process.Drain(stop))
          return;
        closing = true;
      }
      for (int i = 0; !closing && i < 16; ++i) {
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
        const bool drained = closing || process.Drain(stop);
        if (sideband) sideband->Finish(drained);
        return;
      }
      pollfd event{process.Fd(), POLLIN, 0};
      // After BYE the media child has drained. Only the backend terminal and
      // sideband remain; a closed child socket would wake immediately.
      if (poll(&event, closing ? 0 : 1, 5) < 0 && errno != EINTR) return;
    }
  }
};
}  // namespace

void ChatServer::HandleSipReject(int fd, std::string_view id,
                                     const std::string& body) {
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Scoped() || !clients_.OwnsIdentity(http->Client())) {
    detail::SendError(fd, 404, "Call not found", {}, "not_found");
    return;
  }
  Json request;
  const bool realtime = id.starts_with("rtc_");
  if (!(realtime && body.empty()) &&
      !io::ParseJson(body, &request, true, {8, 32, 4096, true})) {
    detail::SendError(fd, 400, "Invalid SIP rejection JSON", {}, "invalid_request");
    return;
  }
  if (realtime) {
    if (request.IsNull()) request = Object({});
    if (request.IsObject() && !request.Has("status_code"))
      Set(&request, "status_code", Number(603));
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

void ChatServer::HandleSipControl(int fd, std::string_view id,
                                  const std::string& body, bool transfer) {
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Scoped() || !clients_.OwnsIdentity(http->Client())) {
    detail::SendError(fd, 404, "Call not found", {}, "not_found");
    return;
  }
  Json request;
  if ((!body.empty() || transfer) &&
      !io::ParseJson(body, &request, true, {8, 32, 65536, true})) {
    detail::SendError(fd, 400, "Invalid call control JSON", {}, "invalid_request");
    return;
  }
  if (transfer) {
    ChatPlanError error;
    if (auto status = ValidateLiveWireShape(request, LiveShape::kSessionRefer, &error);
        !status) {
      detail::SendError(fd, error.http_status, status.message(), {}, error.code, error.param);
      return;
    }
  } else if (!body.empty() && (!request.IsObject() || !request.object.empty())) {
    detail::SendError(fd, 400, "Hangup accepts no parameters", {}, "invalid_request");
    return;
  }
  const auto deadline = Clock::now() + 5s;
  const auto reply = transfer
      ? sip_service_.Refer(http->Client().Id(), id, request.GetString("target_uri"), deadline)
      : sip_service_.Hangup(http->Client().Id(), id, deadline);
  if (!reply.ok()) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {}, reply.ClientCode());
    return;
  }
  detail::SendSimple(fd, 200, "OK", "", "application/json");
}

void ChatServer::HandleLiveMediaCreate(int fd, std::string_view source,
                                       const std::string& body,
                                       std::atomic<int>*& admission,
                                       std::string_view sip_id) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  const auto client = http->Client();
  const bool sip = !sip_id.empty();
  if (sip && (!client.Scoped() || !clients_.OwnsIdentity(client))) {
    detail::SendError(fd, 404, "Call not found", {}, "not_found");
    return;
  }
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
          sip ? LiveShape::kSessionAccept :
          source.empty() ? LiveShape::kMediaCreate : LiveShape::kSessionFork,
          &error);
      !status) {
    detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                      error.param);
    return;
  }
  const auto failed = [fd](const ObjectReply& reply) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode(), reply.value.GetString("param"));
  };
  std::unique_ptr<SipInvitation> invitation;
  if (sip) {
    const auto reply = sip_service_.Reserve(client.Id(), sip_id, &invitation);
    if (!reply.ok()) { failed(reply); return; }
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
  owner->invitation = std::move(invitation);
  const auto id = sip ? std::string(sip_id) : detail::NewRequestId("live_");
  const auto cancelled = [&] {
    return stop_requested_.load() || !clients_.OwnsIdentity(client) ||
           detail::ClientConnectionFailed(fd) ||
           (owner->invitation && owner->invitation->Ended());
  };
  std::shared_ptr<const Json> state;
  if (auto reply = LoadLiveFork(fd, client, source, &state); !reply.ok()) {
    failed(reply);
    return;
  }
  std::string answer;
  auto* telephone = owner->invitation.get();
  const auto negotiated = telephone
      ? owner->process.Negotiate(telephone->offer, telephone->local_address,
                                 cancelled, &answer, &telephone->peer_address)
      : owner->process.Negotiate(request.Find("transport")->GetString("sdp"),
                                 live_webrtc_address_, cancelled, &answer);
  struct WipeAnswer {
    std::string& value;
    ~WipeAnswer() { CryptoWipe(&value); }
  } wipe{answer};
  if (telephone) CryptoWipe(&telephone->offer);
  if (!negotiated.ok()) { failed(negotiated); return; }
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
  auto callbacks =
      MakeLiveCallbacks(client, id, message_bytes,
                        sip ? LiveTransport::kSip : LiveTransport::kWebRtc,
                        owner->sideband, std::move(state));
  if (sip) {
    callbacks.send_dtmf = [process = &owner->process](char key) {
      return process->Send('K', {&key, 1});
    };
  }
  owner->call = std::make_unique<LiveMediaCall>(
      id, expires, !sip && answer.find("\nm=application ") != answer.npos,
      std::move(callbacks), limits,
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
  const auto result = sip ? std::string{} : OutputJson(
      Object({{"session", Object({{"id", String(id)}})},
              {"transport", Object({{"type", String("webrtc")},
                                    {"sdp", String(std::move(answer))}})}}));
  std::promise<bool> published;
  decltype(live_media_threads_)::iterator reserved_slot;
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
    reserved_slot = slot;
    owner->slot.counter = std::exchange(admission, nullptr);
    if (slot->worker.joinable()) slot->worker.join();
    slot->client = client.Id();
    slot->session = id;
    slot->attachable.store(!sip);
    slot->sideband = owner->sideband;
    slot->finished.store(false);
    try {
      slot->worker = std::jthread([this, slot, owner,
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
  if (sip) {
    const auto reply = cancelled()
        ? object_detail::Error(409, "sip_call_conflict", "Call cancelled")
        : telephone->Answer(answer, Clock::now() + 5s);
    if (!reply.ok() || !owner->process.ActivateSip(cancelled) ||
        !owner->call->Receive('V', {})) {
      published.set_value(false);
      if (!reply.ok()) failed(reply);
      else detail::SendError(fd, 503, "Media activation failed", {}, "live_media_unavailable");
      return;
    }
    reserved_slot->attachable.store(true);
    published.set_value(true);
    detail::SendSimple(fd, 200, "OK", "", "application/json");
    return;
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
