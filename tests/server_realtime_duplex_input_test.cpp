#include "q4t/server/realtime_duplex_input.h"

#include <stdexcept>

#include "q4t/test.h"
#include "../src/server/object_protocol.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Turn = RealtimeTranscriptionSettings::Turn;
Json Parse(std::string text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("duplex fixture JSON");
  return value;
}
Json Session(std::string input) {
  return Parse("{\"type\":\"realtime\",\"audio\":{\"input\":" + input + "}}");
}
}  // namespace

Q4T_TEST(realtime_duplex_text_session_does_not_acquire_audio_owner) {
  RealtimeDuplexInput out;
  Q4T_CHECK(
      PlanRealtimeDuplexInput(Parse(R"({"type":"realtime","audio":{"output":{
                  "voice":"vivian","format":{"type":"audio/pcm"}}}})"),
                              &out)
          .ok());
  Q4T_CHECK(!out.enabled && !out.transcript_events);
  Q4T_CHECK(!out.RespondOnCommit(true) && !out.InterruptOnSpeechStart());
  Q4T_CHECK(PlanRealtimeDuplexInput(Session("{}"), &out).ok());
  Q4T_CHECK(out.enabled && !out.transcript_events);
  Q4T_CHECK(out.processing.request.model == "qwen3-asr-0.6b");
  Q4T_CHECK(!out.RespondOnCommit(true) && !out.RespondOnCommit(false));
  return true;
}

Q4T_TEST(realtime_duplex_manual_commit_never_automatically_responds) {
  for (const auto turn :
       {"null", R"({"type":"server_vad"})", R"({"type":"semantic_vad"})"}) {
    RealtimeDuplexInput out;
    Q4T_CHECK(
        PlanRealtimeDuplexInput(
            Session(std::string("{\"turn_detection\":") + turn + '}'), &out)
            .ok());
    Q4T_CHECK(!out.RespondOnCommit(false));
    Q4T_CHECK(out.RespondOnCommit(true) ==
              (out.processing.turn != Turn::kManual));
  }
  return true;
}

Q4T_TEST(realtime_duplex_vad_response_and_interruption_are_independent) {
  for (const auto type : {"server_vad", "semantic_vad"}) {
    for (const bool create : {false, true}) {
      for (const bool interrupt : {false, true}) {
        auto input =
            Object({{"turn_detection",
                     Object({{"type", String(type)},
                             {"create_response", Boolean(create)},
                             {"interrupt_response", Boolean(interrupt)}})}});
        RealtimeDuplexInput out;
        Q4T_CHECK(
            PlanRealtimeDuplexInput(Session(OutputJson(input)), &out).ok());
        Q4T_CHECK(out.RespondOnCommit(true) == create);
        Q4T_CHECK(out.InterruptOnSpeechStart() == interrupt);
        Q4T_CHECK(!out.RespondOnCommit(false));
      }
    }
  }
  return true;
}

Q4T_TEST(realtime_duplex_public_transcript_off_keeps_native_audio_processing) {
  RealtimeDuplexInput out;
  Q4T_CHECK(PlanRealtimeDuplexInput(Session(R"({"format":{"type":"audio/pcma"},
    "transcription":{"model":"qwen3-asr-0.6b","language":"zh",
      "prompt":"术语","keywords":["Thor"]}})"),
                                    &out)
                .ok());
  Q4T_CHECK(out.transcript_events && out.processing.request.language == "zh");
  Q4T_CHECK(out.processing.format.rate == 8000);
  Q4T_CHECK(PlanRealtimeDuplexInput(Session(R"({"format":{"type":"audio/pcma"},
    "transcription":null})"),
                                    &out)
                .ok());
  Q4T_CHECK(out.enabled && !out.transcript_events);
  Q4T_CHECK(out.processing.format.encoding == RealtimeAudioEncoding::kALaw);
  Q4T_CHECK(out.processing.request.model == "qwen3-asr-0.6b");
  Q4T_CHECK(out.processing.request.prompt.empty() &&
            out.processing.request.language.empty() &&
            out.processing.request.keywords.empty());
  return true;
}

Q4T_TEST(
    realtime_duplex_uses_real_processing_contract_and_transcript_includes) {
  auto session = Session(R"({"format":{"type":"audio/pcm","rate":24000},
    "noise_reduction":{"type":"far_field"},"transcription":{
      "language":"en","languages":["en","zh"],"prompt":"Names"},
    "turn_detection":{"type":"server_vad","threshold":0.4,
      "prefix_padding_ms":150,"silence_duration_ms":450,
      "create_response":false}})");
  Set(&session, "include",
      Array({String("item.input_audio_transcription.logprobs")}));
  RealtimeDuplexInput out;
  Q4T_CHECK(PlanRealtimeDuplexInput(session, &out).ok());
  Q4T_CHECK(out.processing.request.logprobs && out.transcript_events);
  Q4T_CHECK(out.processing.noise ==
            RealtimeTranscriptionSettings::Noise::kFarField);
  Q4T_CHECK(out.processing.request.chunking.threshold == .4 &&
            out.processing.request.chunking.prefix_padding_ms == 150 &&
            out.processing.request.chunking.silence_duration_ms == 450);
  Q4T_CHECK(!out.RespondOnCommit(true) && out.InterruptOnSpeechStart());
  Q4T_CHECK(out.processing.request.languages ==
            std::vector<std::string>({"en", "zh"}));
  Q4T_CHECK(PlanRealtimeDuplexInput(
                Session(R"({"turn_detection":{"type":"server_vad"}})"), &out)
                .ok());
  Q4T_CHECK(out.processing.request.chunking.silence_duration_ms == 500);
  Q4T_CHECK(
      RealtimeTranscriptionSettings{}.request.chunking.silence_duration_ms ==
      200);
  return true;
}

