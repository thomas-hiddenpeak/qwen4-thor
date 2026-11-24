#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/server/realtime_legacy_credentials.h"
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
                                     const std::string& body,
                                     RealtimeCredentialApi api) {
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
  std::optional<double> legacy_temperature;
  Json requested_modes;
  if (api != RealtimeCredentialApi::kClientSecret) {
    ObjectReply reply;
    if (api == RealtimeCredentialApi::kConversation) {
      if (const auto* modes = request.Find("modalities")) requested_modes = *modes;
      double temperature = 0.8;
      reply = LegacyConversationCredential(request, &request, &temperature);
      legacy_temperature = temperature;
    } else {
      reply = LegacyTranscriptionCredential(request, &request);
    }
    if (!reply.ok()) {
      detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                        reply.ClientCode(), reply.value.GetString("param"));
      return;
    }
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
  Json resource, legacy_resource;
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
        resource = preview.SessionResource();
        if (api == RealtimeCredentialApi::kTranscription) legacy_resource = preview.SessionResource(true);
      }
    }
  } else {
    RealtimeConversationSession preview(
        id, detail::NewRequestId("conv_"),
        RealtimeClientDefaults(fd, startup.GetString("model")), output,
        MakeRealtimeCallbacks(client, legacy_temperature), bytes);
    reply = preview.PreviewCredential(startup, &resource);
    if (reply.ok() && legacy_temperature)
      legacy_resource = LegacyConversationResource(resource,
          requested_modes.IsNull() ? nullptr : &requested_modes, *legacy_temperature);
    if (reply.ok() && startup.Has("prompt") && resource.Has("prompt"))
      Set(&startup, "prompt", Field(resource, "prompt"));
  }
  if (reply.ok() && !reply.commit_unknown)
    reply = realtime_secrets_.Issue({client.Id(), headers.gateway_token, issuer, legacy_temperature},
                                    request, resource);
  if (!reply.ok() || reply.commit_unknown) {
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), {},
                      reply.ClientCode(), reply.value.GetString("param"));
    return;
  }
  if (api != RealtimeCredentialApi::kClientSecret) {
    if (api == RealtimeCredentialApi::kTranscription)
      Set(&legacy_resource, "modalities", Array({String("text")}));
    Set(&legacy_resource, "client_secret",
        Object({{"value", Field(reply.value, "value")},
                {"expires_at", Field(reply.value, "expires_at")}}));
    reply.value = std::move(legacy_resource);
  }
  detail::SendSimple(fd, 200, "OK", OutputJson(reply.value), "application/json",
                     {}, {}, {}, true);
}
}  // namespace q4t::server
