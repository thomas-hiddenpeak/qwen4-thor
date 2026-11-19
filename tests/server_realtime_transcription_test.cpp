#include "q4t/server/realtime_transcription_session.h"

#include <chrono>
#include <future>
#include <stdexcept>
#include <thread>

#include "q4t/io/base64.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/realtime_pcm.h"
#include "q4t/test.h"
#include "../src/server/object_protocol.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
std::string Base64(std::string_view raw) {
  std::string out;
  if (!q4t::io::EncodeBase64(raw, kWebSocketMessageLimit, &out))
    throw std::runtime_error("fixture base64");
  return out;
}
Json Parse(std::string text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("fixture JSON");
  return result;
}
std::vector<Json> Drain(WebSocketOutput& output) {
  std::vector<Json> events;
  WebSocketDecoder decoder(false);
  while (!output.Front().empty()) {
    const auto frame = output.Front();
    size_t consumed = 0;
    if (decoder.Consume(
            {reinterpret_cast<const uint8_t*>(frame.data()), frame.size()},
            &consumed) != WebSocketRead::kEvent ||
        consumed != frame.size())
      throw std::runtime_error("fixture frame");
    events.push_back(Parse(std::string(decoder.Event().payload)));
    decoder.Acknowledge();
    output.Acknowledge();
  }
  return events;
}
ObjectReply Completed(std::string text = "real executor fixture") {
  ObjectReply reply;
  reply.value = Object({{"text", String(std::move(text))},
                        {"usage", Object({{"type", String("tokens")},
                                          {"input_tokens", Number(23)},
                                          {"output_tokens", Number(7)},
                                          {"total_tokens", Number(30)}})}});
  return reply;
}
struct Fixture {
  WebSocketOutput output;
  RequestRegistry requests;
  std::atomic<int> calls{0}, released{0};
  RealtimeTranscriptionCallbacks callbacks;
  Fixture() {
    callbacks.validate = [](const auto&) { return ObjectReply{}; };
    callbacks.acquire = [&](const std::string& id) {
      return requests.Register(id, "", Clock::now() + 1s);
    };
    callbacks.release = [&](const auto& control) {
      requests.Remove(control);
      ++released;
    };
    callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto& emit) {
      ++calls;
      if (!emit(Object({{"delta", String("real executor fixture")}})))
        return Error(409, "cancelled", "Fixture execution cancelled");
      return Completed();
    };
  }
};
bool Send(RealtimeTranscriptionSession& session, std::string_view text) {
  return session.Message(WebSocketOpcode::kText, text);
}
constexpr auto kAppend =
    R"({"type":"input_audio_buffer.append","audio":"AQACAA=="})";
