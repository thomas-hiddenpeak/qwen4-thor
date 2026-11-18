#include "q4t/server/chat_audio_history.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/chat_audio_output.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool ValidTranscript(std::string_view text) {
  return !text.empty() && text.size() <= (1U << 20) &&
         text.find('\0') == text.npos && detail::NormalizeUtf8(text) == text;
}
ObjectReply Invalid(const std::string& param, const std::string& message) {
  auto reply = Error(400, "invalid_request", message);
  reply.value = Object({{"param", String(param)}});
  return reply;
}
}  // namespace

bool HasChatAudioHistory(const io::Json& request) {
  const auto* messages = request.GetArray("messages");
  if (!messages) return false;
  return std::any_of(messages->array.begin(), messages->array.end(),
                     [](const auto& message) {
                       const auto* audio = message.Find("audio");
                       return audio && !audio->IsNull();
                     });
}

ObjectReply PublishChatAudioHistory(ObjectStore& objects,
                                    const BinaryAccess& access,
                                    const std::string& id, int64_t created_at,
                                    std::string_view transcript,
                                    SpeechFormat format,
                                    const SpeechEncodingReceipt& receipt) {
  std::string checked;
  const auto reference = Object(
      {{"role", String("assistant")}, {"audio", Object({{"id", String(id)}})}});
  if (!ChatAudioHistoryId(reference, &checked, nullptr) ||
      !ValidTranscript(transcript) || created_at <= 0 ||
      created_at > 9007199254740991LL - kChatAudioLifetimeSeconds ||
      !receipt.bytes || !receipt.input_samples || !receipt.output_samples ||
      receipt.sha256.size() != 64 ||
      !std::all_of(receipt.sha256.begin(), receipt.sha256.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
      }))
    return Error(500, "chat_audio_receipt_invalid",
                 "Complete synthesized audio receipt required");
  const auto expires_at = created_at + kChatAudioLifetimeSeconds;
  ObjectRequest write;
  write.operation = ObjectOperation::kPut;
  write.kind = ObjectKind::kAudio;
  write.scope = access.scope;
  write.id = id;
  write.created_at = created_at;
  write.ttl_seconds = kChatAudioLifetimeSeconds;
  write.expires_at = expires_at;
  write.mutate_before = expires_at;
  write.data =
      Object({{"format", String("q4t.chat_audio.v1")},
              {"id", String(id)},
              {"expires_at", Number(expires_at)},
              {"transcript", String(std::string(transcript))},
              {"encoding", String(std::string(ChatAudioFormatName(format)))},
              {"bytes", Number(receipt.bytes)},
              {"sha256", String(receipt.sha256)},
              {"input_samples", Number(receipt.input_samples)},
              {"output_samples", Number(receipt.output_samples)}});
  return objects.Execute(write, access.deadline, access.cancelled);
}

ObjectReply ResolveChatAudioHistory(ObjectStore* objects,
                                    const BinaryAccess& access,
                                    const io::Json& request, io::Json* out) {
  if (!out || !request.IsObject())
    return Error(500, "chat_audio_history_invalid", "Missing history owner");
  auto next = request;
  if (HasChatAudioHistory(request)) {
    if (!objects)
      return Error(503, "object_store_unavailable",
                   "Audio history storage is not configured");
    size_t total_bytes = 0;
    auto& messages = Field(next, "messages").array;
    for (size_t index = 0; index < messages.size(); ++index) {
      auto& message = messages[index];
      const auto param = "messages[" + std::to_string(index) + "].audio";
      std::string id;
      ChatPlanError error;
      const auto parsed = ChatAudioHistoryId(message, &id, &error);
      if (!parsed) return Invalid(param + ".id", parsed.message());
      if (id.empty()) continue;
      ObjectRequest read;
      read.kind = ObjectKind::kAudio;
      read.scope = access.scope;
      read.id = id;
      auto reply = objects->Execute(read, access.deadline, access.cancelled);
      if (!reply.ok()) {
        if (reply.http_status == 404)
          reply = Error(404, "audio_not_found",
                        "Audio reference is missing or expired");
        reply.value = Object({{"param", String(param + ".id")}});
        return reply;
      }
      const auto* data = reply.value.Find("data");
      if (!data || data->GetString("format") != "q4t.chat_audio.v1" ||
          data->GetString("id") != id ||
          data->GetInt("expires_at") != reply.value.GetInt("expires_at") ||
          !ValidTranscript(data->GetString("transcript")))
        return Error(503, "chat_audio_history_corrupt",
                     "Stored audio reference is invalid");
      if (data->GetInt("expires_at") <= std::time(nullptr))
        return Invalid(param + ".id", "Audio reference has expired");
      const auto transcript = data->GetString("transcript");
      if (transcript.size() > (1U << 20) - total_bytes)
        return Invalid(param, "Audio history exceeds text capacity");
      total_bytes += transcript.size();
      auto& content = Field(message, "content");
      // Equal text/audio is one utterance. Additional caller-supplied text is
      // preserved in order, never silently overwritten by the reference.
      if (content.IsNull()) {
        if (message.GetString("refusal") != transcript)
          content = String(transcript);
      } else if (content.IsString()) {
        if (content.str != transcript) content.str += "\n" + transcript;
      } else if (content.IsArray()) {
        content.array.push_back(
            Object({{"type", String("text")}, {"text", String(transcript)}}));
      } else {
        return Invalid("messages[" + std::to_string(index) + "].content",
                       "Invalid assistant content");
      }
      std::erase_if(message.object,
                    [](const auto& field) { return field.first == "audio"; });
    }
  }
  *out = std::move(next);
  return {};
}
}  // namespace q4t::server
