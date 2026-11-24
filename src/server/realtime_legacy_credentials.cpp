#include "q4t/server/realtime_legacy_credentials.h"

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string param, std::string message) {
  auto reply = Error(400, "invalid_request_error", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}

void Copy(const Json& from, std::string_view source, Json* to,
          std::string_view target) {
  if (const auto* value = from.Find(std::string(source)))
    Set(to, std::string(target), *value);
}

ObjectReply AudioFormat(const Json& body, std::string_view field, Json* audio) {
  const auto* value = body.Find(std::string(field));
  if (!value) return {};
  Json format;
  if (value->IsString() && value->str == "pcm16")
    format = Object({{"type", String("audio/pcm")}, {"rate", Number(24000)}});
  else if (value->IsString() && value->str == "g711_ulaw")
    format = Object({{"type", String("audio/pcmu")}});
  else if (value->IsString() && value->str == "g711_alaw")
    format = Object({{"type", String("audio/pcma")}});
  else
    return Invalid(std::string(field), "Unknown Realtime audio format");
  Set(audio, "format", std::move(format));
  return {};
}

}  // namespace

void CopyLegacyRealtimeAudioFormat(const Json& audio, std::string_view key, Json* result) {
  const auto* format = audio.Find("format");
  if (!format) return;
  const auto type = format->GetString("type");
  if (type == "audio/pcm") Set(result, std::string(key), String("pcm16"));
  if (type == "audio/pcmu") Set(result, std::string(key), String("g711_ulaw"));
  if (type == "audio/pcma") Set(result, std::string(key), String("g711_alaw"));
}

ObjectReply LegacyRealtimeExpiry(const Json& body, std::string_view field,
                                 Json* expiry) {
  *expiry = Object({{"anchor", String("created_at")}, {"seconds", Number(60)}});
  const auto* secret = body.Find("client_secret");
  if (!secret) return {};
  std::string param;
  auto check = InputFields(*secret, {field}, &param, "client_secret");
  if (!check) return Invalid(param, check.message());
  const auto* when = secret->Find(std::string(field));
  if (!when) return {};
  const auto path = InputParamPath("client_secret", field);
  check = InputFields(*when, {"anchor", "seconds"}, &param, path);
  if (!check) return Invalid(param, check.message());
  if (when->Has("anchor") && when->GetString("anchor") != "created_at")
    return Invalid(path + ".anchor", "Expected created_at");
  if (const auto* ttl = when->Find("seconds")) {
    if (!IsInputInteger(*ttl) || ttl->AsInt() < 10 || ttl->AsInt() > 7200)
      return Invalid(path + ".seconds", "Expected 10 to 7200 seconds");
    Set(expiry, "seconds", *ttl);
  }
  return {};
}

ObjectReply LegacyConversationCredential(const Json& body, Json* grant,
                                         double* temperature) {
  std::string param;
  const auto check = InputFields(body,
      {"client_secret", "model", "instructions", "modalities", "voice",
       "input_audio_format", "output_audio_format", "input_audio_transcription",
       "input_audio_noise_reduction", "turn_detection", "tools", "tool_choice",
       "temperature", "max_response_output_tokens", "speed", "tracing",
       "prompt", "truncation"}, &param, "body");
  if (!check) return Invalid(param, check.message());
  Json expiry;
  auto reply = LegacyRealtimeExpiry(body, "expires_after", &expiry);
  if (!reply.ok()) return reply;
  const auto* sampling = body.Find("temperature");
  if (sampling && !(sampling->IsNumber() && sampling->number >= 0.6 &&
                    sampling->number <= 1.2))
    return Invalid("temperature", "Expected a number between 0.6 and 1.2");
  auto session = Object({{"type", String("realtime")}});
  for (const auto* k : {"model", "instructions", "tools", "tool_choice",
                        "tracing", "prompt", "truncation"})
    Copy(body, k, &session, k);
  Copy(body, "max_response_output_tokens", &session, "max_output_tokens");
  if (const auto* modes = body.Find("modalities")) {
    bool text = false, audio = false;
    if (!modes->IsArray() || modes->array.empty())
      return Invalid("modalities", "Expected text or audio");
    for (const auto& mode : modes->array) {
      if (mode.IsString() && mode.str == "text" && !text) text = true;
      else if (mode.IsString() && mode.str == "audio" && !audio) audio = true;
      else return Invalid("modalities", "Invalid or duplicate modality");
    }
    Set(&session, "output_modalities", Array({String(audio ? "audio" : "text")}));
  }
  auto input = Object(), output = Object(), audio = Object();
  reply = AudioFormat(body, "input_audio_format", &input);
  if (!reply.ok()) return reply;
  reply = AudioFormat(body, "output_audio_format", &output);
  if (!reply.ok()) return reply;
  Copy(body, "input_audio_transcription", &input, "transcription");
  Copy(body, "input_audio_noise_reduction", &input, "noise_reduction");
  Copy(body, "turn_detection", &input, "turn_detection");
  Copy(body, "speed", &output, "speed");
  Copy(body, "voice", &output, "voice");
  if (!input.object.empty()) Set(&audio, "input", std::move(input));
  if (!output.object.empty()) Set(&audio, "output", std::move(output));
  if (!audio.object.empty()) Set(&session, "audio", std::move(audio));
  *temperature = sampling ? sampling->number : 0.8;
  *grant = Object({{"session", std::move(session)}, {"expires_after", std::move(expiry)}});
  return {};
}

Json LegacyConversationResource(const Json& session, const Json* requested_modalities,
                                double temperature) {
  auto flat = Object();
  for (const auto* k : {"id", "object", "model", "instructions", "tools",
                        "tool_choice", "tracing", "prompt", "truncation"})
    Copy(session, k, &flat, k);
  Copy(session, "max_output_tokens", &flat, "max_response_output_tokens");
  Json number;
  number.type = Json::Type::kNumber;
  number.number = temperature;
  Set(&flat, "temperature", std::move(number));
  if (requested_modalities) Set(&flat, "modalities", *requested_modalities);
  else if (const auto* modes = session.Find("output_modalities")) {
    const bool audio = modes->IsArray() && !modes->array.empty() &&
                       modes->array.front().str == "audio";
    Set(&flat, "modalities", audio ? Array({String("text"), String("audio")}) : *modes);
  }
  if (const auto* audio = session.Find("audio")) {
    if (const auto* in = audio->Find("input")) {
      CopyLegacyRealtimeAudioFormat(*in, "input_audio_format", &flat);
      Copy(*in, "transcription", &flat, "input_audio_transcription");
      Copy(*in, "noise_reduction", &flat, "input_audio_noise_reduction");
      Copy(*in, "turn_detection", &flat, "turn_detection");
    }
    if (const auto* out = audio->Find("output")) {
      CopyLegacyRealtimeAudioFormat(*out, "output_audio_format", &flat);
      Copy(*out, "voice", &flat, "voice");
      Copy(*out, "speed", &flat, "speed");
    }
  }
  return flat;
}
}  // namespace q4t::server