constexpr auto kCommit = R"({"type":"input_audio_buffer.commit"})";
bool Finish(RealtimeTranscriptionSession& session, WebSocketOutput& output,
            std::vector<Json>* events) {
  const auto deadline = Clock::now() + 1s;
  while (Clock::now() < deadline) {
    if (!session.Tick()) return false;
    auto next = Drain(output);
    for (auto& event : next) events->push_back(std::move(event));
    if (session.QueuedItems() == 0) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
}  // namespace

Q4T_TEST(realtime_transcription_strict_event_and_error_identity) {
  for (const auto text :
       {R"({"type":"response.create","event_id":"caller"})",
        R"({"type":"input_audio_buffer.append","event_id":"caller","audio":3})",
        R"({"type":"input_audio_buffer.clear","event_id":"caller","extra":0})",
        R"({"type":"session.update","event_id":"caller","session":null})"}) {
    RealtimeTranscriptionCommand out;
    out.audio = "unchanged";
    const auto reply = ParseRealtimeTranscriptionCommand(text, 4096, &out);
    Q4T_CHECK(!reply.ok() && reply.value.GetString("event_id") == "caller");
    Q4T_CHECK(out.audio == "unchanged");
  }
  for (
      const auto text :
      {R"({"type":"input_audio_buffer.clear","type":"input_audio_buffer.commit"})",
       R"({"type":"input_audio_buffer.clear","event_id":"\u0000"})",
       R"({"type":"input_audio_buffer.clear"} [])", "[]", "null"}) {
    RealtimeTranscriptionCommand out;
    Q4T_CHECK(!ParseRealtimeTranscriptionCommand(text, 4096, &out).ok());
  }
  RealtimeTranscriptionCommand out;
  Q4T_CHECK(!ParseRealtimeTranscriptionCommand(kAppend, 16, &out).ok());
  Q4T_CHECK(!ParseRealtimeTranscriptionCommand(kAppend, 0, &out).ok());
  Q4T_CHECK(!ParseRealtimeTranscriptionCommand(kAppend, 4096, nullptr).ok());
  return true;
}

Q4T_TEST(realtime_transcription_canonical_base64_roundtrip_and_padding) {
  for (size_t size = 0; size < 260; ++size) {
    std::string raw(size, '\0');
    for (size_t i = 0; i < size; ++i) raw[i] = char(i);
    auto event = Object({{"type", String("input_audio_buffer.append")},
                         {"audio", String(Base64(raw))}});
    RealtimeTranscriptionCommand out;
    Q4T_CHECK(
        ParseRealtimeTranscriptionCommand(OutputJson(event), 4096, &out).ok());
    Q4T_CHECK(out.audio == raw);
  }
  for (const auto encoded : {"A", "AA", "AAA", "====", "=AAA", "AA=A",
                             "AB==", "AAB=", "AA==AA==", "AA\n=", "_A=="}) {
    const auto event = Object({{"type", String("input_audio_buffer.append")},
                               {"audio", String(encoded)},
                               {"event_id", String("bad_audio")}});
    RealtimeTranscriptionCommand out;
    out.audio = "preserved";
    const auto reply =
        ParseRealtimeTranscriptionCommand(OutputJson(event), 4096, &out);
    Q4T_CHECK(!reply.ok() && reply.value.GetString("event_id") == "bad_audio");
    Q4T_CHECK(out.audio == "preserved");
  }
  return true;
}

Q4T_TEST(realtime_transcription_ga_and_legacy_settings_are_equivalent) {
  const auto ga = Parse(R"({"type":"transcription","audio":{"input":{
    "format":{"type":"audio/pcmu"},"transcription":{"model":"qwen3-asr-0.6b",
    "language":"zh","languages":["zh","en"],"keywords":["Thor"],"prompt":"会议"},
    "turn_detection":{"type":"server_vad","threshold":0.4,
      "prefix_padding_ms":200,"silence_duration_ms":400},
    "noise_reduction":{"type":"near_field"}}},
    "include":["item.input_audio_transcription.logprobs"]})");
  const auto legacy = Parse(R"({"input_audio_format":"g711_ulaw",
    "input_audio_transcription":{"model":"qwen3-asr-0.6b","language":"zh",
    "languages":["zh","en"],"keywords":["Thor"],"prompt":"会议"},
    "turn_detection":{"type":"server_vad","threshold":0.4,
      "prefix_padding_ms":200,"silence_duration_ms":400},
    "input_audio_noise_reduction":{"type":"near_field"},
    "include":["item.input_audio_transcription.logprobs"]})");
  RealtimeTranscriptionSettings a, b;
  Q4T_CHECK(UpdateRealtimeTranscriptionSettings(ga, false, {}, &a).ok());
  Q4T_CHECK(UpdateRealtimeTranscriptionSettings(legacy, true, {}, &b).ok());
  Q4T_CHECK(OutputJson(RealtimeTranscriptionSettingsJson(a, false)) ==
            OutputJson(RealtimeTranscriptionSettingsJson(b, false)));
  Q4T_CHECK(a.format.rate == 8000 && a.request.language == "zh" &&
            a.request.logprobs);
  Q4T_CHECK(a.request.chunking.threshold == .4);
  return true;
}

