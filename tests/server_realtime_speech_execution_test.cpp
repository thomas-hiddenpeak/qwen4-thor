#include "q4t/server/realtime_speech_execution.h"

#include <cmath>

#include "../src/server/object_protocol.h"
#include "q4t/server/native_chat_generation.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
Json Parse(const std::string& raw) {
  Json value;
  if (!q4t::io::ParseJson(raw, &value, true))
    throw std::runtime_error("speech execution CPU fixture JSON");
  return value;
}
RealtimeSpeechOptions Options(std::string format = "audio/pcm") {
  RealtimeSpeechOptions options;
  ChatPlanError error;
  const auto settings = Object(
      {{"output_modalities", Array({String("audio")})},
       {"audio",
        Object({{"output",
                 Object({{"format", Object({{"type", String(format)}})}})}})}});
  if (!PlanRealtimeSpeech(settings, Object(), &options, &error))
    throw std::runtime_error("speech execution CPU options");
  return options;
}
SpeechEncodingReceipt Receipt(std::string_view pcm) {
  SpeechEncodingReceipt receipt;
  receipt.input_samples = receipt.output_samples = pcm.size() / 2;
  receipt.bytes = pcm.size();
  receipt.sha256 = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(pcm.data()), pcm.size()}));
  return receipt;
}
}  // namespace

Q4T_TEST(realtime_speech_options_keep_format_voice_and_session_speed) {
  const auto settings = Parse(
      R"({"output_modalities":["audio"],"audio":{"output":{"format":{"type":"audio/pcmu"},"voice":"serena","speed":0.5}}})");
  RealtimeSpeechOptions options;
  ChatPlanError error;
  Q4T_CHECK(PlanRealtimeSpeech(
      settings,
      Parse(R"({"audio":{"output":{"format":{"type":"audio/pcma"}}}})"),
      &options, &error));
  Q4T_CHECK(options.enabled && options.chat.enabled && options.chat.text &&
            options.format ==
                RealtimeAudioFormat(RealtimeAudioEncoding::kALaw, 8000));
  Q4T_CHECK(options.chat.speech.format == SpeechFormat::kPcm &&
            options.chat.speech.voice == "serena" &&
            options.chat.speech.speed == 0.5 &&
            options.chat.speech.input.empty());
  const auto original = options;
  Q4T_CHECK(PlanRealtimeSpeech(
      settings, Parse(R"({"audio":{"output":{"voice":{"id":"voice_cpu"}}}})"),
      &options, &error));
  Q4T_CHECK(options.chat.speech.voice_id &&
            options.chat.speech.voice == "voice_cpu" &&
            !SameRealtimeVoice(options, original));
  Q4T_CHECK(PlanRealtimeSpeech(
      settings, Parse(R"({"output_modalities":["text"]})"), &options, &error));
  Q4T_CHECK(!options.enabled && !options.chat.enabled &&
            SameRealtimeVoice(options, original));
  return true;
}

Q4T_TEST(realtime_speech_options_reject_invalid_values_without_substitution) {
  for (const auto* raw :
       {R"({"speed":0.24})", R"({"speed":1.51})", R"({"speed":"1"})",
        R"({"voice":"alloy"})", R"({"voice":{"id":""}})",
        R"({"format":{"type":"audio/pcm","rate":16000}})",
        R"({"format":{"type":"audio/pcmu","rate":8000}})",
        R"({"format":{"type":42}})", R"({"format":{"type":"wav"}})",
        R"({"format":{"extra":1}})", R"({"extra":1})"}) {
    auto settings = Object({{"audio", Object({{"output", Parse(raw)}})}});
    auto options = Options();
    ChatPlanError error;
    Q4T_CHECK(!PlanRealtimeSpeech(settings, Object(), &options, &error));
    Q4T_CHECK(options.enabled && options.chat.speech.voice == "vivian");
  }
  auto options = Options();
  ChatPlanError error;
  Q4T_CHECK(!PlanRealtimeSpeech(Object(),
                                Parse(R"({"audio":{"output":{"speed":1.2}}})"),
                                &options, &error));
  return true;
}

