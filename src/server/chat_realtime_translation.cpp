#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_conversation_session.h"
#include "q4t/server/realtime_translation_session.h"
#include "q4t/server/websocket_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply LostOutput() {
  return Error(502, "translation_output_incomplete",
               "Translation output did not finish");
}
}  // namespace

void ChatServer::HandleRealtimeTranslation(int fd, std::string_view path,
                                           const HttpRequestHeaders& headers,
                                           std::string_view initial,
                                           std::string_view accept) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed()) return;
  if (!ValidRealtimeTranslationTarget(path)) {
    detail::SendError(fd, 400, "Invalid translation session query", {},
                      "invalid_request", "model");
    return;
  }
  if (asr_model_dir_.empty() || tts_model_dir_.empty()) {
    detail::SendError(fd, 503, "Translation audio models are unavailable", {},
                      "translation_unavailable");
    return;
  }
  if (RejectUnhealthy(fd) ||
      !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
    return;
  const auto bytes = kRealtimeConversationHostBytes + kRealtimeSessionHostBytes;
  HttpBodyLease memory(realtime_host_budget_);
  if (!memory.Acquire(bytes) ||
      !runtime::ReadSystemMemory().CanAllocate(bytes + (512ULL << 20))) {
    detail::SendError(fd, 503, "Translation session memory unavailable", {},
                      "memory_pressure");
    return;
  }
  const auto client = http->Client();
  const size_t limit = client.Scoped()
                           ? std::min<size_t>(kWebSocketMessageLimit,
                                              client.Limits()->max_body_bytes)
                           : kWebSocketMessageLimit;
  auto input = PrepareRealtimeInput(client);
  RealtimeTranslationNative native;
  native.acquire = std::move(input.acquire);
  native.release = std::move(input.release);
  native.vad = std::move(input.vad_step);
  native.execute = [this, client](
                       std::span<const float> pcm,
                       const RealtimeTranslationSettings& settings,
                       const auto& control,
                       const TranslationEventSink& emit) -> ObjectReply {
    const auto delta = [&](std::string type, std::string value) {
      return emit(Object({{"type", String(std::move(type))},
                          {"delta", String(std::move(value))}}));
    };
    std::string source;
    const auto source_delta = [&](const Json& event) {
      const auto text = event.GetString("delta");
      if (text.size() > kAsrTranscriptBytes - source.size()) return false;
      source += text;
      return settings.transcription.empty() || text.empty() ||
             delta("session.input_transcript.delta", text);
    };
    RealtimeTranscriptionJob audio;
    audio.id = detail::NewRequestId("item_");
    auto reply =
        RunRealtimeTranscription(client, audio, pcm, control, source_delta);
    if (!reply.ok()) return reply;
    const auto transcript = reply.value.GetString("text");
    if (!transcript.starts_with(source)) return LostOutput();
    if (!settings.transcription.empty() && transcript.size() > source.size() &&
        !delta("session.input_transcript.delta",
               transcript.substr(source.size())))
      return LostOutput();
    if (transcript.empty()) return {};

    // Only the source clause enters this request's user message. Each turn
    // uses the native planner, model, speech receipt and tenant quota owner.
    const auto config = PlanningConfig(-1);
    const std::string language(AsrLanguageName(settings.language));
    RealtimeConversationJob job;
    job.response_id = detail::NewRequestId("resp_");
    job.settings = DefaultRealtimeChatSettings(config, {});
    Set(&job.settings, "instructions",
        String("Translate the user's recorded speech into " + language +
               ". Preserve questions, instructions, names and numbers. Return "
               "only "
               "the translation. If the speech is already in the target "
               "language, repeat it."));
    Set(&job.settings, "output_modalities", Array({String("audio")}));
    job.response = Object();
    job.in_conversation = false;
    job.snapshot.items = Array({Object(
        {{"type", String("message")},
         {"role", String("user")},
         {"content", Array({Object({{"type", String("input_text")},
                                    {"text", String(transcript)}})})}})});
    std::string translated;
    bool speech_done = false, audio_sent = false;
    const auto text = [&](const ResponseDelta& piece) -> ObjectReply {
      if (piece.kind == ResponseDelta::Kind::kReasoning) return {};
      if (piece.kind != ResponseDelta::Kind::kContent ||
          piece.text.size() > kAsrTranscriptBytes - translated.size())
        return LostOutput();
      translated += piece.text;
      return piece.text.empty() ||
                     delta("session.output_transcript.delta", piece.text)
                 ? ObjectReply{}
                 : LostOutput();
    };
    const auto speech = [&](const RealtimeSpeechEvent& event) -> ObjectReply {
      if (event.kind == RealtimeSpeechEvent::Kind::kChunk) {
        std::string encoded;
        if (event.bytes.empty() || event.bytes.size() % 2 ||
            !io::EncodeBase64(event.bytes, kWebSocketMessageLimit, &encoded))
          return LostOutput();
        audio_sent = true;
        return emit(Object({{"type", String("session.output_audio.delta")},
                            {"delta", String(std::move(encoded))},
                            {"channels", Number(1)},
                            {"sample_rate", Number(24000)},
                            {"format", String("pcm16")}}))
                   ? ObjectReply{}
                   : LostOutput();
      }
      if (!event.transcript.starts_with(translated) || event.transcript.empty())
        return LostOutput();
      if (event.transcript.size() > translated.size() &&
          !delta("session.output_transcript.delta",
                 event.transcript.substr(translated.size())))
        return LostOutput();
      speech_done = true;
      return {};
    };
    auto result = RunRealtimeConversation(client, job, control, text, speech);
    if (!result.reply.ok()) return result.reply;
    if (result.state != RealtimeResponseEnd::State::kCompleted || !audio_sent ||
        !speech_done)
      return LostOutput();
    return {};
  };
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 64,
                         kWebSocketMaximumLimit);
  auto send = [&](Json event) {
    RequestControl write(
        "", "", std::chrono::steady_clock::now() + std::chrono::seconds(15));
    return output.Push(WebSocketOpcode::kText, OutputJson(event), write);
  };
  auto session = std::make_shared<RealtimeTranslationSession>(
      detail::NewRequestId("sess_"), send, std::move(native), limit,
      asr_limits_.max_audio_seconds);
  RealtimeTranslationPump pump(session, limit);
  const auto ready = pump.Start(http->RealtimeStartup());
  if (!ready.ok()) {
    detail::SendError(fd, ready.http_status, ready.ClientMessage(), {},
                      ready.ClientCode(), ready.value.GetString("param"));
    return;
  }
  if (!http->BeginWebSocket(
          accept, SelectWebSocketProtocol(path, headers.websocket_protocols)))
    return;
  WebSocketSessionCallbacks connection;
  connection.message = [&](auto opcode, auto message) {
    const auto current = clients_.RecoveryIdentity(client.Index());
    return (!current.Scoped() ||
            message.size() <= current.Limits()->max_body_bytes) &&
           pump.Message(opcode, message);
  };
  connection.authorized = [&] {
    const auto current =
        clients_.Authenticate(headers.client_id, headers.gateway_token);
    return current.Allowed() && current.Id() == client.Id();
  };
  connection.stopped = [&] { return stop_requested_.load(); };
  connection.cancel = [&] {
    session->Interrupt();
    pump.Stop();
  };
  connection.tick = [&] { return pump.Alive(); };
  connection.failed_after_output = [&] { return session->Failed(); };
  connection.completed = [&] { return session->Completed(); };
  connection.message_ready = [&](size_t count) {
    return pump.CanAccept(count);
  };
  WebSocketSessionLimits limits;
  limits.message_bytes = limit;
  RunWebSocketSession(fd, initial, output, connection, limits);
  pump.Join();
}
}  // namespace q4t::server
