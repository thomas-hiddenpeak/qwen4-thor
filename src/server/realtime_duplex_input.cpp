#include "q4t/server/realtime_duplex_input.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_asr_policy.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string param, std::string message) {
  auto reply = Error(400, "invalid_request_error", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Fields(const Json& value,
                   std::initializer_list<std::string_view> fields,
                   std::string_view path) {
  std::string param;
  const auto status = InputFields(value, fields, &param, path);
  if (!status) return Invalid(param, status.message());
  for (size_t i = 0; i < value.object.size(); ++i)
    for (size_t j = 0; j < i; ++j)
      if (value.object[i].first == value.object[j].first)
        return Invalid(InputParamPath(path, value.object[i].first),
                       "Duplicate input setting");
  return {};
}
}  // namespace

ObjectReply PlanRealtimeDuplexInput(const Json& session,
                                    RealtimeDuplexInput* output) {
  if (!output)
    return Error(500, "realtime_input_owner_invalid", "Missing input owner");
  if (!session.IsObject() || session.GetString("type") != "realtime")
    return Invalid("session.type", "Expected an effective realtime session");
  RealtimeDuplexInput next;
  // The conversation API's VAD default is 500ms. Do not change the existing
  // transcription-only endpoint's independent 200ms default.
  next.processing.request.chunking.silence_duration_ms = 500;
  auto native = Object({{"type", String("transcription")}});
  if (const auto* include = session.Find("include"))
    Set(&native, "include", *include);
  if (const auto* audio = session.Find("audio")) {
    auto reply = Fields(*audio, {"input", "output"}, "session.audio");
    if (!reply.ok()) return reply;
    if (const auto* input = audio->Find("input")) {
      reply = Fields(
          *input,
          {"format", "noise_reduction", "transcription", "turn_detection"},
          "session.audio.input");
      if (!reply.ok()) return reply;
      next.enabled = true;
      auto processing = *input;
      if (const auto* transcription = input->Find("transcription")) {
        next.transcript_events = !transcription->IsNull();
        if (transcription->IsNull())
          std::erase_if(processing.object, [](const auto& field) {
            return field.first == "transcription";
          });
      }
      if (const auto* turn = input->Find("turn_detection");
          turn && !turn->IsNull()) {
        constexpr auto path = "session.audio.input.turn_detection";
        reply = Fields(*turn,
                       {"type", "threshold", "prefix_padding_ms",
                        "silence_duration_ms", "eagerness", "create_response",
                        "interrupt_response", "idle_timeout_ms"},
                       path);
        if (!reply.ok()) return reply;
        for (const auto name : {"create_response", "interrupt_response"}) {
          if (const auto* value = turn->Find(name)) {
            if (!value->IsBool())
              return Invalid(InputParamPath(path, name),
                             "Expected a boolean turn control");
            (std::string_view(name) == "create_response"
                 ? next.create_response
                 : next.interrupt_response) = value->boolean;
          }
        }
        if (const auto* timeout = turn->Find("idle_timeout_ms")) {
          if (turn->GetString("type") != "server_vad")
            return Invalid(InputParamPath(path, "idle_timeout_ms"),
                           "Idle timeout requires server VAD");
          if (!timeout->IsNull()) {
            if (!IsInputInteger(*timeout) || timeout->number < 5000 ||
                timeout->number > 30000)
              return Invalid(InputParamPath(path, "idle_timeout_ms"),
                             "Idle timeout requires 5000 to 30000 ms");
            next.idle_timeout_ms = static_cast<uint32_t>(timeout->number);
          }
        }
        std::erase_if(Field(processing, "turn_detection").object,
                      [](const auto& field) {
                        return field.first == "create_response" ||
                               field.first == "interrupt_response" ||
                               field.first == "idle_timeout_ms";
                      });
      }
      Set(&native, "audio", Object({{"input", std::move(processing)}}));
    }
  }
  auto reply = UpdateRealtimeTranscriptionSettings(
      native, false, next.processing, &next.processing);
  if (!reply.ok()) return reply;
  if (!next.processing.delay.empty() &&
      (!RealtimeAsrDelayPolicy(next.processing.delay).Valid() ||
       next.processing.turn != RealtimeTranscriptionSettings::Turn::kManual))
    return Invalid("session.audio.input.transcription.delay",
                   "Continuous transcription requires manual audio commits");
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server
