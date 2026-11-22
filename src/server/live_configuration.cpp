#include "q4t/server/live_configuration.h"
#include "q4t/server/live_media_policy.h"
#include "q4t/server/live_responses_settings.h"

#include <algorithm>

#include "q4t/server/application_routes.h"
#include "q4t/server/input_contract.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr size_t kInstructionTokens = 16384, kHistoryTokens = 8192;

Status Invalid(ChatPlanError* error, std::string path, std::string message,
               std::string code = "invalid_request", int http = 400) {
  if (error) *error = {std::move(code), std::move(path), http};
  return Status::Fail(std::move(message));
}
bool Blank(std::string_view value) {
  return std::ranges::all_of(value, [](char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
  });
}
Status BackendFailure(ChatPlanError* error) {
  return Invalid(error, "session", "Native Live configuration check failed",
                 "live_processor_unavailable", 503);
}
Status Audio(Json* next, const LiveConfigurationContext& context,
             ChatPlanError* error) {
  if (!next->Has("audio")) {
    Set(next, "audio", Object());
  }
  auto* audio = &Field(*next, "audio");
  if (context.transport != LiveTransport::kWebSocket) {
    if (audio->Has("format"))
      return Invalid(error, "session.audio.format",
                     "This transport negotiates its media format");
  } else {
    if (!audio->Has("format"))
      Set(audio, "format",
          Object({{"type", String("audio/pcm")}, {"rate", Number(24000)}}));
    const auto* format = audio->Find("format");
    const auto* rate = format->Find("rate");
    const auto type = format->GetString("type");
    if (!rate || !IsInputInteger(*rate) ||
        (type == "audio/pcm" ? rate->number != 16000 && rate->number != 24000
                             : rate->number != 8000))
      return Invalid(
          error, "session.audio.format.rate",
          "Live PCM requires 16000/24000 Hz; G.711 requires 8000 Hz");
  }
  if (!audio->Has("output")) {
    Set(audio, "output", Object());
  }
  auto* output = &Field(*audio, "output");
  if (!output->Has("voice")) Set(output, "voice", context.default_voice);
  return {};
}
Status History(const Json& next, ChatPlanError* error) {
  const auto* input = next.GetArray("input");
  if (!input || input->array.size() > 128)
    return Invalid(error, "session.input", "Live accepts at most 128 messages");
  for (size_t i = 0; i < input->array.size(); ++i) {
    const auto& item = input->array[i];
    const auto path = "session.input[" + std::to_string(i) + "]";
    const auto* content = item.GetArray("content");
    if (!content || content->array.size() != 1)
      return Invalid(error, path + ".content", "Expected one text part");
    const auto& part = content->array[0];
    const auto type = part.GetString("type");
    const bool assistant = item.GetString("role") == "assistant";
    if (!type.empty() && (assistant ? type != "text" && type != "output_text"
                                    : type != "input_text"))
      return Invalid(error, path + ".content[0].type",
                     "Text part does not match its message role");
  }
  return {};
}
Status Delegation(Json* next, ChatPlanError* error) {
  if (!next->Has("delegation") || next->Find("delegation")->IsNull()) {
    Set(next, "delegation", Object({{"type", String("client")}}));
  }
  auto* selected = &Field(*next, "delegation");
  if (!selected->Has("acknowledgement_delay_ms"))
    Set(selected, "acknowledgement_delay_ms",
        Number(selected->GetString("type") == "decisions" ? 400 : 0));
  const auto* delay = selected->Find("acknowledgement_delay_ms");
  // A nonnegative signed 32-bit duration is representable by native timers;
  // the independent absolute session expiry still bounds the actual wait.
  if (!IsInputInteger(*delay) || delay->number < 0 ||
      delay->number > 2147483647)
    return Invalid(error, "session.delegation.acknowledgement_delay_ms",
                   "Expected an integer duration from 0 to 2147483647 ms");
  const auto* triggers = next->GetArray("triggers");
  bool input = false, output = false;
  for (const auto& trigger : triggers->array) {
    bool& found =
        trigger.GetString("event_type") == "session.input_transcript.delta"
            ? input
            : output;
    if (found)
      return Invalid(error, "session.triggers",
                     "At most one trigger per transcript event is allowed");
    found = true;
  }
  return {};
}
// Only these settings are sparse nested updates. Arrays, selectors and text
// format unions replace atomically; merging discriminator variants would
// retain fields from the old variant and silently corrupt the configuration.
void MergeBackend(Json* current, const Json& update) {
  for (const auto& [name, value] : update.object) {
    auto* previous = current->Has(name) ? &Field(*current, name) : nullptr;
    if ((name == "reasoning" || name == "text") && previous &&
        previous->IsObject() && value.IsObject()) {
      for (const auto& [key, child] : value.object) Set(previous, key, child);
    } else {
      Set(current, name, value);
    }
  }
}
}  // namespace

