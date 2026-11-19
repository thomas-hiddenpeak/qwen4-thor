#include "q4t/server/chat_audio_output.h"

#include <algorithm>
#include <cmath>
#include <utility>

#include "q4t/io/base64.h"
#include "q4t/server/output_schema.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string param, const char* message) {
  if (error) *error = {"invalid_request", std::move(param), 400};
  return Status::Fail(message);
}
bool AudioId(std::string_view id) {
  return id.starts_with("audio_") && id.size() > 6 && id.size() <= 256 &&
         std::all_of(id.begin(), id.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
         });
}
Status Delta(io::Json fields, std::string* out) {
  if (!out) return Status::Fail("Missing Chat audio delta owner");
  *out = OutputJson(Object({{"audio", std::move(fields)}}));
  return {};
}
}  // namespace

std::string_view ChatAudioFormatName(SpeechFormat format) {
  return format == SpeechFormat::kPcm ? "pcm16" : SpeechFormatName(format);
}

Status ParseChatAudioOptions(const io::Json& request, ChatAudioOptions* out,
                             ChatPlanError* error) {
  if (error) *error = {};
  if (!out || !request.IsObject())
    return Invalid(error, "audio", "Missing Chat audio request owner");
  ChatAudioOptions next;
  if (const auto* modes = request.Find("modalities");
      modes && !modes->IsNull()) {
    if (!modes->IsArray() || modes->array.empty() || modes->array.size() > 2)
      return Invalid(error, "modalities", "Expected distinct text/audio modes");
    next.text = false;
    for (const auto& mode : modes->array) {
      if (!mode.IsString() || (mode.str != "text" && mode.str != "audio") ||
          (mode.str == "text" ? std::exchange(next.text, true)
                              : std::exchange(next.enabled, true)))
        return Invalid(error, "modalities",
                       "Expected distinct text/audio modes");
    }
  }
  const auto* audio = request.Find("audio");
  if (!next.enabled) {
    if (audio && !audio->IsNull())
      return Invalid(error, "audio", "Audio options require audio modality");
    *out = std::move(next);
    return {};
  }
  if (!audio || !audio->IsObject())
    return Invalid(error, "audio", "Audio modality requires output options");
  for (const auto& [name, value] : audio->object) {
    (void)value;
    if (name != "format" && name != "voice")
      return Invalid(error, "audio." + name, "Unknown Chat audio option");
  }
  const auto* format = audio->Find("format");
  if (!format || !format->IsString() || format->str == "pcm")
    return Invalid(error, "audio.format", "Expected a Chat audio format");
  const auto* voice = audio->Find("voice");
  if (!voice)
    return Invalid(error, "audio.voice", "Audio output requires a voice");
  // Reuse Speech's strict format/voice Unicode and object validation. This
  // constant input exists only to parse options and is cleared before publish.
  const auto options =
      Object({{"model", String("native-chat-speech")},
              {"input", String("x")},
              {"voice", *voice},
              {"response_format",
               String(format->str == "pcm16" ? "pcm" : format->str)}});
  ChatPlanError parsed;
  auto status = ParseSpeechRequest(OutputJson(options), &next.speech, &parsed);
  if (!status) {
    parsed.param = parsed.param == "response_format" ? "audio.format"
                                                     : "audio." + parsed.param;
    if (error) *error = std::move(parsed);
    return status;
  }
  next.speech.model.clear();
  next.speech.input.clear();
  *out = std::move(next);
  return {};
}

Status ValidateChatSpeechRequest(const SpeechRequest& request,
                                 SpeechRequest* validated,
                                 ChatPlanError* error) {
  if (!std::isfinite(request.speed))
    return Invalid(error, "speed", "Speech speed must be finite");
  Json speed;
  speed.type = Json::Type::kNumber;
  speed.number = request.speed;
  auto wire = Object(
      {{"model", String(request.model)},
       {"input", String(request.input)},
       {"voice", request.voice_id ? Object({{"id", String(request.voice)}})
                                  : String(request.voice)},
       {"response_format", String(std::string(SpeechFormatName(request.format)))},
       {"speed", std::move(speed)}});
  if (!request.instructions.empty())
    Set(&wire, "instructions", String(request.instructions));
  return ParseSpeechRequest(OutputJson(wire), validated, error);
}

Status ChatAudioHistoryId(const io::Json& message, std::string* id,
                          ChatPlanError* error) {
  if (!id || !message.IsObject())
    return Invalid(error, "audio", "Invalid audio history message");
  const auto* audio = message.Find("audio");
  if (!audio || audio->IsNull()) {
    id->clear();
    return {};
  }
  if (message.GetString("role") != "assistant" || !audio->IsObject() ||
      audio->object.size() != 1 || !audio->Find("id") ||
      !audio->Find("id")->IsString() || !AudioId(audio->GetString("id")))
    return Invalid(error, "audio.id",
                   "Expected an assistant audio id reference");
  *id = audio->GetString("id");
  return {};
}