Q4T_TEST(realtime_transcription_patch_preserves_and_explicit_null_clears) {
  RealtimeTranscriptionSettings old;
  Q4T_CHECK(
      UpdateRealtimeTranscriptionSettings(
          Parse(
              R"({"input_audio_format":"g711_alaw","input_audio_transcription":{
        "language":"en","keywords":["Thor"],"prompt":"context"},
        "turn_detection":{"type":"semantic_vad","eagerness":"high"},
        "input_audio_noise_reduction":{"type":"far_field"}})"),
          true, {}, &old)
          .ok());
  auto next = old;
  Q4T_CHECK(
      UpdateRealtimeTranscriptionSettings(
          Parse(R"({"audio":{"input":{"format":{},"transcription":{"prompt":""},
        "turn_detection":null,"noise_reduction":null}},"include":[]})"),
          false, old, &next)
          .ok());
  Q4T_CHECK(next.format == old.format && next.request.language == "en");
  Q4T_CHECK(next.request.prompt.empty() &&
            next.request.keywords == old.request.keywords);
  Q4T_CHECK(next.noise == RealtimeTranscriptionSettings::Noise::kNone &&
            next.turn == RealtimeTranscriptionSettings::Turn::kManual);
  return true;
}

Q4T_TEST(realtime_transcription_invalid_settings_are_atomic) {
  RealtimeTranscriptionSettings out;
  out.request.prompt = "preserved";
  for (
      const auto text :
      {R"({"type":"realtime"})", R"({"audio":{"output":{}}})",
       R"({"audio":{"input":{"format":{"type":"audio/pcm","rate":16000}}}})",
       R"({"audio":{"input":{"format":{"type":"audio/pcmu","rate":8000}}}})",
       R"({"audio":{"input":{"transcription":{"language":"EN"}}}})",
       R"({"audio":{"input":{"transcription":{"language":"en","languages":["zh"]}}}})",
       R"({"audio":{"input":{"transcription":{"keywords":["x","x"]}}}})",
       R"({"audio":{"input":{"transcription":{"delay":"yesterday"}}}})",
       R"({"include":["item.input_audio_transcription.logprobs","extra"]})",
       R"({"audio":{"input":{"turn_detection":{"type":"server_vad","eagerness":"low"}}}})",
       R"({"audio":{"input":{"turn_detection":{"type":"semantic_vad","threshold":0.3}}}})",
       R"({"audio":{"input":{"turn_detection":{"type":"server_vad","threshold":1.01}}}})",
       R"({"audio":{"input":{"turn_detection":{"type":"server_vad","prefix_padding_ms":0.5}}}})",
       R"({"audio":{"input":{"noise_reduction":{"type":"fake"}}}})"}) {
    Q4T_CHECK(!UpdateRealtimeTranscriptionSettings(Parse(text), false, {}, &out)
                   .ok());
    Q4T_CHECK(out.request.prompt == "preserved");
  }
  return true;
}

Q4T_TEST(realtime_transcription_commit_delta_final_and_actual_usage) {
  Fixture f;
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 30);
  Q4T_CHECK(session.Start());
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(events.size() == 5 && f.calls == 1 && f.released == 1);
  const std::vector<std::string> types = {
      "session.created", "input_audio_buffer.committed",
      "conversation.item.created",
      "conversation.item.input_audio_transcription.delta",
      "conversation.item.input_audio_transcription.completed"};
  std::vector<std::string> ids;
  for (size_t i = 0; i < events.size(); ++i) {
    Q4T_CHECK(events[i].GetString("type") == types[i]);
    const auto id = events[i].GetString("event_id");
    Q4T_CHECK(!id.empty() &&
              std::find(ids.begin(), ids.end(), id) == ids.end());
    ids.push_back(id);
  }
  const auto item = events[1].GetString("item_id");
  Q4T_CHECK(events[2].Find("item")->GetString("id") == item);
  Q4T_CHECK(events[3].GetString("item_id") == item &&
            events[4].GetString("item_id") == item);
  Q4T_CHECK(events[4].GetString("transcript") == events[3].GetString("delta"));
  Q4T_CHECK(events[4].Find("usage")->GetInt("total_tokens") == 30);
  Q4T_CHECK(session.AudioBytes() == 0);
  return true;
}