Q4T_TEST(realtime_speech_delivery_matches_actual_pcm_and_typed_eof) {
  std::string actual;
  std::vector<RealtimeSpeechEvent> terminals;
  RealtimeSpeechExecution delivery(
      Options(),
      [&](const auto& event) {
        actual += event.bytes;
        if (event.kind == RealtimeSpeechEvent::Kind::kDone)
          terminals.push_back(event);
        return ObjectReply{};
      },
      {});
  std::string pcm;
  for (size_t i = 0; i < 2400; ++i) pcm += char(i & 255);
  Q4T_CHECK(delivery.Valid() && delivery.Append(pcm.substr(0, 3)).ok() &&
            delivery.Append(pcm.substr(3)).ok());
  Q4T_CHECK(delivery.Finish("actual transcript", Receipt(pcm), {7, 33}).ok());
  Q4T_CHECK(actual == pcm && delivery.Completed() && terminals.size() == 1 &&
            delivery.Usage()->input == 7 && delivery.Usage()->output == 33);
  const auto& done = terminals[0];
  Q4T_CHECK(done.bytes.empty() && done.transcript == "actual transcript" &&
            done.receipt.input_samples == 1200 &&
            done.receipt.output_samples == 1200 && done.receipt.bytes == 2400);
  Q4T_CHECK(!delivery.Append(pcm).ok() &&
            !delivery.Finish("actual transcript", Receipt(pcm), {7, 33}).ok());
  Q4T_CHECK(terminals.size() == 1 && actual == pcm);
  return true;
}

Q4T_TEST(realtime_speech_delivery_g711_keeps_one_continuous_sample_clock) {
  for (const auto& [format, expected] :
       std::vector<std::pair<std::string, uint8_t>>{{"audio/pcmu", 255},
                                                    {"audio/pcma", 213}}) {
    std::string wire;
    RealtimeSpeechReceipt done;
    RealtimeSpeechExecution delivery(
        Options(format),
        [&](const auto& event) {
          wire += event.bytes;
          if (event.kind == RealtimeSpeechEvent::Kind::kDone)
            done = event.receipt;
          return ObjectReply{};
        },
        {});
    const std::string pcm(2002, '\0');
    Q4T_CHECK(delivery.Append(pcm.substr(0, 1)).ok() &&
              delivery.Append(pcm.substr(1, 1000)).ok() &&
              delivery.Append(pcm.substr(1001)).ok() &&
              delivery.Finish("silence fixture", Receipt(pcm), {5, 17}).ok());
    Q4T_CHECK(wire == std::string(333, char(expected)) &&
              done.input_samples == 1001 && done.output_samples == 333 &&
              done.bytes == 333);
  }
  return true;
}

Q4T_TEST(realtime_speech_delivery_rejects_forged_receipts_without_done) {
  const std::string pcm(480, '\1');
  for (int fault = 0; fault < 5; ++fault) {
    size_t completed = 0;
    RealtimeSpeechExecution delivery(
        Options(),
        [&](const auto& event) {
          completed += event.kind == RealtimeSpeechEvent::Kind::kDone;
          return ObjectReply{};
        },
        {});
    auto receipt = Receipt(pcm);
    ActualUsage usage{5, 17};
    if (fault == 0) receipt.sha256[0] ^= 1;
    if (fault == 1) ++receipt.output_samples;
    if (fault == 2) ++receipt.bytes;
    if (fault == 3) receipt.input_samples = 0;
    if (fault == 4) usage.output = 0;
    Q4T_CHECK(delivery.Append(pcm).ok());
    Q4T_CHECK(!delivery.Finish("fixture", receipt, usage).ok() &&
              !delivery.Completed() && !delivery.Usage());
    Q4T_CHECK(!delivery.Finish("fixture", Receipt(pcm), {5, 17}).ok() &&
              !delivery.Append(pcm).ok() && completed == 0);
  }
  return true;
}