Status ChatAudioIdDelta(std::string_view id, std::string* out) {
  if (!AudioId(id)) return Status::Fail("Invalid Chat audio id");
  return Delta(Object({{"id", String(std::string(id))}}), out);
}
Status ChatAudioDataDelta(std::string_view bytes, std::string* out) {
  std::string encoded;
  if (bytes.empty() || bytes.size() > 65536 ||
      !io::EncodeBase64(bytes, 87384, &encoded))
    return Status::Fail("Invalid Chat audio data chunk");
  return Delta(Object({{"data", String(encoded)}}), out);
}
Status ChatAudioTranscriptDelta(std::string_view text, std::string* out) {
  if (text.empty() || text.size() > 1 << 20)
    return Status::Fail("Invalid Chat audio transcript chunk");
  return Delta(Object({{"transcript", String(std::string(text))}}), out);
}
Status ChatAudioExpiryDelta(int64_t expires_at, std::string* out) {
  if (expires_at <= 0 || expires_at > 9007199254740991LL)
    return Status::Fail("Invalid Chat audio expiry");
  return Delta(Object({{"expires_at", Number(expires_at)}}), out);
}
Status ChatAudioDataStream::Append(std::string_view bytes,
                                   std::vector<std::string>* deltas) {
  if (!deltas || finished_ || bytes.empty() || bytes.size() > 65536)
    return Status::Fail("Invalid audio data stream state");
  auto input = carry_ + std::string(bytes);
  const auto whole = input.size() / 3 * 3;
  std::vector<std::string> pending;
  for (size_t offset = 0; offset < whole;) {
    const auto count = std::min<size_t>(65535, whole - offset);
    std::string delta;
    auto status = ChatAudioDataDelta(
        std::string_view(input).substr(offset, count), &delta);
    if (!status) return status;
    pending.push_back(std::move(delta));
    offset += count;
  }
  carry_ = input.substr(whole);
  *deltas = std::move(pending);
  return {};
}
Status ChatAudioDataStream::Finish(std::vector<std::string>* deltas) {
  if (!deltas || finished_)
    return Status::Fail("Audio data stream already completed");
  std::vector<std::string> pending;
  if (!carry_.empty()) {
    std::string delta;
    auto status = ChatAudioDataDelta(carry_, &delta);
    if (!status) return status;
    pending.push_back(std::move(delta));
  }
  carry_.clear();
  finished_ = true;
  *deltas = std::move(pending);
  return {};
}
Status AttachChatAudio(const io::Json& message, const ChatAudioOptions& options,
                       std::string_view id, int64_t expires_at,
                       std::string_view bytes, std::string_view transcript,
                       size_t max_json_bytes, io::Json* out) {
  if (!out || !options.enabled || !message.IsObject() ||
      message.GetString("role") != "assistant" || !AudioId(id) ||
      expires_at <= 0 || expires_at > 9007199254740991LL || bytes.empty() ||
      transcript.empty() || transcript.size() > max_json_bytes ||
      (message.GetString("content") != transcript &&
       message.GetString("refusal") != transcript))
    return Status::Fail("Invalid complete Chat audio message");
  std::string encoded;
  if (!io::EncodeBase64(bytes, max_json_bytes, &encoded))
    return Status::Fail("Chat audio exceeds response budget");
  auto next = message;
  Set(&next, "audio",
      Object({{"id", String(std::string(id))},
              {"data", String(encoded)},
              {"expires_at", Number(expires_at)},
              {"transcript", String(std::string(transcript))}}));
  if (!options.text) Set(&next, "content", Json());
  if (OutputJson(next).size() > max_json_bytes)
    return Status::Fail("Chat audio exceeds response budget");
  *out = std::move(next);
  return {};
}

Status AddChatAudioUsage(const ActualUsage& synthesis, uint64_t audio_tokens,
                         io::Json* usage) {
  const auto integer = [](const Json& value, const char* key,
                          uint64_t* result) {
    const auto* field = value.Find(key);
    if (!field || !field->IsNumber() || !std::isfinite(field->number) ||
        field->number < 0 || field->number > double(kActualUsageLimit) ||
        field->number != double(static_cast<uint64_t>(field->number)))
      return false;
    *result = static_cast<uint64_t>(field->number);
    return true;
  };
  uint64_t input = 0, output = 0, total = 0;
  if (!usage || !usage->IsObject() || !ValidActualUsage(synthesis) ||
      !synthesis.input || !synthesis.output || !audio_tokens ||
      audio_tokens > synthesis.output || synthesis.cached ||
      synthesis.reasoning || synthesis.cache_write ||
      !integer(*usage, "prompt_tokens", &input) ||
      !integer(*usage, "completion_tokens", &output) ||
      !integer(*usage, "total_tokens", &total) ||
      input > kActualUsageLimit - output || input + output != total ||
      synthesis.input + synthesis.output > kActualUsageLimit - total)
    return Status::Fail("Invalid Chat audio usage");
  auto next = *usage;
  auto& details = Field(next, "completion_tokens_details");
  if (details.IsNull()) details = Object();
  uint64_t previous_audio = 0;
  if (!details.IsObject() ||
      (details.Has("audio_tokens") &&
       !integer(details, "audio_tokens", &previous_audio)) ||
      previous_audio > output)
    return Status::Fail("Invalid Chat audio token details");
  auto& extra = Field(next, "q4t_audio_synthesis");
  uint64_t prior_input = 0, prior_output = 0;
  if (!extra.IsNull() &&
      (!extra.IsObject() || !integer(extra, "input_tokens", &prior_input) ||
       !integer(extra, "output_tokens", &prior_output) || prior_input > input ||
       prior_output > output))
    return Status::Fail("Invalid Chat synthesis usage");
  extra = Object({{"input_tokens", Number(prior_input + synthesis.input)},
                  {"output_tokens", Number(prior_output + synthesis.output)}});
  // Adding a top-level field may invalidate references into the object.
  Set(&Field(next, "completion_tokens_details"), "audio_tokens",
      Number(previous_audio + audio_tokens));
  Set(&next, "prompt_tokens", Number(input + synthesis.input));
  Set(&next, "completion_tokens", Number(output + synthesis.output));
  Set(&next, "total_tokens",
      Number(total + synthesis.input + synthesis.output));
  *usage = std::move(next);
  return {};
}
}  // namespace q4t::server