Q4T_TEST(realtime_transcription_item_snapshot_survives_concurrent_updates) {
  Fixture f;
  std::promise<void> entered;
  std::atomic<bool> unblock{false};
  std::vector<std::string> prompts;
  f.callbacks.execute = [&](const auto& job, const auto& bytes,
                            const auto& control, const auto&) {
    const int call = ++f.calls;
    if (call == 1) {
      entered.set_value();
      while (!unblock && !control->Cancelled())
        std::this_thread::sleep_for(1ms);
    }
    prompts.push_back(job.settings.request.prompt);
    std::vector<float> pcm;
    if (!DecodeRealtimePcm(job.audio, bytes, 16000, &pcm) || pcm.size() != 1)
      return Error(500, "borrow_changed", "Borrowed PCM changed");
    return Completed(std::to_string(call));
  };
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 30);
  Q4T_CHECK(session.Start());
  Q4T_CHECK(
      Send(session, R"({"type":"session.update","session":{"audio":{"input":{
      "transcription":{"prompt":"first"}}}}})"));
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit) && session.Tick());
  Q4T_CHECK(entered.get_future().wait_for(1s) == std::future_status::ready);
  Q4T_CHECK(
      Send(session, R"({"type":"session.update","session":{"audio":{"input":{
      "transcription":{"prompt":"second"}}}}})"));
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(session.QueuedItems() == 2 && f.calls == 1);
  unblock = true;
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(prompts == std::vector<std::string>({"first", "second"}));
  std::string previous;
  for (const auto& event : events)
    if (event.GetString("type") == "input_audio_buffer.committed") {
      Q4T_CHECK(event.GetString("previous_item_id") == previous);
      previous = event.GetString("item_id");
    }
  Q4T_CHECK(f.calls == 2 && f.released == 2);
  return true;
}

Q4T_TEST(realtime_transcription_clear_format_and_failed_commit_preserve_audio) {
  Fixture f;
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 30);
  Q4T_CHECK(session.Start());
  Q4T_CHECK(
      Send(session, R"({"type":"input_audio_buffer.append","audio":"AQ=="})"));
  Q4T_CHECK(Send(
      session, R"({"type":"input_audio_buffer.commit","event_id":"partial"})"));
  Q4T_CHECK(session.AudioBytes() == 1 && session.QueuedItems() == 0);
  auto events = Drain(f.output);
  Q4T_CHECK(events.back().Find("error")->GetString("event_id") == "partial");
  Q4T_CHECK(Send(session, R"({"type":"transcription_session.update","session":{
    "input_audio_format":"g711_ulaw"}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.back().GetString("type") == "error");
  Q4T_CHECK(Send(session, R"({"type":"input_audio_buffer.clear"})"));
  Q4T_CHECK(session.AudioBytes() == 0);
  Q4T_CHECK(Send(session, R"({"type":"transcription_session.update","session":{
    "input_audio_format":"g711_ulaw"}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.back().GetString("type") == "transcription_session.updated");
  Q4T_CHECK(events.back().Find("session")->GetString("input_audio_format") ==
            "g711_ulaw");
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(Send(session, kAppend));
  Q4T_CHECK(Send(session, R"({"type":"input_audio_buffer.clear"})"));
  Q4T_CHECK(session.AudioBytes() == 4 && session.QueuedItems() == 1);
  return true;
}

