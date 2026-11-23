#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_conversation_session.h"
#include "q4t/server/realtime_transcription_session.h"

namespace q4t::server {
using namespace object_detail;

io::Json ChatServer::RealtimeClientDefaults(int fd, std::string model) {
  auto value = DefaultRealtimeChatSettings(PlanningConfig(fd), std::move(model));
  if (!tts_model_dir_.empty())
    Set(&value, "output_modalities", Array({String("audio")}));
  if (!asr_model_dir_.empty())
    Set(&Field(value, "audio"), "input",
        Object({{"format", Object({{"type", String("audio/pcm")},
                                   {"rate", Number(24000)}})},
                {"transcription", Json()},
                {"turn_detection", vad_model_dir_.empty()
                                       ? Json()
                                       : Object({{"type", String("server_vad")}})}}));
  return value;
}

void ChatServer::IssueRealtimeSecret(int fd, const HttpRequestHeaders& headers,
                                     const std::string& body) {
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  const auto client = http->Client();
  std::string_view issuer = headers.authorization;
  if (client.Scoped()) {
    if (!issuer.starts_with("Q4T-Issuer ")) {
      detail::SendError(fd, 401, "Missing authenticated issuing key", {}, "invalid_api_key");
      return;
    }
    issuer.remove_prefix(11);
  } else {
    issuer = {};
  }
  Json request = Object();
  if (!body.empty() && !io::ParseJson(body, &request, true,
                                      {64, 65536, kWebSocketMessageLimit, true})) {
    detail::SendError(fd, 400, "Invalid credential request JSON", {}, "invalid_request", "body");
    return;
  }
  ChatPlanError failure;
  if (const auto status = ValidateRealtimeWireShape(
          request, RealtimeShape::kClientSecretCreate, &failure, "body"); !status) {
    detail::SendError(fd, failure.http_status, status.message(), {}, failure.code, failure.param);
    return;
  }
  if (!request.Has("session"))
    Set(&request, "session", Object({{"type", String("realtime")}}));
  auto& startup = Field(request, "session");
  const auto id = detail::NewRequestId("sess_");
  Json resource;
  ObjectReply reply;
  const size_t bytes = client.Scoped()
      ? std::min<size_t>(client.Limits()->max_body_bytes, kWebSocketMessageLimit)
      : kWebSocketMessageLimit;
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 64, kWebSocketMaximumLimit);
  if (startup.GetString("type") == "transcription") {
    if (asr_model_dir_.empty()) {
      detail::SendError(fd, 404, "Transcription model is not configured", {}, "model_not_found", "model");
      return;
    }
    RealtimeTranscriptionSettings settings;
    reply = UpdateRealtimeTranscriptionSettings(startup, false, {}, &settings);
    if (reply.ok()) {
      RealtimeTranscriptionSession preview(id, output, PrepareRealtimeInput(client),
                                           asr_limits_.max_audio_seconds, bytes);
      reply = preview.Configure(settings);
      if (reply.ok()) {
        resource = RealtimeTranscriptionSettingsJson(settings, false);
        Set(&resource, "id", String(id));
        Set(&resource, "object", String("realtime.transcription_session"));
      }
    }
  } else {
    RealtimeConversationSession preview(
        id, detail::NewRequestId("conv_"),
        RealtimeClientDefaults(fd, startup.GetString("model")), output,
        MakeRealtimeCallbacks(client), bytes);
    reply = preview.PreviewCredential(startup, &resource);
    if (reply.ok() && startup.Has("prompt") && resource.Has("prompt"))
      Set(&startup, "prompt", Field(resource, "prompt"));
  }
  if (reply.ok() && !reply.commit_unknown)
    reply = realtime_secrets_.Issue({client.Id(), headers.gateway_token, issuer},
                                    request, resource);
  if (!reply.ok() || reply.commit_unknown) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode(), reply.value.GetString("param"));
    return;
  }
  detail::SendSimple(fd, 200, "OK", OutputJson(reply.value), "application/json",
                     {}, {}, {}, true);
}
}  // namespace q4t::server
