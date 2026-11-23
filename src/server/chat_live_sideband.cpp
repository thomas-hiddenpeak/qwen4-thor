#include "chat_server_internal.h"

#include <algorithm>

#include "q4t/server/application_routes.h"
#include "q4t/server/live_sideband.h"
#include "q4t/server/websocket_protocol.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
void ChatServer::HandleLiveSideband(int fd, std::string_view path,
                                    const HttpRequestHeaders& headers,
                                    std::string_view initial,
                                    std::string_view accept) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  const auto client = http->Client();
  const auto call = RealtimeSidebandId(path);
  const bool realtime = !call.empty();
  const auto id = realtime ? call : LiveSidebandId(path);
  const auto query_at = path.find('?');
  if (!realtime && query_at != path.npos &&
      path.substr(query_at + 1) != "graceful_close=true" &&
      path.substr(query_at + 1) != "graceful_close=false") {
    detail::SendError(fd, 400, "Invalid Live attach query", {},
                      "invalid_request", "graceful_close");
    return;
  }
  std::shared_ptr<LiveSideband> stream;
  bool owned = false;
  {
    std::lock_guard lock(live_media_mu_);
    for (const auto& slot : live_media_threads_) {
      if (client.Scoped() && !slot.finished.load() && slot.attachable.load() &&
          slot.client == client.Id() && slot.session == id) {
        stream = slot.sideband.lock();
        owned = true;
        break;
      }
    }
  }
  if (!stream) {
    detail::SendError(
        fd, owned ? 503 : 404,
        owned ? "Live replay unavailable" : "Live session not found", {},
        owned ? "live_replay_unavailable" : "object_not_found");
    return;
  }
  HttpBodyLease memory(realtime_host_budget_);
  if (!memory.Acquire((128ULL << 20) + 2 * kWebSocketMessageLimit)) {
    detail::SendError(fd, 503, "Live observer memory unavailable", {},
                      "memory_pressure");
    return;
  }
  uint64_t next = 0;
  std::shared_ptr<const std::string> snapshot;
  if (auto reply = realtime ? stream->AttachRealtime(&next, &snapshot)
                            : stream->Attach(&next, LiveSideband::Clock::now());
      !reply.ok()) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode());
    return;
  }
  const size_t limit =
      std::min<size_t>(kWebSocketMessageLimit, client.Limits()->max_body_bytes);
  WebSocketOutput output(64ULL << 20, 64, LiveSideband::kReplayBytes);
  if (snapshot) {
    const auto initial_event =
        "{\"type\":\"session.created\",\"event_id\":\"" +
        detail::NewRequestId("event_") + "\",\"session\":" + *snapshot + "}";
    if (!output.TryPush(WebSocketOpcode::kText, initial_event)) {
      detail::SendError(fd, 503, "Current session exceeds output capacity", {},
                        "realtime_session_unavailable");
      return;
    }
    snapshot.reset();
  }
  if (!http->BeginWebSocket(
          accept, SelectWebSocketProtocol(path, headers.websocket_protocols)))
    return;
  bool completed = false;
  WebSocketSessionCallbacks cb;
  cb.tick = [&] {
    for (int i = 0; i < 16; ++i) {
      std::shared_ptr<const std::string> event;
      switch (stream->Next(next, &event)) {
        case LiveSideband::Read::kCompleted:
          completed = true;
          return true;
        case LiveSideband::Read::kPending:
          return true;
        case LiveSideband::Read::kLost:
          return false;
        case LiveSideband::Read::kEvent:
          if (!output.TryPush(WebSocketOpcode::kText, *event)) return true;
          ++next;
      }
    }
    return true;
  };
  cb.completed = [&] { return completed; };
  cb.message_ready = [&](size_t size) { return stream->HasSpace(size); };
  cb.message = [&](auto opcode, auto message) {
    const auto current = clients_.RecoveryIdentity(client.Index());
    return current.Scoped() && current.Id() == client.Id() &&
           message.size() <= current.Limits()->max_body_bytes &&
           stream->Enqueue(opcode, message);
  };
  cb.authorized = [&] {
    const auto current =
        clients_.Authenticate(headers.client_id, headers.gateway_token);
    return current.Scoped() && current.Id() == client.Id();
  };
  cb.stopped = [this] { return stop_requested_.load(); };
  WebSocketSessionLimits limits;
  limits.message_bytes = limit;
  RunWebSocketSession(fd, initial, output, cb, limits);
}
}  // namespace q4t::server