Q4T_TEST(realtime_transcription_processor_rejection_never_updates_settings) {
  Fixture f;
  f.callbacks.validate = [](const auto& settings) {
    return settings.noise == RealtimeTranscriptionSettings::Noise::kNone
               ? ObjectReply{}
               : Error(400, "no_processor", "No fixture processor");
  };
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 30);
  Q4T_CHECK(session.Start());
  Q4T_CHECK(Send(session,
                 R"({"type":"session.update","event_id":"settings", "session":{
    "audio":{"input":{"noise_reduction":{"type":"near_field"},
    "transcription":{"prompt":"must not leak"}}}}})"));
  Q4T_CHECK(Send(session, R"({"type":"session.update","session":{}})"));
  const auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 3 && events[1].GetString("type") == "error");
  Q4T_CHECK(events[1].Find("error")->GetString("event_id") == "settings");
  const auto* input = events[2].Find("session")->Find("audio")->Find("input");
  Q4T_CHECK(input->Find("noise_reduction")->IsNull());
  Q4T_CHECK(input->Find("transcription")->GetString("prompt").empty());
  return true;
}

Q4T_TEST(realtime_transcription_queue_full_and_duration_are_bounded) {
  Fixture f;
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 1);
  Q4T_CHECK(session.Start());
  Drain(f.output);
  const auto too_long =
      Object({{"type", String("input_audio_buffer.append")},
              {"audio", String(Base64(std::string(48002, '\0')))}});
  Q4T_CHECK(Send(session, OutputJson(too_long)) && session.AudioBytes() == 0);
  Q4T_CHECK(Drain(f.output)[0].Find("error")->GetString("code") ==
            "audio_duration_limit");
  for (size_t i = 0; i < kRealtimeQueuedItems; ++i) {
    Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
    Drain(f.output);
  }
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(session.QueuedItems() == kRealtimeQueuedItems &&
            session.AudioBytes() == 132);
  Q4T_CHECK(Drain(f.output)[0].GetString("type") == "error");
  return true;
}

Q4T_TEST(realtime_transcription_execution_errors_have_one_failed_terminal) {
  for (int mode = 0; mode < 3; ++mode) {
    Fixture f;
    f.callbacks.execute = [mode](const auto&, const auto&, const auto&,
                                 const auto& emit) -> ObjectReply {
      if (!emit(Object({{"delta", String("partial")}})))
        return Error(409, "cancelled", "cancelled");
      if (mode == 0) throw std::runtime_error("fixture compute error");
      if (mode == 1) return ObjectReply{};  // Missing actual usage/text.
      return Error(429, "client_tpm_exhausted", "No quota");
    };
    RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks,
                                         30);
    Q4T_CHECK(session.Start() && Send(session, kAppend) &&
              Send(session, kCommit));
    std::vector<Json> events;
    Q4T_CHECK(Finish(session, f.output, &events));
    Q4T_CHECK(events.size() == 5 &&
              events.back().GetString("type") ==
                  "conversation.item.input_audio_transcription.failed");
    Q4T_CHECK(!events.back().Has("transcript") && !events.back().Has("usage"));
    Q4T_CHECK(f.released == 1 && session.AudioBytes() == 0);
  }
  return true;
}

