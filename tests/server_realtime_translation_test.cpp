#include "q4t/server/realtime_translation_session.h"

#include <cmath>

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;

Json Parsed(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("fixture JSON");
  return value;
}
}  // namespace

Q4T_TEST(realtime_translation_settings_preserve_effective_audio_contract) {
  RealtimeTranslationSettings settings;
  auto create = Parsed(R"({"model":"gpt-realtime-translate","audio":{"input":{
    "transcription":{"model":"gpt-realtime-whisper"},
    "noise_reduction":{"type":"near_field","strength":0.4}},"output":{"language":"fr"}}})");
  Q4T_CHECK(settings.Apply(create, true).ok() && settings.language == "fr");
  Q4T_CHECK(
      settings
          .Apply(
              Parsed(
                  R"({"audio":{"input":{"noise_reduction":{"type":"far_field"}}}})"),
              false)
          .ok());
  Q4T_CHECK(settings.strength == .4 &&
            settings.noise == q4t::audio::NoiseReduction::kFarField);
  auto resource = settings.Resource("sess_cpu", 4600);
  ChatPlanError error;
  Q4T_CHECK(ValidateRealtimeWireShape(
      resource, RealtimeShape::kTranslationSession, &error));
  for (
      const auto* bad :
      {R"({"audio":{"input":{"noise_reduction":{"type":"near_field","strength":2}}}})",
       R"({"model":"unavailable"})",
       R"({"audio":{"output":{"language":"xx"}}})",
       R"({"audio":{"input":{"transcription":{"model":"missing"}}}})"}) {
    Q4T_CHECK(!settings.Apply(Parsed(bad), false).ok());
    Q4T_CHECK(OutputJson(resource) ==
              OutputJson(settings.Resource("sess_cpu", 4600)));
  }
  Q4T_CHECK(
      settings
          .Apply(
              Parsed(
                  R"({"audio":{"input":{"noise_reduction":null,"transcription":null}}})"),
              false)
          .ok());
  Q4T_CHECK(settings.strength == 1 && settings.transcription.empty() &&
            settings.noise == q4t::audio::NoiseReduction::kOff);
  return true;
}

Q4T_TEST(realtime_translation_flushes_before_close_and_preserves_update_order) {
  std::vector<std::string> events, languages;
  int acquired = 0, released = 0;
  RealtimeTranslationNative native;
  native.acquire = [&](const std::string& id) {
    ++acquired;
    return std::make_shared<RequestControl>(
        id, "", std::chrono::steady_clock::now() + std::chrono::seconds(10));
  };
  native.release = [&](const auto&) { ++released; };
  native.execute = [&](std::span<const float> pcm, const auto& settings,
                       const auto&, const auto& emit) -> ObjectReply {
    if (pcm.size() < 64) return Error(500, "fixture_short", "Missing audio");
    languages.push_back(settings.language);
    const bool ok =
        emit(Object({{"type", String("session.output_transcript.delta")},
                     {"delta", String("fixture")}}));
    return ok ? ObjectReply{} : Error(500, "fixture_sink", "Missing delivery");
  };
  const auto output = [&](Json event) {
    ChatPlanError error;
    if (!ValidateRealtimeWireShape(
            event, RealtimeShape::kTranslationServerEvent, &error))
      return false;
    events.push_back(event.GetString("type"));
    return true;
  };
  RealtimeTranslationSession session("sess_cpu", output, std::move(native),
                                     1 << 20, 10);
  const auto send = [&](const Json& event) {
    return session.Message(WebSocketOpcode::kText, OutputJson(event));
  };
  const auto append = [&](std::string_view bytes) {
    std::string encoded;
    return q4t::io::EncodeBase64(bytes, 1 << 20, &encoded) &&
           send(Object({{"type", String("session.input_audio_buffer.append")},
                        {"audio", String(std::move(encoded))}}));
  };
  std::string speech;
  for (size_t i = 0; i < 12000; ++i) {
    const auto sample = uint16_t(int16_t(5000 * std::sin(i * .1)));
    speech.push_back(char(sample & 255));
    speech.push_back(char(sample >> 8));
  }
  Q4T_CHECK(session.Start());
  Q4T_CHECK(append(std::string_view(speech).substr(0, 1)));
  Q4T_CHECK(append(std::string_view(speech).substr(1)));
  Q4T_CHECK(append(std::string(24000, char(0))));
  Q4T_CHECK(languages == std::vector<std::string>{"en"} &&
            !session.Completed());
  Q4T_CHECK(append(speech));
  Q4T_CHECK(send(Parsed(
      R"({"type":"session.update","session":{"audio":{"output":{"language":"fr"}}}})")));
  Q4T_CHECK(languages.size() == 2 && languages.back() == "en");
  Q4T_CHECK(events.back() == "session.updated");
  Q4T_CHECK(append(speech));
  Q4T_CHECK(send(Parsed(R"({"type":"session.close"})")));
  Q4T_CHECK(session.Completed() && events.back() == "session.closed");
  Q4T_CHECK(languages.size() == 3 && languages.back() == "fr");
  Q4T_CHECK(acquired == 3 && released == acquired);
  return true;
}
