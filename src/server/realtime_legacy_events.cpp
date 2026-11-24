#include "q4t/server/realtime_legacy_events.h"

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_legacy_credentials.h"

namespace q4t::server {
namespace {
using namespace object_detail;
void Erase(Json* obj, std::string_view key) {
  std::erase_if(obj->object, [key](const auto& kv) { return kv.first == key; });
}
void ContentPart(Json* part, bool to_legacy) {
  const auto type = part->GetString("type");
  if (to_legacy && type == "output_text") Set(part, "type", String("text"));
  if (to_legacy && type == "output_audio") Set(part, "type", String("audio"));
  if (!to_legacy && type == "text") Set(part, "type", String("output_text"));
  if (!to_legacy && type == "audio") Set(part, "type", String("output_audio"));
}
void Item(Json* item, bool to_legacy) {
  if (item->GetString("role") != "assistant" || !item->Has("content")) return;
  for (auto& part : Field(*item, "content").array) ContentPart(&part, to_legacy);
}
ObjectReply Invalid(std::string param, std::string message) {
  auto error = Error(400, "invalid_request_error", std::move(message));
  error.value = Object({{"param", String(std::move(param))}});
  return error;
}
}  // namespace

ObjectReply DecodeLegacyRealtimeEvent(Json* event,
                                      std::optional<double>* temperature) {
  const auto type = event->GetString("type");
  if (type == "conversation.item.create" && event->Has("item")) {
    Item(&Field(*event, "item"), false);
    return {};
  }
  if (type != "response.create" && type != "session.update") return {};
  const bool session = type == "session.update";
  const std::string key = session ? "session" : "response";
  if (!event->Has(key)) return {};
  auto& options = Field(*event, key);
  if (!options.IsObject()) return Invalid(key, "Expected configuration object");
  Json extras = Object();
  if (!session) {
    std::string param;
    const auto valid = InputFields(options,
        {"conversation", "input", "instructions", "max_response_output_tokens",
         "metadata", "modalities", "output_audio_format", "temperature",
         "tool_choice", "tools", "voice"}, &param, "response");
    if (!valid) return Invalid(param, valid.message());
    for (const auto* name : {"conversation", "metadata", "input"}) {
      if (!options.Has(name)) continue;
      auto value = Field(options, name);
      if (std::string_view(name) == "input" && value.IsArray())
        for (auto& item : value.array) Item(&item, false);
      Set(&extras, name, std::move(value));
      Erase(&options, name);
    }
  }
  // A socket can change an established conversation but cannot mint a grant.
  if (options.Has("client_secret"))
    return Invalid(key + ".client_secret", "Client credentials are issued over HTTP");
  const bool override_temperature = options.Has("temperature");
  Json request;
  double sampling = 0;
  auto result = LegacyConversationCredential(options, &request, &sampling);
  if (!result.ok()) return result;
  options = Field(request, "session");
  if (!session) Erase(&options, "type");
  for (auto& [name, value] : extras.object) Set(&options, name, std::move(value));
  if (override_temperature) *temperature = sampling;
  return {};
}

bool EncodeLegacyRealtimeEvent(Json* event, double session_temperature) {
  const auto type = event->GetString("type");
  if (type == "conversation.item.done") return false;
  if (type == "session.created" || type == "session.updated") {
    auto& session = Field(*event, "session");
    session = LegacyConversationResource(session, nullptr, session_temperature);
  } else if (type == "conversation.item.added") {
    Set(event, "type", String("conversation.item.created"));
  } else {
    for (const auto& [modern, legacy] :
         {std::pair{"response.output_text.", "response.text."},
          std::pair{"response.output_audio.", "response.audio."},
          std::pair{"response.output_audio_transcript.", "response.audio_transcript."}}) {
      if (type.starts_with(modern)) {
        Set(event, "type", String(std::string(legacy) + type.substr(std::string_view(modern).size())));
        break;
      }
    }
  }
  if (event->Has("item")) Item(&Field(*event, "item"), true);
  if (event->Has("part")) ContentPart(&Field(*event, "part"), true);
  if (event->Has("response")) {
    auto& response = Field(*event, "response");
    if (response.Has("output_modalities")) {
      const auto& modes = Field(response, "output_modalities");
      const bool audio = modes.IsArray() && !modes.array.empty() && modes.array[0].str == "audio";
      Set(&response, "modalities", audio ? Array({String("text"), String("audio")}) : modes);
      Erase(&response, "output_modalities");
    }
    if (response.Has("output"))
      for (auto& item : Field(response, "output").array) Item(&item, true);
  }
  return true;
}
}  // namespace q4t::server