Q4T_TEST(realtime_duplex_idle_timeout_is_server_vad_only_and_bounded) {
  for (const auto timeout : {"5000", "6000", "30000", "null"}) {
    RealtimeDuplexInput out;
    Q4T_CHECK(
        PlanRealtimeDuplexInput(
            Session(
                std::string(
                    R"({"turn_detection":{"type":"server_vad","idle_timeout_ms":)") +
                timeout + "}}"),
            &out)
            .ok());
    Q4T_CHECK(out.idle_timeout_ms.has_value() ==
              (std::string_view(timeout) != "null"));
  }
  RealtimeDuplexInput out;
  for (const auto timeout : {"0", "4999", "30001", "5000.1", "\"6000\"", "true",
                             "5000.0000000000001"}) {
    Q4T_CHECK(
        !PlanRealtimeDuplexInput(
             Session(
                 std::string(
                     R"({"turn_detection":{"type":"server_vad","idle_timeout_ms":)") +
                 timeout + "}}"),
             &out)
             .ok());
  }
  Q4T_CHECK(!PlanRealtimeDuplexInput(Session(R"({"turn_detection":{
    "type":"semantic_vad","idle_timeout_ms":null}})"),
                                     &out)
                 .ok());
  return true;
}

Q4T_TEST(realtime_duplex_failed_settings_preserve_all_accepted_fields) {
  RealtimeDuplexInput out;
  Q4T_CHECK(PlanRealtimeDuplexInput(Session(R"({"format":{"type":"audio/pcmu"},
    "transcription":{"language":"zh","prompt":"kept"},
    "turn_detection":{"type":"server_vad","create_response":false,
      "interrupt_response":false,"idle_timeout_ms":9000}})"),
                                    &out)
                .ok());
  const auto before =
      OutputJson(RealtimeTranscriptionSettingsJson(out.processing, false));
  for (
      const auto input :
      {"null", "true", R"({"unknown":1})",
       R"({"turn_detection":{"type":"fake"}})",
       R"({"turn_detection":{"type":"server_vad","create_response":null}})",
       R"({"turn_detection":{"type":"server_vad","interrupt_response":1}})",
       R"({"turn_detection":{"type":"server_vad","create_response":true,"create_response":false}})",
       R"({"turn_detection":{"type":"server_vad","eagerness":"high"}})",
       R"({"turn_detection":{"type":"semantic_vad","threshold":0.3}})",
       R"({"turn_detection":{"type":"server_vad","typo":true}})",
       R"({"format":{"type":"audio/pcm","rate":16000}})",
       R"({"transcription":{"language":"en","languages":["zh"]}})",
       R"({"transcription":false})",
       R"({"noise_reduction":{"type":"fake"}})"}) {
    const auto reply = PlanRealtimeDuplexInput(Session(input), &out);
    Q4T_CHECK(!reply.ok() && reply.http_status == 400 &&
              reply.value.Has("param"));
    Q4T_CHECK(out.enabled && out.transcript_events && !out.create_response &&
              !out.interrupt_response && out.idle_timeout_ms == 9000);
    Q4T_CHECK(OutputJson(RealtimeTranscriptionSettingsJson(out.processing,
                                                           false)) == before);
  }
  return true;
}

Q4T_TEST(realtime_duplex_continuous_asr_needs_manual_commit) {
  RealtimeDuplexInput out;
  Q4T_CHECK(PlanRealtimeDuplexInput(Session(R"({"transcription":{"delay":"low"},
    "turn_detection":null})"),
                                    &out)
                .ok());
  for (const auto type : {"server_vad", "semantic_vad"}) {
    const auto input =
        Object({{"transcription", Object({{"delay", String("low")}})},
                {"turn_detection", Object({{"type", String(type)}})}});
    const auto reply =
        PlanRealtimeDuplexInput(Session(OutputJson(input)), &out);
    Q4T_CHECK(!reply.ok() && reply.value.GetString("param") ==
                                 "session.audio.input.transcription.delay");
    Q4T_CHECK(out.processing.turn == Turn::kManual);
  }
  return true;
}

Q4T_TEST(realtime_duplex_empty_include_clears_prior_effective_request) {
  RealtimeDuplexInput out;
  auto value = Session(R"({"transcription":{}})");
  Set(&value, "include",
      Array({String("item.input_audio_transcription.logprobs")}));
  Q4T_CHECK(PlanRealtimeDuplexInput(value, &out).ok() &&
            out.processing.request.logprobs);
  Set(&value, "include", Array());
  Q4T_CHECK(PlanRealtimeDuplexInput(value, &out).ok() &&
            !out.processing.request.logprobs);
  for (
      const auto bad :
      {"null", "true", R"(["anything"])",
       R"(["item.input_audio_transcription.logprobs","item.input_audio_transcription.logprobs"])"}) {
    Set(&value, "include", Parse(bad));
    Q4T_CHECK(!PlanRealtimeDuplexInput(value, &out).ok());
    Q4T_CHECK(!out.processing.request.logprobs);
  }
  return true;
}

Q4T_TEST(realtime_duplex_rejects_invalid_owner_and_session_envelope) {
  RealtimeDuplexInput out;
  for (const auto text : {"null", "{}", R"({"type":"transcription"})",
                          R"({"type":"realtime","audio":null})",
                          R"({"type":"realtime","audio":{"unknown":{}}})"})
    Q4T_CHECK(!PlanRealtimeDuplexInput(Parse(text), &out).ok());
  const auto reply = PlanRealtimeDuplexInput(Session("{}"), nullptr);
  Q4T_CHECK(reply.http_status == 500 &&
            reply.code == "realtime_input_owner_invalid");
  return true;
}