Status ResolveLiveConfiguration(const Json& input,
                                const LiveConfigurationContext& context,
                                Json* output, ChatPlanError* error) {
  if (!output || !error || !context.count_text || !context.count_history ||
      !context.validate || context.default_voice.IsNull())
    return BackendFailure(error);
  *error = {};
  auto status =
      ValidateLiveWireShape(input, LiveShape::kSessionStart, error, "session");
  if (!status) return status;
  if (const auto* client = input.Find("client")) {
    LiveMediaPolicy permissions;
    status = permissions.Configure(client, error);
    if (!status) return status;
  }
  Json next = input;
  if (!next.Has("input")) Set(&next, "input", Array());
  if (!next.Has("store")) Set(&next, "store", Boolean(false));
  if (!next.Has("triggers")) Set(&next, "triggers", Array());
  status = Audio(&next, context, error);
  if (status) status = History(next, error);
  if (status) status = Delegation(&next, error);
  if (!status) return status;
  const auto* instructions = next.Find("instructions");
  const bool defaults =
      !instructions || instructions->IsNull() || Blank(instructions->str);
  if (defaults)
    Set(&next, "instructions", String(context.default_instructions));
  status =
      ValidateLiveWireShape(next, LiveShape::kSessionStart, error, "session");
  if (!status) return status;
  try {
    size_t count = 0;
    status = context.count_text(next.GetString("instructions"), &count, error);
    if (!status) return status;
    if (count > kInstructionTokens)
      return Invalid(error, "session.instructions",
                     "Live instructions exceed 16384 tokens",
                     "context_length_exceeded");
    count = 0;
    status = context.count_history(*next.Find("input"), &count, error);
    if (!status) return status;
    if (count > kHistoryTokens)
      return Invalid(error, "session.input",
                     "Rendered Live startup history exceeds 8192 tokens",
                     "context_length_exceeded");
    status = context.validate(next, error);
    if (!status) return status;
  } catch (...) {
    return BackendFailure(error);
  }
  *output = std::move(next);
  *error = {};
  return {};
}