Q4T_TEST(realtime_speech_delivery_unknown_receipts_and_cancel_never_replay) {
  const std::string pcm(480, '\0');
  for (bool lose_done : {false, true}) {
    size_t calls = 0;
    RealtimeSpeechExecution delivery(
        Options(),
        [&](const auto& event) {
          ++calls;
          if (lose_done && event.kind == RealtimeSpeechEvent::Kind::kChunk)
            return ObjectReply{};
          auto reply = Error(503, "cpu_unknown_audio_delivery", "CPU fixture");
          reply.commit_unknown = true;
          return reply;
        },
        {});
    auto reply = delivery.Append(pcm);
    if (lose_done) reply = delivery.Finish("fixture", Receipt(pcm), {5, 17});
    Q4T_CHECK(reply.commit_unknown && !delivery.Completed());
    const auto observed = calls;
    Q4T_CHECK(delivery.Append(pcm).commit_unknown &&
              delivery.Finish("fixture", Receipt(pcm), {5, 17}).commit_unknown);
    Q4T_CHECK(calls == observed && !delivery.Usage());
  }
  bool cancelled = false;
  size_t done = 0;
  RealtimeSpeechExecution delivery(
      Options(),
      [&](const auto& event) {
        done += event.kind == RealtimeSpeechEvent::Kind::kDone;
        return ObjectReply{};
      },
      [&] { return cancelled; });
  Q4T_CHECK(delivery.Append(pcm).ok());
  cancelled = true;
  Q4T_CHECK(!delivery.Finish("fixture", Receipt(pcm), {5, 17}).ok() && !done);
  return true;
}

Q4T_TEST(realtime_speech_delivery_checks_post_tempo_native_pcm_receipt) {
  for (double speed : {0.5, 1.5}) {
    auto options = Options();
    options.chat.speech.speed = speed;
    RealtimeSpeechReceipt completed;
    RealtimeSpeechExecution delivery(
        options,
        [&](const auto& event) {
          if (event.kind == RealtimeSpeechEvent::Kind::kDone)
            completed = event.receipt;
          return ObjectReply{};
        },
        {});
    // The source is explicit CPU waveform data, not a claim of model speech.
    bool sent = false;
    SpeechPcmSource source;
    source.next = [&](std::vector<float>* pcm, bool* eof) {
      pcm->clear();
      if (!sent) {
        for (int i = 0; i < 2400; ++i)
          pcm->push_back(0.1f * std::sin(float(i) * 0.03f));
        sent = true;
      }
      *eof = true;
      return ObjectReply{};
    };
    source.final_samples = [] { return std::optional<size_t>(2400); };
    SpeechEncodingReceipt native;
    const auto reply = WriteSpeechAudio(
        options.chat.speech, 2400, {},
        {"client:cpu", std::chrono::steady_clock::now() + 5s, {}}, source,
        [&](std::string_view bytes) { return delivery.Append(bytes); },
        &native);
    Q4T_CHECK(reply.ok() && native.input_samples == 2400 &&
              native.output_samples != native.input_samples);
    Q4T_CHECK(delivery.Finish("fixture", native, {6, 17}).ok());
    Q4T_CHECK(completed.input_samples == native.output_samples &&
              completed.output_samples == native.output_samples &&
              completed.bytes == native.bytes);
  }
  return true;
}

Q4T_TEST(realtime_native_speech_requires_prepared_pcm_and_borrowed_owner) {
  const auto request = Parse(R"({"messages":[]})");
  ChatRequestPlan plan;
  NativeChatSpeech speech{
      [](auto) { return ObjectReply{}; },
      [](auto, const auto&, const auto&) { return ObjectReply{}; }};
  ActualUsageCounter usage;
  usage.Enable();
  speech.usage = &usage;
  auto sink = [](const ResponseDelta&) { return ObjectReply{}; };
  Q4T_CHECK(
      !NativeChatGeneration(request, sink, 1024, {nullptr, nullptr, &speech})
           .Valid());
  plan.audio_output = Options().chat;
  Q4T_CHECK(NativeChatGeneration(request, sink, 1024, {&plan, nullptr, &speech})
                .Valid());
  Q4T_CHECK(
      !NativeChatGeneration(request, sink, 1024, {&plan, nullptr}).Valid());
  plan.audio_output.speech.format = SpeechFormat::kMp3;
  Q4T_CHECK(
      !NativeChatGeneration(request, sink, 1024, {&plan, nullptr, &speech})
           .Valid());
  plan.audio_output.speech.format = SpeechFormat::kPcm;
  speech.usage = nullptr;
  Q4T_CHECK(
      !NativeChatGeneration(request, sink, 1024, {&plan, nullptr, &speech})
           .Valid());
  return true;
}
