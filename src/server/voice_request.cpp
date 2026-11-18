#include "q4t/server/voice_request.h"

#include <algorithm>
#include <map>
#include <set>
#include <span>

#include "q4t/io/json.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/http_response.h"
#include "q4t/server/multipart.h"

namespace q4t::server {
namespace {
using Fields = std::map<std::string, std::string>;
Status Invalid(ChatPlanError* error, std::string param, std::string message,
               int status = 400) {
  if (error)
    *error = {status == 413 ? "request_too_large" : "invalid_request",
              std::move(param), status};
  return Status::Fail(std::move(message));
}
bool Utf8(std::string_view s) {
  return s.find('\0') == s.npos && detail::NormalizeUtf8(s) == s;
}
bool Text(std::string_view s, size_t limit) {
  return Utf8(s) && s.find_first_not_of(" \t\r\n\f\v") != s.npos &&
         size_t(std::count_if(s.begin(), s.end(), [](unsigned char c) {
           return (c & 0xc0) != 0x80;
         })) <= limit;
}
std::string Mime(std::string_view value) {
  value = value.substr(0, value.find(';'));
  while (!value.empty() && (value.back() == ' ' || value.back() == '\t'))
    value.remove_suffix(1);
  while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
    value.remove_prefix(1);
  std::string result(value);
  for (auto& c : result)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return result;
}
bool AudioMime(std::string_view value) {
  // Unspecified/octet-stream uploads still require successful content decoding.
  constexpr std::string_view types[] = {"",
                                        "application/octet-stream",
                                        "audio/mpeg",
                                        "audio/wav",
                                        "audio/x-wav",
                                        "audio/ogg",
                                        "audio/aac",
                                        "audio/flac",
                                        "audio/webm",
                                        "audio/mp4"};
  return std::find(std::begin(types), std::end(types), Mime(value)) !=
         std::end(types);
}
Status ParseFields(std::string_view content_type, std::string_view body,
                   std::span<const std::string_view> allowed,
                   std::string_view audio_field, Fields* fields,
                   VoiceAudio* audio, ChatPlanError* error, bool allow_json) {
  if (body.size() > kVoiceFormBytes)
    return Invalid(error, "body", "Voice form exceeds byte limit", 413);
  const auto known = [&](std::string_view name) {
    return std::find(allowed.begin(), allowed.end(), name) != allowed.end();
  };
  if (Mime(content_type) == "application/json" && allow_json) {
    if (body.size() > 64 * 1024)
      return Invalid(error, "body", "Voice JSON exceeds byte limit", 413);
    io::Json value;
    auto status = io::ParseJson(std::string(body), &value, false,
                                {4, 32, 16 * 1024, true});
    if (!status || !value.IsObject())
      return Invalid(error, "body", "Expected an unambiguous JSON object");
    for (const auto& [key, item] : value.object) {
      if (!known(key) || key == audio_field)
        return Invalid(error, key, "Unexpected voice field");
      if (!item.IsString() || !Utf8(item.str))
        return Invalid(error, key, "Expected UTF-8 text without NUL");
      fields->emplace(key, item.str);
    }
    return {};
  }
  std::string boundary;
  if (!MultipartBoundary(content_type, &boundary))
    return Invalid(error, "body", "Expected multipart/form-data");
  std::string name, value;
  std::set<std::string> seen;
  bool binary = false;
  MultipartCallbacks callbacks;
  callbacks.begin = [&](const MultipartPart& part) -> Status {
    name = part.name;
    value.clear();
    binary = !audio_field.empty() && name == audio_field;
    if (!known(name) || !seen.insert(name).second)
      return Invalid(error, name, "Unexpected or duplicate voice field");
    if (!binary && part.has_filename)
      return Invalid(error, name, "Only audio may contain a file");
    if (binary) {
      if (!Utf8(part.filename) || part.filename.size() > 1024)
        return Invalid(error, name, "Invalid audio filename");
      if (!AudioMime(part.content_type))
        return Invalid(error, name, "Invalid audio content type");
      audio->filename = part.filename;  // Metadata, never opened as a path.
      audio->content_type = part.content_type;
    }
    return {};
  };
  callbacks.data = [&](std::string_view bytes) -> Status {
    auto& target = binary ? audio->bytes : value;
    const size_t limit = binary ? kVoiceAudioBytes : 16 * 1024;
    if (bytes.size() > limit - target.size())
      return Invalid(error, name, "Voice field exceeds byte limit", 413);
    target.append(bytes);
    return {};
  };
  callbacks.end = [&]() -> Status {
    if (binary)
      return audio->bytes.empty() ? Invalid(error, name, "Audio is empty")
                                  : Status{};
    if (!Utf8(value))
      return Invalid(error, name, "Expected UTF-8 text without NUL");
    fields->emplace(name, value);
    return {};
  };
  MultipartDecoder decoder(boundary, kVoiceFormBytes, allowed.size(),
                           std::move(callbacks));
  auto status = decoder.Feed(body);
  if (status) status = decoder.Finish();
  if (!status && (!error || error->param.empty()))
    return Invalid(error, "body", "Malformed voice multipart form");
  return status;
}
}  // namespace

Status ParseVoiceCreateRequest(std::string_view content_type,
                               std::string_view body, VoiceCreateRequest* out,
                               ChatPlanError* error) {
  if (error) *error = {};
  if (!out) return Invalid(error, {}, "Missing voice request owner", 500);
  constexpr std::string_view allowed[] = {"type",         "name",   "consent",
                                          "audio_sample", "prompt", "model",
                                          "script_hint"};
  Fields fields;
  VoiceCreateRequest next;
  auto status = ParseFields(content_type, body, allowed, "audio_sample",
                            &fields, &next.audio_sample, error, true);
  if (!status) return status;
  if (!Text(fields["name"], 256))
    return Invalid(error, "name", "Expected a name of 1 to 256 characters");
  next.name = fields["name"];
  const auto type = fields.contains("type") ? fields["type"] : "audio_sample";
  if (type == "audio_sample") {
    for (const auto* key : {"prompt", "model", "script_hint"})
      if (fields.contains(key))
        return Invalid(error, key, "Prompt field conflicts with audio sample");
    if (!ValidApplicationId(fields["consent"]))
      return Invalid(error, "consent", "Expected a consent recording ID");
    if (next.audio_sample.bytes.empty())
      return Invalid(error, "audio_sample", "Expected a nonempty audio file");
    next.consent = fields["consent"];
  } else if (type == "prompt") {
    if (fields.contains("consent") || !next.audio_sample.bytes.empty())
      return Invalid(error, "type", "Audio sample fields conflict with prompt");
    next.type = VoiceType::kPrompt;
    if (!Text(fields["prompt"], 4096))
      return Invalid(error, "prompt", "Expected a nonempty voice description");
    next.prompt = fields["prompt"];
    if (fields.contains("model")) {
      if (!Text(fields["model"], 256))
        return Invalid(error, "model", "Expected a model identifier");
      next.model = fields["model"];
    }
    if (fields.contains("script_hint")) {
      if (!Text(fields["script_hint"], 2000))
        return Invalid(error, "script_hint", "Expected 1 to 2000 characters");
      next.script_hint = fields["script_hint"];
    }
  } else {
    return Invalid(error, "type", "Expected audio_sample or prompt");
  }
  *out = std::move(next);
  return {};
}

Status ParseVoiceConsentRequest(std::string_view content_type,
                                std::string_view body, VoiceConsentRequest* out,
                                ChatPlanError* error) {
  if (error) *error = {};
  if (!out) return Invalid(error, {}, "Missing consent request owner", 500);
  constexpr std::string_view allowed[] = {"name", "language", "recording"};
  Fields fields;
  VoiceConsentRequest next;
  auto status = ParseFields(content_type, body, allowed, "recording", &fields,
                            &next.recording, error, false);
  if (!status) return status;
  if (!Text(fields["name"], 256))
    return Invalid(error, "name", "Expected a name of 1 to 256 characters");
  if (!WellFormedVoiceLanguage(fields["language"]))
    return Invalid(error, "language", "Expected a BCP 47 language tag");
  if (next.recording.bytes.empty())
    return Invalid(error, "recording", "Expected a nonempty consent recording");
  next.name = fields["name"];
  next.language = fields["language"];
  *out = std::move(next);
  return {};
}

Status ParseVoiceConsentUpdate(std::string_view content_type,
                               std::string_view body, std::string* name,
                               ChatPlanError* error) {
  if (error) *error = {};
  if (!name) return Invalid(error, {}, "Missing consent name owner", 500);
  constexpr std::string_view allowed[] = {"name"};
  Fields fields;
  VoiceAudio unused;
  auto status = ParseFields(content_type, body, allowed, {}, &fields, &unused,
                            error, true);
  if (!status) return status;
  if (!Text(fields["name"], 256))
    return Invalid(error, "name", "Expected a name of 1 to 256 characters");
  *name = fields["name"];
  return {};
}
}  // namespace q4t::server