Status ResolveLiveForkConfiguration(const Json& state, const Json& overrides,
                                    const LiveConfigurationContext& context,
                                    Json* output, ChatPlanError* error) {
  if (!error || !output)
    return Status::Fail("Missing fork configuration destination");
  const auto* source = state.Find("configuration");
  if (!source || !source->IsObject() || !source->GetBool("store") ||
      state.GetString("format") != "q4t.live_fork.v1" ||
      !source->Find("audio") || !source->Find("audio")->IsObject() ||
      !ValidateLiveWireShape(*source, LiveShape::kSessionStart, error,
                             "session"))
    return Invalid(error, "session", "Invalid saved Live configuration",
                   "invalid_recording", 503);
  auto status = ValidateLiveWireShape(
      Object({{"type", String("session.start")}, {"session", overrides}}),
      LiveShape::kForkClient, error);
  if (!status) return status;
  Json next = *source;
  std::erase_if(Field(next, "audio").object,
                [](auto& field) { return field.first == "format"; });
  if (context.transport == LiveTransport::kWebSocket) {
    if (overrides.Has("client"))
      return Invalid(error, "session.client",
                     "Client permissions require WebRTC");
    std::erase_if(next.object,
                  [](auto& field) { return field.first == "client"; });
    if (const auto* selected = overrides.Find("audio"))
      if (const auto* format = selected->Find("format"))
        Set(&Field(next, "audio"), "format", *format);
  } else {
    if (overrides.Has("audio"))
      return Invalid(error, "session.audio",
                     "Media forks negotiate audio on the transport");
    if (const auto* client = overrides.Find("client")) {
      const auto* channel = client->Find("data_channel");
      if (!channel || !channel->IsObject())
        return Invalid(error, "session.client", "Invalid data channel");
      if (!next.Has("client"))
        Set(&next, "client", *client);
      else {
        auto& effective = Field(Field(next, "client"), "data_channel");
        for (const auto& [name, v] : channel->object) Set(&effective, name, v);
      }
    }
  }
  if (const auto* store = overrides.Find("store")) Set(&next, "store", *store);
  const auto* delegation = overrides.Find("delegation");
  const auto* responses = delegation ? delegation->Find("responses") : nullptr;
  if (state.GetBool("requires_tool_credentials") &&
      (!responses || !responses->Has("tools")))
    return Invalid(
        error, "session.delegation.responses.tools",
        "Supply current tool settings to replace private source credentials");
  if (delegation) {
    // Reuse sparse backend updates and immutable owner checks.
    return UpdateLiveConfiguration(next, Object({{"delegation", *delegation}}),
                                   context, output, error);
  }
  return ResolveLiveConfiguration(next, context, output, error);
}

Status UpdateLiveConfiguration(const Json& current, const Json& update,
                               const LiveConfigurationContext& context,
                               Json* output, ChatPlanError* error) {
  if (!output || !error) return BackendFailure(error);
  *error = {};
  auto status = ValidateLiveWireShape(current, LiveShape::kSessionStart, error,
                                      "session");
  if (status)
    status = ValidateLiveWireShape(update, LiveShape::kSessionUpdate, error,
                                   "session");
  if (!status) return status;
  auto next = current;
  if (!next.Has("delegation") || next.Find("delegation")->IsNull())
    Set(&next, "delegation", Object({{"type", String("client")}}));
  if (const auto* patch = update.Find("delegation")) {
    auto* selected = &Field(next, "delegation");
    const auto type = patch->IsNull() ? "client" : patch->GetString("type");
    if (selected->GetString("type") != type)
      return Invalid(error, "session.delegation.type",
                     "Delegation ownership cannot change after startup");
    if (!patch->IsNull()) {
      for (const auto& [name, value] : patch->object) {
        if (name == "responses" || name == "decisions") {
          auto* backend =
              selected->Has(name) ? &Field(*selected, name) : nullptr;
          if (backend && backend->IsObject())
            MergeBackend(backend, value);
          else
            Set(selected, name, value);
        } else {
          Set(selected, name, value);
        }
      }
    }
  }
  if (const auto* triggers = update.Find("triggers"))
    Set(&next, "triggers", *triggers);
  return ResolveLiveConfiguration(next, context, output, error);
}

Status LiveSessionResource(const Json& configuration, std::string_view id,
                           uint64_t expires_at, Json* output,
                           ChatPlanError* error) {
  if (!output || !error) return BackendFailure(error);
  if (!ValidApplicationId(id) || !expires_at ||
      expires_at > 9007199254740991ULL)
    return Invalid(error, "session", "Invalid native Live session identity",
                   "live_session_identity_invalid", 503);
  auto status = ValidateLiveWireShape(configuration, LiveShape::kSessionStart,
                                      error, "session");
  if (!status) return status;
  auto next = configuration;
  RedactLiveResponseCredentials(&next);
  Set(&next, "id", String(std::string(id)));
  Set(&next, "expires_at", Number(static_cast<int64_t>(expires_at)));
  Set(&next, "status", String("active"));
  status = ValidateLiveWireShape(next, LiveShape::kSession, error, "session");
  if (!status) return status;
  *output = std::move(next);
  *error = {};
  return {};
}
}  // namespace q4t::server
