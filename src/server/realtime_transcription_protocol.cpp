#include "q4t/server/realtime_transcription_protocol.h"

#include <algorithm>
#include <array>
#include <cmath>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/websocket.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string param, std::string message,
                    std::string code = "invalid_request_error") {
  auto result = Error(400, std::move(code), std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
ObjectReply Fields(const Json& object,
                   std::initializer_list<std::string_view> fields,
                   std::string_view parent) {
  std::string param;
  const auto status = InputFields(object, fields, &param, parent);
  return status ? ObjectReply{} : Invalid(param, status.message());
}
bool Text(const Json& value, size_t maximum) {
  return value.IsString() && value.str.size() <= maximum &&
         value.str.find('\0') == value.str.npos;
}
bool Language(const Json& value) {
  return Text(value, 3) &&
         (value.str.empty() || value.str == "yue" ||
          (value.str.size() == 2 &&
           std::all_of(value.str.begin(), value.str.end(),
                       [](char c) { return c >= 'a' && c <= 'z'; })));
}
ObjectReply DecodeAudio(std::string_view text, std::string* output) {
  if (text.size() % 4)
    return Invalid("audio", "Audio must be canonical base64");
  const size_t groups = text.size() / 4;
  const size_t padding =
      text.empty()
          ? 0
          : size_t(text.back() == '=') +
                size_t(text.size() > 1 && text[text.size() - 2] == '=');
  const size_t size = groups * 3 - padding;
  if (size > 15ULL << 20)
    return Invalid("audio", "Audio append exceeds its byte budget",
                   "audio_too_large");
  std::string decoded;
  decoded.reserve(size);
  for (size_t i = 0; i < text.size(); i += 4) {
    std::array<unsigned, 4> code{};
    for (size_t j = 0; j < 4; ++j) {
      const auto value = io::kBase64Alphabet.find(text[i + j]);
      if (value == std::string_view::npos) {
        if (i + 4 != text.size() || j < 4 - padding || text[i + j] != '=')
          return Invalid("audio", "Audio must be canonical base64");
      } else
        code[j] = value;
    }
    if (i + 4 == text.size() &&
        ((padding == 2 && code[1] % 16) || (padding == 1 && code[2] % 4)))
      return Invalid("audio", "Audio has noncanonical base64 padding");
    const auto value =
        (code[0] << 18) | (code[1] << 12) | (code[2] << 6) | code[3];
    for (const int shift : {16, 8, 0})
      if (decoded.size() < size)
        decoded.push_back(char((value >> shift) & 255));
  }
  *output = std::move(decoded);
  return {};
}
ObjectReply Transcription(const Json& object, AsrRequest* request,
                          std::string* delay, std::string_view parent) {
  auto reply = Fields(
      object, {"model", "language", "languages", "prompt", "keywords", "delay"},
      parent);
  if (!reply.ok()) return reply;
  for (const auto& [name, value] : object.object) {
    const auto param = InputParamPath(parent, name);
    if (name == "model" || name == "prompt" || name == "delay") {
      if (!Text(value, name == "prompt" ? kAsrPromptBytes : 128) ||
          (name == "model" && value.str.empty()))
        return Invalid(param, "Invalid transcription text field");
      if (name == "model")
        request->model = value.str;
      else if (name == "prompt")
        request->prompt = value.str;
      else {
        if (value.str != "minimal" && value.str != "low" &&
            value.str != "medium" && value.str != "high" &&
            value.str != "xhigh")
          return Invalid(param, "Invalid transcription delay");
        *delay = value.str;
      }
    } else if (name == "language") {
      if (!Language(value))
        return Invalid(param, "Invalid transcription language");
      request->language = value.str;
    } else {
      if (!value.IsArray() || value.array.size() > 64)
        return Invalid(param, "Invalid transcription hints");
      std::vector<std::string> values;
      for (const auto& entry : value.array) {
        if (!(name == "languages" ? Language(entry) : Text(entry, 256)) ||
            entry.str.empty() ||
            std::find(values.begin(), values.end(), entry.str) != values.end())
          return Invalid(param, "Invalid or duplicate transcription hint");
        values.push_back(entry.str);
      }
      (name == "languages" ? request->languages : request->keywords) =
          std::move(values);
    }
  }
  if (!request->language.empty() && !request->languages.empty() &&
      std::find(request->languages.begin(), request->languages.end(),
                request->language) == request->languages.end())
    return Invalid(std::string(parent),
                   "Transcription language hints conflict");
  return {};
}
ObjectReply Format(const Json& value, bool legacy, RealtimeAudioFormat* output,
                   std::string_view parent) {
  std::string type;
  if (legacy) {
    if (!value.IsString())
      return Invalid(std::string(parent), "Invalid audio format");
    type = value.str;
  } else {
    auto reply = Fields(value, {"type", "rate"}, parent);
    if (!reply.ok()) return reply;
    const auto previous =
        output->encoding == RealtimeAudioEncoding::kPcm16   ? "audio/pcm"
        : output->encoding == RealtimeAudioEncoding::kMuLaw ? "audio/pcmu"
                                                            : "audio/pcma";
    type = value.GetString("type", previous);
    if (value.Has("type") && !value.Find("type")->IsString())
      return Invalid(std::string(parent), "Invalid audio format type");
    if (const auto* rate = value.Find("rate");
        rate && (type != "audio/pcm" || !IsInputInteger(*rate) ||
                 rate->AsInt() != 24000))
      return Invalid(InputParamPath(parent, "rate"),
                     "PCM audio requires 24000 Hz");
  }
  if (type == (legacy ? "pcm16" : "audio/pcm"))
    *output = {};
  else if (type == (legacy ? "g711_ulaw" : "audio/pcmu"))
    *output = {RealtimeAudioEncoding::kMuLaw, 8000};
  else if (type == (legacy ? "g711_alaw" : "audio/pcma"))
    *output = {RealtimeAudioEncoding::kALaw, 8000};
  else
    return Invalid(std::string(parent), "Invalid realtime audio format");
  return {};
}
ObjectReply Turn(const Json& value, RealtimeTranscriptionSettings* settings,
                 std::string_view parent) {
  using T = RealtimeTranscriptionSettings::Turn;
  if (value.IsNull()) {
    settings->turn = T::kManual;
    return {};
  }
  auto reply = Fields(value,
                      {"type", "threshold", "prefix_padding_ms",
                       "silence_duration_ms", "eagerness"},
                      parent);
  if (!reply.ok()) return reply;
  const auto type = value.GetString("type");
  if (type != "server_vad" && type != "semantic_vad")
    return Invalid(InputParamPath(parent, "type"),
                   "Invalid turn detection type");
  settings->turn = type == "server_vad" ? T::kServerVad : T::kSemanticVad;
  for (const auto& [name, v] : value.object) {
    if (name == "type") continue;
    const auto param = InputParamPath(parent, name);
    if (name == "eagerness") {
      if (type != "semantic_vad" || !v.IsString() ||
          (v.str != "auto" && v.str != "low" && v.str != "medium" &&
           v.str != "high"))
        return Invalid(param, "Invalid semantic VAD eagerness");
      settings->eagerness = v.str;
    } else {
      if (type != "server_vad")
        return Invalid(param, "Field requires server VAD");
      if (name == "threshold") {
        if (!v.IsNumber() || !std::isfinite(v.number) || v.number < 0 ||
            v.number > 1)
          return Invalid(param, "Invalid VAD threshold");
        settings->request.chunking.threshold = v.number;
      } else {
        if (!IsInputInteger(v) || v.AsInt(-1) < 0 || v.AsInt() > 30000)
          return Invalid(param, "Invalid VAD duration");
        (name == "prefix_padding_ms"
             ? settings->request.chunking.prefix_padding_ms
             : settings->request.chunking.silence_duration_ms) = v.AsInt();
      }
    }
  }
  return {};
}
}  // namespace

ObjectReply ParseRealtimeTranscriptionCommand(
    std::string_view message, size_t message_bytes,
    RealtimeTranscriptionCommand* output) {
  if (!output || message_bytes == 0 || message_bytes > kWebSocketMessageLimit)
    return Error(500, "realtime_owner_invalid",
                 "Invalid realtime command owner");
  if (message.size() > message_bytes)
    return Invalid("event", "Realtime event exceeds its byte budget",
                   "event_too_large");
  Json value;
  const io::JsonParseLimits limits{12, 4096, message_bytes, true};
  auto parsed = io::ParseJson(std::string(message), &value, true, limits);
  if (!parsed || !value.IsObject())
    return Invalid("event", "Invalid realtime JSON event");
  RealtimeTranscriptionCommand command;
  if (const auto* id = value.Find("event_id")) {
    if (!Text(*id, 512) || id->str.empty())
      return Invalid("event_id", "Invalid event identity");
    command.event_id = id->str;
  }
  const auto failed = [&](ObjectReply error) {
    if (!command.event_id.empty())
      Set(&error.value, "event_id", String(command.event_id));
    return error;
  };
  const auto type = value.GetString("type");
  ObjectReply reply;
  if (type == "session.update" || type == "transcription_session.update") {
    reply = Fields(value, {"type", "event_id", "session"}, "event");
    if (!reply.ok()) return failed(std::move(reply));
    const auto* session = value.Find("session");
    if (!session || !session->IsObject())
      return failed(Invalid("session", "Session update requires an object"));
    command.kind = RealtimeTranscriptionCommandKind::kUpdate;
    command.legacy = type == "transcription_session.update";
    command.session = *session;
  } else if (type == "input_audio_buffer.append") {
    reply = Fields(value, {"type", "event_id", "audio"}, "event");
    if (!reply.ok()) return failed(std::move(reply));
    const auto* audio = value.Find("audio");
    if (!audio || !audio->IsString())
      return failed(Invalid("audio", "Audio append requires base64 text"));
    reply = DecodeAudio(audio->str, &command.audio);
    if (!reply.ok()) return failed(std::move(reply));
    command.kind = RealtimeTranscriptionCommandKind::kAppend;
  } else if (type == "input_audio_buffer.clear" ||
             type == "input_audio_buffer.commit") {
    reply = Fields(value, {"type", "event_id"}, "event");
    if (!reply.ok()) return failed(std::move(reply));
    command.kind = type == "input_audio_buffer.clear"
                       ? RealtimeTranscriptionCommandKind::kClear
                       : RealtimeTranscriptionCommandKind::kCommit;
  } else if (type == "conversation.item.delete" ||
             type == "conversation.item.retrieve") {
    reply = Fields(value, {"type", "event_id", "item_id"}, "event");
    if (!reply.ok()) return failed(std::move(reply));
    const auto* id = value.Find("item_id");
    if (!id || !Text(*id, 512) || id->str.empty())
      return failed(
          Invalid("item_id", "Conversation event requires an identity"));
    command.item_id = id->str;
    command.kind = type == "conversation.item.delete"
                       ? RealtimeTranscriptionCommandKind::kDelete
                       : RealtimeTranscriptionCommandKind::kRetrieve;
  } else
    return failed(Invalid("type",
                          "Event is not valid in a transcription session",
                          "invalid_event"));
  *output = std::move(command);
  return {};
}

ObjectReply UpdateRealtimeTranscriptionSettings(
    const io::Json& session, bool legacy,
    const RealtimeTranscriptionSettings& current,
    RealtimeTranscriptionSettings* output) {
  if (!output)
    return Error(500, "realtime_owner_invalid", "Missing session owner");
  auto reply = legacy
                   ? Fields(session,
                            {"include", "input_audio_format",
                             "input_audio_noise_reduction",
                             "input_audio_transcription", "turn_detection"},
                            "session")
                   : Fields(session, {"type", "audio", "include"}, "session");
  if (!reply.ok()) return reply;
  if (!legacy && session.Has("type") &&
      session.GetString("type") != "transcription")
    return Invalid("session.type", "Session type must remain transcription");
  auto settings = current;
  if (const auto* include = session.Find("include")) {
    if (!include->IsArray() || include->array.size() > 1 ||
        (!include->array.empty() &&
         (!include->array[0].IsString() ||
          include->array[0].str != "item.input_audio_transcription.logprobs")))
      return Invalid("session.include",
                     "Invalid transcription output includes");
    settings.request.logprobs = !include->array.empty();
  }
  const Json* input = &session;
  std::string parent = "session";
  if (!legacy) {
    input = nullptr;
    if (const auto* audio = session.Find("audio")) {
      reply = Fields(*audio, {"input"}, "session.audio");
      if (!reply.ok()) return reply;
      if (const auto* nested = audio->Find("input")) {
        reply = Fields(
            *nested,
            {"format", "noise_reduction", "transcription", "turn_detection"},
            "session.audio.input");
        if (!reply.ok()) return reply;
        input = nested;
        parent = "session.audio.input";
      }
    }
  }
  if (input) {
    const auto field = [&](const char* modern, const char* old) {
      return std::string(legacy ? old : modern);
    };
    const auto format = field("format", "input_audio_format");
    const auto transcript = field("transcription", "input_audio_transcription");
    const auto noise = field("noise_reduction", "input_audio_noise_reduction");
    if (const auto* v = input->Find(format)) {
      reply =
          Format(*v, legacy, &settings.format, InputParamPath(parent, format));
      if (!reply.ok()) return reply;
    }
    if (const auto* v = input->Find(transcript)) {
      reply = Transcription(*v, &settings.request, &settings.delay,
                            InputParamPath(parent, transcript));
      if (!reply.ok()) return reply;
    }
    if (const auto* v = input->Find(noise)) {
      using N = RealtimeTranscriptionSettings::Noise;
      if (v->IsNull())
        settings.noise = N::kNone;
      else {
        reply = Fields(*v, {"type"}, InputParamPath(parent, noise));
        if (!reply.ok()) return reply;
        const auto type = v->GetString("type");
        if (type != "near_field" && type != "far_field")
          return Invalid(InputParamPath(parent, noise),
                         "Invalid input noise reduction");
        settings.noise = type == "near_field" ? N::kNearField : N::kFarField;
      }
    }
    if (const auto* v = input->Find("turn_detection")) {
      reply = Turn(*v, &settings, InputParamPath(parent, "turn_detection"));
      if (!reply.ok()) return reply;
    }
  }
  *output = std::move(settings);
  return {};
}

io::Json RealtimeTranscriptionSettingsJson(
    const RealtimeTranscriptionSettings& s, bool legacy) {
  const auto format = s.format.encoding;
  const auto type = format == RealtimeAudioEncoding::kPcm16   ? "audio/pcm"
                    : format == RealtimeAudioEncoding::kMuLaw ? "audio/pcmu"
                                                              : "audio/pcma";
  Json audio_format =
      legacy ? String(format == RealtimeAudioEncoding::kPcm16   ? "pcm16"
                      : format == RealtimeAudioEncoding::kMuLaw ? "g711_ulaw"
                                                                : "g711_alaw")
             : Object({{"type", String(type)}});
  if (!legacy && format == RealtimeAudioEncoding::kPcm16)
    Set(&audio_format, "rate", Number(24000));
  auto transcript = Object({{"model", String(s.request.model)},
                            {"language", String(s.request.language)},
                            {"prompt", String(s.request.prompt)}});
  for (const auto name : {"languages", "keywords"}) {
    auto values = Array();
    for (const auto& text : std::string_view(name) == "languages"
                                ? s.request.languages
                                : s.request.keywords)
      values.array.push_back(String(text));
    Set(&transcript, name, std::move(values));
  }
  if (!s.delay.empty()) Set(&transcript, "delay", String(s.delay));
  const auto noise =
      s.noise == RealtimeTranscriptionSettings::Noise::kNone
          ? Json{}
          : Object(
                {{"type",
                  String(
                      s.noise ==
                              RealtimeTranscriptionSettings::Noise::kNearField
                          ? "near_field"
                          : "far_field")}});
  Json turn;
  if (s.turn == RealtimeTranscriptionSettings::Turn::kServerVad) {
    auto threshold = Number(0);
    threshold.number = s.request.chunking.threshold;
    threshold.str.clear();
    turn = Object(
        {{"type", String("server_vad")},
         {"threshold", threshold},
         {"prefix_padding_ms", Number(s.request.chunking.prefix_padding_ms)},
         {"silence_duration_ms",
          Number(s.request.chunking.silence_duration_ms)}});
  } else if (s.turn == RealtimeTranscriptionSettings::Turn::kSemanticVad) {
    turn = Object(
        {{"type", String("semantic_vad")}, {"eagerness", String(s.eagerness)}});
  }
  const auto include =
      s.request.logprobs
          ? Array({String("item.input_audio_transcription.logprobs")})
          : Array();
  if (legacy)
    return Object({{"input_audio_format", audio_format},
                   {"input_audio_transcription", transcript},
                   {"input_audio_noise_reduction", noise},
                   {"turn_detection", turn},
                   {"include", include}});
  return Object(
      {{"type", String("transcription")},
       {"include", include},
       {"audio", Object({{"input", Object({{"format", audio_format},
                                           {"transcription", transcript},
                                           {"noise_reduction", noise},
                                           {"turn_detection", turn}})}})}});
}
}  // namespace q4t::server