Q4T_TEST(realtime_transcription_cancel_unblocks_producer_and_preserves_borrow) {
  Fixture f;
  std::promise<void> blocked;
  std::atomic<bool> borrowed_valid{false};
  f.callbacks.execute = [&](const auto&, const auto& bytes, const auto& control,
                            const auto& emit) {
    const auto* pointer = bytes[0].data();
    const auto part = Object({{"delta", String(std::string(65536, 'x'))}});
    blocked.set_value();
    while (emit(part)) {
    }
    borrowed_valid = control->Cancelled() && bytes[0].data() == pointer &&
                     bytes[0].size() == 4 && bytes[0][0] == 1;
    return Error(409, "cancelled", "cancelled");
  };
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 30);
  Q4T_CHECK(session.Start() && Send(session, kAppend) &&
            Send(session, kCommit));
  Q4T_CHECK(session.Tick());
  Q4T_CHECK(blocked.get_future().wait_for(1s) == std::future_status::ready);
  const auto deadline = Clock::now() + 1s;
  while (f.output.Bytes() < (2ULL << 20) && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(f.output.Bytes() >= (2ULL << 20));
  const auto start = Clock::now();
  session.Cancel();
  session.Join();
  Q4T_CHECK(Clock::now() - start < 500ms && borrowed_valid && f.released == 1);
  Q4T_CHECK(!session.Tick() && !Send(session, kAppend));
  const auto events = Drain(f.output);
  for (const auto& event : events)
    Q4T_CHECK(event.GetString("type") !=
              "conversation.item.input_audio_transcription.completed");
  return true;
}

Q4T_TEST(realtime_transcription_commit_output_overflow_never_launches_worker) {
  Fixture f;
  WebSocketOutput small(4096, 1);
  f.callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto&) {
    ++f.calls;
    return Completed();
  };
  RealtimeTranscriptionSession session("sess_test", small, f.callbacks, 30);
  Q4T_CHECK(session.Start());
  Drain(small);
  // Two commit events need two slots; this constructor's tiny mailbox is
  // intentionally rejected by the application path, with no worker launched.
  Q4T_CHECK(Send(session, kAppend));
  Q4T_CHECK(!Send(session, kCommit));
  Q4T_CHECK(f.calls == 0);
  return true;
}

Q4T_TEST(realtime_transcription_terminal_waits_for_output_capacity) {
  Fixture f;
  f.callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto&) {
    ++f.calls;
    return Completed();
  };
  RealtimeTranscriptionSession session("sess_test", f.output, f.callbacks, 30);
  Q4T_CHECK(session.Start() && Send(session, kAppend) &&
            Send(session, kCommit));
  Drain(f.output);
  for (size_t i = 0; i < 64; ++i)
    Q4T_CHECK(
        f.output.TryPush(WebSocketOpcode::kText, R"({"type":"occupied"})"));
  Q4T_CHECK(session.Tick());
  const auto deadline = Clock::now() + 1s;
  while (f.released == 0 && Clock::now() < deadline) {
    Q4T_CHECK(session.Tick());
    std::this_thread::sleep_for(1ms);
  }
  Q4T_CHECK(f.released == 1 && session.QueuedItems() == 1 &&
            session.AudioBytes() == 4);
  const auto buffered = Drain(f.output);
  Q4T_CHECK(buffered.size() == 64);
  Q4T_CHECK(session.Tick() && session.QueuedItems() == 0);
  const auto terminal = Drain(f.output);
  Q4T_CHECK(terminal.size() == 1 &&
            terminal[0].GetString("type") ==
                "conversation.item.input_audio_transcription.completed");
  return true;
}

Q4T_TEST(realtime_transcription_capabilities_separate_configured_and_pending) {
  const auto absent = Parse(ChatCapabilitiesJson("primary", false, false));
  const auto present = Parse(
      ChatCapabilitiesJson("primary", false, false, 0, nullptr, false, true));
  Q4T_CHECK(!absent.Find("realtime")->GetBool("transcription_configured"));
  const auto* realtime = present.Find("realtime");
  Q4T_CHECK(realtime->GetBool("transcription_configured"));
  Q4T_CHECK(realtime->GetBool("manual_commit") &&
            realtime->GetBool("per_turn_client_admission"));
  Q4T_CHECK(realtime->GetString("usage") == "actual_tokens_per_item");
  Q4T_CHECK(!realtime->GetBool("automatic_replay"));
  Q4T_CHECK(realtime->GetArray("pending_execution")->array.size() == 6);
  Q4T_CHECK(present.GetBool("implementation_only") &&
            !present.GetBool("release_attestation"));
  return true;
}
