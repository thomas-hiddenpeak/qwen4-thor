#include "q4t/server/speech_request.h"
#include "q4t/io/json.h"

#include <algorithm>
#include <cmath>


namespace q4t::server {
namespace {
Status Invalid(ChatPlanError* error, std::string param, std::string message,
               int code = 400) {
  if (error)
    *error = {code == 413 ? "request_too_large" : "invalid_request",
              std::move(param), code};
  return Status::Fail(std::move(message));
}
// ParseJson already validates UTF-8 scalars; count decoded characters rather
// than bytes, and reject embedded NUL before passing text to other components.
bool Text(const io::Json* value, size_t limit, bool empty = false) {
  return value && value->IsString() && (empty || !value->str.empty()) &&
         value->str.find('\0') == value->str.npos &&
         size_t(std::count_if(
             value->str.begin(), value->str.end(),
             [](unsigned char c) { return (c & 0xc0) != 0x80; })) <= limit;
}
constexpr std::pair<SpeechFormat, std::string_view> kFormats[] = {
    {SpeechFormat::kMp3, "mp3"}, {SpeechFormat::kOpus, "opus"},
    {SpeechFormat::kAac, "aac"}, {SpeechFormat::kFlac, "flac"},
    {SpeechFormat::kWav, "wav"}, {SpeechFormat::kPcm, "pcm"}};
}  // namespace

std::string_view SpeechFormatName(SpeechFormat format) {
  for (const auto& [value, name] : kFormats)
    if (value == format) return name;
  return {};
}
std::string_view SpeechContentType(SpeechFormat format) {
  switch (format) {
    case SpeechFormat::kMp3:
      return "audio/mpeg";
    case SpeechFormat::kOpus:
      return "audio/ogg";
    case SpeechFormat::kAac:
      return "audio/aac";
    case SpeechFormat::kFlac:
      return "audio/flac";
    case SpeechFormat::kWav:
      return "audio/wav";
    case SpeechFormat::kPcm:
      return "audio/pcm";
  }
  return {};
}

Status ParseSpeechRequest(std::string_view body, SpeechRequest* out,
                          ChatPlanError* error) {
  if (error) *error = {};
  if (!out) return Invalid(error, {}, "Missing speech request owner", 500);
  if (body.size() > kSpeechBodyBytes)
    return Invalid(error, "body", "Speech request exceeds byte budget", 413);
  io::Json request;
  auto status = io::ParseJson(std::string(body), &request, false,
                              {.max_depth = 4,
                               .max_values = 64,
                               .max_string_bytes = 64 * 1024,
                               .reject_duplicate_keys = true});
  if (!status || !request.IsObject())
    return Invalid(error, "body", "Expected an unambiguous JSON object");
  constexpr std::string_view fields[] = {
      "input",           "model", "voice",        "instructions",
      "response_format", "speed", "stream_format"};
  for (const auto& [key, value] : request.object) {
    (void)value;
    if (std::find(std::begin(fields), std::end(fields), key) ==
        std::end(fields))
      return Invalid(error, key, "Unknown speech request field");
  }
  SpeechRequest next;
  for (const auto* key : {"model", "input"}) {
    if (!Text(request.Find(key), key == std::string_view("input") ? 4096 : 256))
      return Invalid(error, key,
                     "Expected nonempty text within character limit");
  }
  next.model = request.GetString("model");
  next.input = request.GetString("input");
  if (const auto* value = request.Find("instructions")) {
    if (!Text(value, 4096, true))
      return Invalid(error, "instructions", "Expected at most 4096 characters");
    next.instructions = value->str;
  }
  const auto* voice = request.Find("voice");
  if (voice && voice->IsObject()) {
    if (voice->object.size() != 1 || voice->object[0].first != "id" ||
        !Text(voice->Find("id"), 256))
      return Invalid(error, "voice.id", "Expected a custom voice id object");
    next.voice_id = true;
    next.voice = voice->GetString("id");
  } else {
    if (!Text(voice, 256))
      return Invalid(error, "voice", "Expected a voice name or id object");
    next.voice = voice->str;
  }
  if (const auto* format = request.Find("response_format")) {
    bool found = false;
    if (format->IsString())
      for (const auto& [value, name] : kFormats)
        if (format->str == name) {
          next.format = value;
          found = true;
        }
    if (!found)
      return Invalid(error, "response_format", "Unknown audio format");
  }
  if (const auto* speed = request.Find("speed")) {
    if (!speed->IsNumber() || !std::isfinite(speed->number) ||
        speed->number < 0.25 || speed->number > 4)
      return Invalid(error, "speed", "Expected speed between 0.25 and 4");
    next.speed = speed->number;
  }
  if (const auto* stream = request.Find("stream_format")) {
    if (!stream->IsString() || (stream->str != "sse" && stream->str != "audio"))
      return Invalid(error, "stream_format", "Expected audio or sse");
    next.sse = stream->str == "sse";
  }
  *out = std::move(next);
  return {};
}
}  // namespace q4t::server
