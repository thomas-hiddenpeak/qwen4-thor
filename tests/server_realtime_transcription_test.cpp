#include "q4t/server/realtime_transcription_session.h"

#include <algorithm>
#include <chrono>
#include <future>
#include <stdexcept>
#include <thread>

#include "q4t/io/base64.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_audio_export.h"
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
  WebSocketDecoder decoder(false, kWebSocketMaximumLimit);
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
std::string CommitId(const std::vector<Json>& events) {
  for (const auto& event : events)
    if (event.GetString("type") == "input_audio_buffer.committed")
      return event.GetString("item_id");
  throw std::runtime_error("missing actual commit identity");
}
bool Delete(RealtimeTranscriptionSession& session, const std::string& id) {
  return Send(session,
              OutputJson(Object({{"type", String("conversation.item.delete")},
                                 {"item_id", String(id)},
                                 {"event_id", String("delete_client")}})));
}
bool Retrieve(RealtimeTranscriptionSession& session, const std::string& id) {
  return Send(session,
              OutputJson(Object({{"type", String("conversation.item.retrieve")},
                                 {"item_id", String(id)},
                                 {"event_id", String("retrieve_client")}})));
}
const Json& Content(const Json& event) {
  const auto* item = event.Find("item");
  if (!item || event.GetString("type") != "conversation.item.retrieved")
    throw std::runtime_error("fixture retrieved item missing");
  const auto* content = item->GetArray("content");
  if (!content || content->array.size() != 1)
    throw std::runtime_error("fixture retrieved content missing");
  return content->array.front();
}
}  // namespace

Q4T_TEST(realtime_transcription_composed_configure_precedes_only_one_ack) {
  Fixture f;
  RealtimeTranscriptionSettings seen;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    seen = job.settings;
    ++f.calls;
    return Completed("actual fixture input");
  };
  RealtimeTranscriptionSession session("sess_composed", f.output, f.callbacks,
                                       30);
  RealtimeTranscriptionSettings next;
  next.format = {RealtimeAudioEncoding::kMuLaw, 8000};
  next.request.language = "zh";
  next.request.prompt = "configured before start";
  Q4T_CHECK(session.Configure(next).ok());
  Q4T_CHECK(Drain(f.output).empty() && f.calls == 0);
  Q4T_CHECK(session.Start());
  auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 &&
            events[0].GetString("type") == "session.created");
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(f.calls == 1 && f.released == 1);
  Q4T_CHECK(seen.format == next.format && seen.request.language == "zh" &&
            seen.request.prompt == "configured before start");
  return true;
}

Q4T_TEST(realtime_transcription_composed_configure_preserves_pending_audio) {
  Fixture f;
  RealtimeTranscriptionSettings seen;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    seen = job.settings;
    ++f.calls;
    return Completed();
  };
  RealtimeTranscriptionSession session("sess_composed", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(session.Start());
  Drain(f.output);
  Q4T_CHECK(Send(session, kAppend));
  const auto bytes = session.AudioBytes();
  RealtimeTranscriptionSettings next;
  next.format = {RealtimeAudioEncoding::kMuLaw, 8000};
  const auto reply = session.Configure(next);
  Q4T_CHECK(!reply.ok() && reply.code == "input_audio_buffer_not_empty");
  Q4T_CHECK(session.AudioBytes() == bytes && Drain(f.output).empty());
  Q4T_CHECK(Send(session, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(seen.format == RealtimeAudioFormat{} && f.calls == 1);
  Q4T_CHECK(session.Configure(next).ok());
  Q4T_CHECK(Drain(f.output).empty() && session.AudioBytes() == 0);
  return true;
}

Q4T_TEST(realtime_transcription_composed_unconfirmed_settings_never_commit) {
  Fixture f;
  int failure = 0;
  f.callbacks.validate = [&](const auto&) {
    if (failure == 1)
      return Error(503, "asr_failed", "fixture processor failure");
    ObjectReply reply;
    reply.commit_unknown = failure == 2;
    return reply;
  };
  RealtimeTranscriptionSettings seen;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    seen = job.settings;
    return Completed();
  };
  RealtimeTranscriptionSession session("sess_composed", f.output, f.callbacks,
                                       30);
  failure = 2;
  Q4T_CHECK(!session.Start() && Drain(f.output).empty());
  failure = 0;
  Q4T_CHECK(session.Start());
  Drain(f.output);
  RealtimeTranscriptionSettings next;
  next.request.prompt = "must not commit";
  for (const int kind : {1, 2}) {
    failure = kind;
    const auto reply = session.Configure(next);
    Q4T_CHECK(!reply.ok() || reply.commit_unknown);
    Q4T_CHECK(Drain(f.output).empty());
  }
  failure = 0;
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(seen.request.prompt.empty());
  return true;
}

Q4T_TEST(realtime_transcription_composed_vad_requires_actual_processor) {
  Fixture f;
  RealtimeTranscriptionSession session("sess_composed", f.output, f.callbacks,
                                       30);
  RealtimeTranscriptionSettings next;
  for (const auto turn : {RealtimeTranscriptionSettings::Turn::kServerVad,
                          RealtimeTranscriptionSettings::Turn::kSemanticVad}) {
    next.turn = turn;
    const auto reply = session.Configure(next);
    Q4T_CHECK(!reply.ok() && reply.code == "realtime_processor_unavailable");
    Q4T_CHECK(Drain(f.output).empty());
  }
  Q4T_CHECK(session.Start());
  const auto events = Drain(f.output);
  const auto* audio = events[0].Find("session")->Find("audio");
  Q4T_CHECK(audio->Find("input")->Find("turn_detection")->IsNull());
  return true;
}

Q4T_TEST(realtime_transcription_composed_stopped_owner_cannot_reconfigure) {
  Fixture f;
  RealtimeTranscriptionSession session("sess_composed", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(session.Start());
  Drain(f.output);
  session.Cancel();
  const auto reply = session.Configure({});
  Q4T_CHECK(!reply.ok() && reply.http_status == 409);
  Q4T_CHECK(Drain(f.output).empty() && f.calls == 0);
  return true;
}

Q4T_TEST(realtime_history_delete_parser_is_strict_and_preserves_error_id) {
  RealtimeTranscriptionCommand out;
  Q4T_CHECK(ParseRealtimeTranscriptionCommand(
                R"({"type":"conversation.item.delete","item_id":"item_a"})",
                4096, &out)
                .ok());
  Q4T_CHECK(out.kind == RealtimeTranscriptionCommandKind::kDelete &&
            out.item_id == "item_a");
  for (
      const auto text :
      {R"({"type":"conversation.item.delete","event_id":"bad"})",
       R"({"type":"conversation.item.delete","event_id":"bad","item_id":null})",
       R"({"type":"conversation.item.delete","event_id":"bad","item_id":""})",
       R"({"type":"conversation.item.delete","event_id":"bad","item_id":"a","audio":""})"}) {
    const auto reply = ParseRealtimeTranscriptionCommand(text, 4096, &out);
    Q4T_CHECK(!reply.ok() && reply.value.GetString("event_id") == "bad" &&
              out.item_id == "item_a");
  }
  return true;
}

Q4T_TEST(realtime_history_queued_turn_uses_actual_prior_completion_at_launch) {
  Fixture f;
  std::vector<std::string> contexts;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    contexts.push_back(job.settings.request.previous_transcript);
    ++f.calls;
    return Completed(f.calls == 1 ? "First actual speech" : "Second speech");
  };
  RealtimeTranscriptionSession session("sess_history", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(session.Start());
  for (int i = 0; i < 2; ++i)
    Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(contexts == std::vector<std::string>({"", "First actual speech"}));
  Q4T_CHECK(f.calls == 2 && f.released == 2);
  return true;
}

Q4T_TEST(realtime_history_queued_delete_skips_execution_and_future_context) {
  Fixture f;
  std::vector<std::string> called, contexts, ids;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    called.push_back(job.id);
    contexts.push_back(job.settings.request.previous_transcript);
    ++f.calls;
    return Completed("Kept speech");
  };
  RealtimeTranscriptionSession session("sess_history", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(session.Start());
  Drain(f.output);
  for (int i = 0; i < 3; ++i) {
    Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
    ids.push_back(CommitId(Drain(f.output)));
  }
  Q4T_CHECK(Delete(session, ids[1]));
  auto deleted = Drain(f.output);
  Q4T_CHECK(deleted.size() == 1 &&
            deleted[0].GetString("type") == "conversation.item.deleted");
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(called == std::vector<std::string>({ids[0], ids[2]}) &&
            contexts == std::vector<std::string>({"", "Kept speech"}));
  for (const auto& event : events)
    Q4T_CHECK(event.GetString("item_id") != ids[1]);
  Q4T_CHECK(session.AudioBytes() == 0 && Delete(session, ids[1]));
  deleted = Drain(f.output);
  Q4T_CHECK(deleted.size() == 1 && deleted[0].Find("error") &&
            deleted[0].Find("error")->GetString("code") == "item_not_found" &&
            deleted[0].Find("error")->GetString("event_id") == "delete_client");
  return true;
}

Q4T_TEST(
    realtime_history_active_delete_keeps_borrow_until_join_and_fences_output) {
  Fixture f;
  std::promise<void> entered;
  auto ready = entered.get_future();
  std::atomic<bool> resume{false}, preserved{false}, late_delta{true};
  f.callbacks.execute = [&](const auto&, const auto& bytes, const auto&,
                            const auto& emit) {
    ++f.calls;
    entered.set_value();
    const auto end = Clock::now() + 1s;
    while (!resume.load() && Clock::now() < end)
      std::this_thread::sleep_for(1ms);
    preserved = bytes[0].size() == 4 && bytes[0][0] == 1;
    late_delta = emit(Object({{"delta", String("Late text")}}));
    return Completed("Late success must not revive a deleted item");
  };
  RealtimeTranscriptionSession session("sess_history", f.output, f.callbacks,
                                       30);
  ScopeExit unblock([&] { resume = true; });
  Q4T_CHECK(session.Start() && Send(session, kAppend) &&
            Send(session, kCommit));
  const auto id = CommitId(Drain(f.output));
  Q4T_CHECK(session.Tick() &&
            ready.wait_for(500ms) == std::future_status::ready);
  Q4T_CHECK(Delete(session, id) && session.Tick());
  Q4T_CHECK(session.AudioBytes() == 4 && session.QueuedItems() == 1 &&
            session.RetainedAudioBytes() == 4);
  const auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 &&
            events[0].GetString("type") == "conversation.item.deleted");
  resume = true;
  std::vector<Json> late;
  Q4T_CHECK(Finish(session, f.output, &late));
  Q4T_CHECK(late.empty() && preserved && !late_delta && f.released == 1 &&
            session.AudioBytes() == 0 && session.RetainedAudioBytes() == 0);
  return true;
}

Q4T_TEST(realtime_history_deletion_changes_future_but_not_running_snapshot) {
  Fixture f;
  std::promise<void> entered;
  auto ready = entered.get_future();
  std::atomic<bool> resume{false}, stable{false};
  std::vector<std::string> contexts;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    const int step = ++f.calls;
    contexts.push_back(job.settings.request.previous_transcript);
    if (step == 2) {
      entered.set_value();
      const auto end = Clock::now() + 1s;
      while (!resume.load() && Clock::now() < end)
        std::this_thread::sleep_for(1ms);
      stable = job.settings.request.previous_transcript == "First";
    }
    return Completed(step == 1 ? "First" : "Second");
  };
  RealtimeTranscriptionSession session("sess_history", f.output, f.callbacks,
                                       30);
  ScopeExit unblock([&] { resume = true; });
  Q4T_CHECK(session.Start() && Send(session, kAppend) &&
            Send(session, kCommit));
  const auto first = CommitId(Drain(f.output));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit) && session.Tick());
  Q4T_CHECK(ready.wait_for(500ms) == std::future_status::ready);
  Q4T_CHECK(Delete(session, first));
  resume = true;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(stable &&
            contexts == std::vector<std::string>({"", "First", "Second"}));
  return true;
}

Q4T_TEST(realtime_history_capacity_is_explicit_and_delete_unblocks_next_turn) {
  Fixture f;
  std::vector<std::string> contexts;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    contexts.push_back(job.settings.request.previous_transcript);
    return Completed(++f.calls == 1 ? std::string(kAsrPromptBytes + 1, 'x')
                                    : "After deletion");
  };
  RealtimeTranscriptionSession session("sess_history", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(session.Start() && Send(session, kAppend) &&
            Send(session, kCommit));
  const auto first = CommitId(Drain(f.output));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  events.clear();
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(f.calls == 1 && events.back().Find("error") &&
            events.back().Find("error")->GetString("code") ==
                "transcription_context_capacity");
  Q4T_CHECK(Delete(session, first));
  Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(f.calls == 2 && contexts == std::vector<std::string>({"", ""}));
  return true;
}

Q4T_TEST(realtime_history_failed_turn_never_becomes_successful_context) {
  Fixture f;
  std::vector<std::string> contexts;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    contexts.push_back(job.settings.request.previous_transcript);
    return ++f.calls == 1 ? Error(503, "fixture_failed", "No successful text")
                          : Completed("Actual successful speech");
  };
  RealtimeTranscriptionSession session("sess_history", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(session.Start());
  for (int i = 0; i < 2; ++i)
    Q4T_CHECK(Send(session, kAppend) && Send(session, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, f.output, &events));
  Q4T_CHECK(contexts == std::vector<std::string>({"", ""}));
  return true;
}

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

Q4T_TEST(
    realtime_transcription_commit_waits_for_control_capacity_before_launch) {
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
  // Two commit events need two slots. The second remains charged/deferred,
  // and the model cannot start before both standard events have been queued.
  Q4T_CHECK(Send(session, kAppend));
  Q4T_CHECK(Send(session, kCommit) && session.Tick());
  Q4T_CHECK(f.calls == 0);
  const auto committed = Drain(small);
  Q4T_CHECK(committed.size() == 1 &&
            committed[0].GetString("type") == "input_audio_buffer.committed");
  std::vector<Json> events;
  Q4T_CHECK(Finish(session, small, &events));
  Q4T_CHECK(events.size() == 2 &&
            events.front().GetString("type") == "conversation.item.created");
  Q4T_CHECK(f.calls == 1);
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
  Q4T_CHECK(realtime->GetArray("pending_execution")->array.size() == 2);
  const auto* semantic = realtime->Find("semantic_vad");
  Q4T_CHECK(
      semantic && semantic->GetBool("implemented") &&
      !semantic->GetBool("configured") && !semantic->GetBool("calibrated") &&
      semantic->GetInt("max_model_steps") == 8 &&
      semantic->GetString("usage") == "included_in_item_and_client_tokens");
  const auto* continuous = realtime->Find("continuous_input");
  Q4T_CHECK(continuous && continuous->GetBool("implemented") &&
            continuous->GetString("commit") == "explicit" &&
            continuous->GetArray("delay")->array.size() == 5 &&
            continuous->GetBool("immutable_prefix"));
  const auto* history = realtime->Find("transcription_history");
  Q4T_CHECK(history && history->GetBool("retrieve") &&
            history->GetBool("delete") &&
            history->GetString("retrieved_audio") == "actual_processed_pcm" &&
            history->GetInt("session_host_reservation_bytes") ==
                int64_t(kRealtimeSessionHostBytes));
  Q4T_CHECK(realtime->Find("server_vad")->GetBool("implemented"));
  Q4T_CHECK(!realtime->Find("server_vad")->GetBool("configured"));
  Q4T_CHECK(present.GetBool("implementation_only") &&
            !present.GetBool("release_attestation"));
  return true;
}

namespace {
constexpr auto kServerVad = R"({"type":"transcription_session.update",
  "session":{"turn_detection":{"type":"server_vad","threshold":0.5,
    "prefix_padding_ms":16,"silence_duration_ms":32}}})";
bool Audio(RealtimeTranscriptionSession& session, size_t samples) {
  const std::string raw(samples * 2, '\0');
  return Send(session,
              OutputJson(Object({{"type", String("input_audio_buffer.append")},
                                 {"audio", String(Base64(raw))}})));
}
}  // namespace

Q4T_TEST(
    realtime_transcription_vad_commits_actual_prefix_without_client_commit) {
  Fixture f;
  size_t frames = 0;
  f.callbacks.vad_step = [&](auto, auto*, float* p) {
    *p = frames == 1 || frames == 2 ? 1 : 0;
    ++frames;
    return q4t::Status{};
  };
  RealtimeAudioItem actual;
  f.callbacks.execute = [&](const auto& job, const auto& bytes, const auto&,
                            const auto&) {
    ++f.calls;
    actual = job.audio;
    if (bytes[0].size() + bytes[1].size() != actual.bytes)
      return Error(500, "fixture_audio", "Audio byte identity differs");
    return Completed("actual prefix fixture");
  };
  RealtimeTranscriptionSession s("sess_vad", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kServerVad) && Audio(s, 6 * 768 + 48));
  std::vector<Json> events;
  const auto deadline = Clock::now() + 1s;
  while (Clock::now() < deadline && (frames < 6 || f.released < 1)) {
    Q4T_CHECK(s.Tick());
    for (auto& event : Drain(f.output)) events.push_back(std::move(event));
    std::this_thread::sleep_for(1ms);
  }
  s.Join();
  Q4T_CHECK(frames == 6 && f.calls == 1 && f.released == 1);
  Q4T_CHECK(actual.begin_ticks == 768 && actual.end_ticks == 6144);
  Q4T_CHECK(actual.samples == 2688 && actual.bytes == 5376);
  std::string id;
  int started = 0, stopped = 0, committed = 0, completed = 0;
  for (const auto& e : events) {
    const auto type = e.GetString("type");
    if (type == "input_audio_buffer.speech_started") {
      ++started;
      id = e.GetString("item_id");
      Q4T_CHECK(e.GetInt("audio_start_ms") == 16);
    } else if (type == "input_audio_buffer.speech_stopped") {
      ++stopped;
      Q4T_CHECK(e.GetString("item_id") == id &&
                e.GetInt("audio_end_ms") == 128);
    } else if (type == "input_audio_buffer.committed") {
      ++committed;
      Q4T_CHECK(stopped == 1 && e.GetString("item_id") == id);
    } else if (type ==
               "conversation.item.input_audio_transcription.completed") {
      ++completed;
      Q4T_CHECK(committed == 1 && e.GetString("item_id") == id);
    }
  }
  Q4T_CHECK(started == 1 && stopped == 1 && committed == 1 && completed == 1);
  return true;
}

Q4T_TEST(realtime_transcription_vad_backpressure_clear_and_cancel_are_bounded) {
  Fixture f;
  int frames = 0;
  f.callbacks.vad_step = [&](auto, auto*, float* p) {
    ++frames;
    *p = 1;
    return q4t::Status{};
  };
  RealtimeTranscriptionSession s("sess_vad", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kServerVad) && Audio(s, 3 * 768));
  Drain(f.output);
  for (int i = 0; i < 64; ++i)
    Q4T_CHECK(f.output.TryPush(WebSocketOpcode::kText, R"({"type":"busy"})"));
  Q4T_CHECK(s.Tick() && frames == 1 && f.calls == 0);
  for (int i = 0; i < 10; ++i) Q4T_CHECK(s.Tick());
  Q4T_CHECK(frames == 1 && f.calls == 0);
  Q4T_CHECK(Send(s, R"({"type":"input_audio_buffer.clear"})"));
  Q4T_CHECK(s.AudioBytes() == 0 && Drain(f.output).size() == 64);
  Q4T_CHECK(s.Tick());
  const auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 2);
  Q4T_CHECK(events[0].GetString("type") == "input_audio_buffer.speech_started");
  Q4T_CHECK(events[1].GetString("type") == "input_audio_buffer.cleared");
  Q4T_CHECK(Audio(s, 768 + 48) && s.Tick());
  const auto next = Drain(f.output);
  Q4T_CHECK(next.size() == 1 && next[0].GetInt("audio_start_ms") == 96);
  Q4T_CHECK(next[0].GetString("item_id") != events[0].GetString("item_id"));
  s.Cancel();
  s.Join();
  Q4T_CHECK(!s.Tick() && f.calls == 0 && s.QueuedItems() == 0);
  return true;
}

Q4T_TEST(realtime_transcription_vad_policy_change_and_manual_override) {
  Fixture f;
  RealtimeTranscriptionSession missing("sess_missing", f.output, f.callbacks,
                                       30);
  Q4T_CHECK(missing.Start() && Send(missing, kServerVad));
  auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 2 && events.back().GetString("type") == "error");
  f.callbacks.vad_step = [](auto, auto*, float* p) {
    *p = 1;
    return q4t::Status{};
  };
  RealtimeTranscriptionSession s("sess_vad", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kServerVad) && Audio(s, 768 + 49) && s.Tick());
  events = Drain(f.output);
  const auto id = events.back().GetString("item_id");
  Q4T_CHECK(!id.empty());
  Q4T_CHECK(
      Send(s, R"({"type":"transcription_session.update","event_id":"policy",
    "session":{"turn_detection":null}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 && events[0].GetString("type") == "error");
  Q4T_CHECK(events[0].Find("error")->GetString("event_id") == "policy");
  Q4T_CHECK(Send(s, kCommit));
  events = Drain(f.output);
  Q4T_CHECK(events.size() == 3 && events[0].GetString("item_id") == id);
  Q4T_CHECK(events[0].GetInt("audio_end_ms") == 34);
  Q4T_CHECK(events[1].GetString("item_id") == id);
  std::vector<Json> done;
  Q4T_CHECK(Finish(s, f.output, &done) && f.calls == 1);
  Q4T_CHECK(Send(s, R"({"type":"transcription_session.update",
    "session":{"turn_detection":null}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 &&
            events[0].GetString("type") == "transcription_session.updated");
  return true;
}

Q4T_TEST(realtime_transcription_vad_compute_failure_never_commits_success) {
  Fixture f;
  f.callbacks.vad_step = [](auto, auto*, float*) {
    return q4t::Status::Fail("injected VAD CPU failure");
  };
  RealtimeTranscriptionSession s("sess_vad", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kServerVad) && Audio(s, 768 + 48));
  Drain(f.output);
  Q4T_CHECK(!s.Tick());
  s.Cancel();
  s.Join();
  Q4T_CHECK(f.calls == 0 && s.QueuedItems() == 0 && Drain(f.output).empty());
  return true;
}

namespace {
constexpr auto kNoise = R"({"type":"transcription_session.update",
  "session":{"input_audio_noise_reduction":{"type":"near_field"}}})";
std::string Signal(size_t count) {
  std::string result(count * 2, '\0');
  for (size_t i = 0; i < count; ++i) {
    const auto word = uint16_t((i * 31) % 30000);
    result[i * 2] = char(word);
    result[i * 2 + 1] = char(word >> 8);
  }
  return result;
}
bool Raw(RealtimeTranscriptionSession& session, const std::string& raw,
         WebSocketOutput& output) {
  const bool ok = Send(
      session, OutputJson(Object({{"type", String("input_audio_buffer.append")},
                                  {"audio", String(Base64(raw))}})));
  if (!ok) {
    for (const auto& event : Drain(output))
      std::fprintf(stderr, "Noise append failure: %s\n",
                   OutputJson(event).c_str());
  }
  return ok;
}
std::vector<float> ExpectedNoise(const std::string& raw) {
  RealtimeNoiseInput pipe({}, q4t::audio::NoiseReduction::kNearField, 0);
  std::vector<float> out;
  const auto sink = [&](auto samples) {
    out.insert(out.end(), samples.begin(), samples.end());
    return q4t::Status{};
  };
  if (!pipe.Append({reinterpret_cast<const uint8_t*>(raw.data()), raw.size()},
                   sink) ||
      !pipe.Finish(sink))
    throw std::runtime_error("fixture noise source");
  return out;
}
}  // namespace

Q4T_TEST(
    realtime_transcription_noise_manual_drain_preserves_fractional_clock_and_settings) {
  Fixture f;
  std::vector<RealtimeAudioItem> items;
  std::vector<std::vector<float>> samples;
  f.callbacks.execute = [&](const auto& job, const auto& bytes, const auto&,
                            const auto&) {
    std::vector<float> value;
    if (!DecodeRealtimePcm(job.audio, bytes, 16000, &value))
      return Error(500, "fixture_decode", "decode");
    items.push_back(job.audio);
    samples.push_back(std::move(value));
    ++f.calls;
    return Completed();
  };
  RealtimeTranscriptionSession s("sess_noise", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kNoise));
  const auto a = Signal(1025), b = Signal(2048);
  Q4T_CHECK(Raw(s, a, f.output) && Send(s, kCommit) && Raw(s, b, f.output) &&
            Send(s, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(items.size() == 2 && samples[0] == ExpectedNoise(a) &&
            samples[1] == ExpectedNoise(b));
  Q4T_CHECK(items[0].format.encoding == RealtimeAudioEncoding::kFloat32);
  Q4T_CHECK(items[0].begin_ticks == 0 && items[0].end_ticks == 2049);
  Q4T_CHECK(items[1].begin_ticks == 2050 && items[1].end_ticks == 6145);
  Q4T_CHECK(Send(s, R"({"type":"transcription_session.update",
    "session":{"input_audio_noise_reduction":null}})"));
  Q4T_CHECK(Send(s, kAppend) && Send(s, kCommit) &&
            Finish(s, f.output, &events));
  Q4T_CHECK(items.size() == 3 && items[2].begin_ticks == 6146);
  Q4T_CHECK(items[2].format.encoding == RealtimeAudioEncoding::kPcm16);
  return true;
}

Q4T_TEST(
    realtime_transcription_noise_vad_and_asr_borrow_identical_processed_samples) {
  Fixture f;
  std::vector<float> vad_samples, asr_samples;
  RealtimeAudioItem actual;
  size_t frames = 0;
  f.callbacks.vad_step = [&](auto samples, auto*, float* p) {
    vad_samples.insert(vad_samples.end(), samples.begin(), samples.end());
    *p = frames == 1 || frames == 2 ? 1 : 0;
    ++frames;
    return q4t::Status{};
  };
  f.callbacks.execute = [&](const auto& job, const auto& bytes, const auto&,
                            const auto&) {
    actual = job.audio;
    if (!DecodeRealtimePcm(job.audio, bytes, 16000, &asr_samples))
      return Error(500, "fixture_decode", "decode");
    ++f.calls;
    return Completed();
  };
  RealtimeTranscriptionSession s("sess_noise", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kNoise) && Send(s, kServerVad));
  Q4T_CHECK(Raw(s, Signal(6 * 768 + 49), f.output));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events) && f.calls == 1);
  Q4T_CHECK(actual.begin_ticks == 768 && actual.end_ticks == 6144);
  Q4T_CHECK(asr_samples.size() == 1792 && vad_samples.size() >= 2048);
  Q4T_CHECK(std::equal(asr_samples.begin(), asr_samples.end(),
                       vad_samples.begin() + 256));
  Q4T_CHECK(actual.format ==
            RealtimeAudioFormat(RealtimeAudioEncoding::kFloat32, 16000));
  return true;
}

Q4T_TEST(
    realtime_transcription_noise_partial_commit_clear_and_wire_format_are_explicit) {
  Fixture f;
  RealtimeAudioItem actual;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto&,
                            const auto&) {
    actual = job.audio;
    ++f.calls;
    return Completed();
  };
  RealtimeTranscriptionSession s("sess_noise", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kNoise) &&
            Raw(s, Signal(1025) + char(17), f.output));
  Drain(f.output);
  Q4T_CHECK(Send(s, kCommit));
  auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 && events[0].GetString("type") == "error");
  Q4T_CHECK(Send(s, R"({"type":"session.update","session":{"audio":{"input":{
    "format":{"type":"audio/f32","rate":16000}}}}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 && events[0].GetString("type") == "error");
  Q4T_CHECK(Send(s, R"({"type":"input_audio_buffer.clear"})"));
  Q4T_CHECK(s.AudioBytes() == 0 && f.calls == 0);
  Q4T_CHECK(Send(s, R"({"type":"transcription_session.update",
    "session":{"input_audio_noise_reduction":null}})"));
  Q4T_CHECK(Send(s, kAppend) && Send(s, kCommit) &&
            Finish(s, f.output, &events));
  Q4T_CHECK(actual.begin_ticks == 2050 && actual.end_ticks == 2054);
  return true;
}

namespace {
constexpr auto kContinuous = R"({"type":"session.update","session":{
  "audio":{"input":{"transcription":{"delay":"minimal","prompt":"first"},
  "turn_detection":null,"noise_reduction":null}}}})";
constexpr auto kClear = R"({"type":"input_audio_buffer.clear"})";
bool Pcm(RealtimeTranscriptionSession& s, size_t samples,
         int16_t value = 8192) {
  std::string raw;
  raw.reserve(samples * 2);
  for (size_t i = 0; i < samples; ++i) {
    raw += char(uint16_t(value) & 255);
    raw += char(uint16_t(value) >> 8);
  }
  return Send(s,
              OutputJson(Object({{"type", String("input_audio_buffer.append")},
                                 {"audio", String(Base64(raw))}})));
}
bool Until(RealtimeTranscriptionSession& s, WebSocketOutput& output,
           std::vector<Json>* events, const std::function<bool()>& ready) {
  const auto end = Clock::now() + 1s;
  while (Clock::now() < end) {
    if (!s.Tick()) return false;
    auto next = Drain(output);
    for (auto& item : next) events->push_back(std::move(item));
    if (ready()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
size_t Events(const std::vector<Json>& events, std::string_view type) {
  return std::ranges::count_if(events, [&](const auto& event) {
    return event.GetString("type") == type;
  });
}
// Deliberately CPU protocol fixture. This is not model/quality evidence.
ObjectReply StreamFixture(const RealtimeTranscriptionJob& job,
                          const std::shared_ptr<RequestControl>& control,
                          const RealtimeTranscriptSink& emit,
                          std::vector<uint64_t>* extents = nullptr) {
  if (!job.stream) return Error(500, "fixture_owner", "Missing input owner");
  std::string text;
  for (;;) {
    const auto read = job.stream->Read(control->deadline(),
                                       [&] { return control->Cancelled(); });
    if (read.kind == AsrStreamReadKind::kEnd) return Completed(text);
    if (read.kind != AsrStreamReadKind::kReady)
      return Error(409, "fixture_cancelled", "CPU fixture cancelled");
    if (extents) extents->push_back(read.samples.size());
    if (!job.stream->Complete(read.step))
      return Error(500, "fixture_ticket", "CPU fixture ticket invalid");
    text += "piece";
    if (!emit(Object({{"delta", String("piece")}})))
      return Error(409, "fixture_cancelled", "CPU fixture output cancelled");
  }
}
}  // namespace

Q4T_TEST(realtime_continuous_executes_before_commit_and_finalizes_same_item) {
  Fixture f;
  std::vector<uint64_t> extents;
  std::string prompt;
  f.callbacks.execute = [&](const auto& job, const auto& bytes,
                            const auto& control, const auto& emit) {
    ++f.calls;
    if (!bytes[0].empty() || !bytes[1].empty())
      return Error(500, "fixture_bytes", "Old ring must not be borrowed");
    prompt = job.settings.request.prompt;
    return StreamFixture(job, control, emit, &extents);
  };
  RealtimeTranscriptionSession s("sess_continuous", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Until(s, f.output, &events, [&] {
    return Events(events,
                  "conversation.item.input_audio_transcription.delta") == 1;
  }));
  Q4T_CHECK(
      Events(events, "input_audio_buffer.committed") == 0 &&
      Events(events, "conversation.item.input_audio_transcription.completed") ==
          0);
  Q4T_CHECK(s.QueuedItems() == 1 && s.AudioBytes() == 30 * 16000 * 4);
  Q4T_CHECK(Send(s, R"({"type":"session.update","session":{"audio":{"input":{
    "transcription":{"prompt":"next"}}}}})"));
  Q4T_CHECK(Pcm(s, 1000) && Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK((extents == std::vector<uint64_t>{8000, 9333}));
  Q4T_CHECK(prompt == "first" && f.calls == 1 && f.released == 1);
  Q4T_CHECK(
      Events(events, "input_audio_buffer.committed") == 1 &&
      Events(events, "conversation.item.created") == 1 &&
      Events(events, "conversation.item.input_audio_transcription.completed") ==
          1);
  const auto id = events.back().GetString("item_id");
  Q4T_CHECK(events.back().GetString("transcript") == "piecepiece");
  for (const auto& event : events)
    if (event.Has("item_id")) Q4T_CHECK(event.GetString("item_id") == id);
  Q4T_CHECK(s.AudioBytes() == 0 && s.QueuedItems() == 0);
  return true;
}

Q4T_TEST(
    realtime_continuous_clear_preserves_old_borrow_and_starts_fresh_owner) {
  Fixture f;
  std::atomic<bool> borrowing{false}, preserved{false};
  f.callbacks.execute = [&](const auto& job, const auto&, const auto& control,
                            const auto& emit) {
    const auto ordinal = ++f.calls;
    if (ordinal != 1) return StreamFixture(job, control, emit);
    const auto read = job.stream->Read(control->deadline());
    if (read.kind != AsrStreamReadKind::kReady)
      return Error(500, "fixture_read", "Borrow not ready");
    ScopeExit release([&] { job.stream->Complete(read.step); });
    const float first = read.samples[100];
    borrowing.store(true);
    while (!control->Cancelled()) std::this_thread::sleep_for(1ms);
    preserved = read.samples[100] == first && first == .25f &&
                job.stream->StorageBytes() == 30 * 16000 * 4;
    return Error(409, "fixture_cancelled", "Old CPU fixture cancelled");
  };
  RealtimeTranscriptionSession s("sess_clear", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Until(s, f.output, &events, [&] { return borrowing.load(); }));
  Q4T_CHECK(Send(s, kClear) && s.AudioBytes() == 30 * 16000 * 4);
  Q4T_CHECK(Pcm(s, 13000, -8192) && s.AudioBytes() == 2 * 30 * 16000 * 4);
  Q4T_CHECK(Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK(preserved && f.calls == 2 && f.released == 2 &&
            s.AudioBytes() == 0);
  Q4T_CHECK(
      Events(events, "input_audio_buffer.cleared") == 1 &&
      Events(events, "conversation.item.input_audio_transcription.failed") ==
          1 &&
      Events(events, "conversation.item.input_audio_transcription.completed") ==
          1);
  return true;
}

Q4T_TEST(realtime_continuous_early_failure_requires_clear_without_auto_replay) {
  Fixture f;
  f.callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto&) {
    ++f.calls;
    return Error(429, "client_rpm_exhausted", "CPU quota fixture");
  };
  RealtimeTranscriptionSession s("sess_failed", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(f.calls == 1 &&
            Events(events,
                   "conversation.item.input_audio_transcription.failed") == 1);
  Q4T_CHECK(Pcm(s, 13000) && Send(s, kCommit));
  const auto errors = Drain(f.output);
  Q4T_CHECK(errors.size() == 2 && s.QueuedItems() == 0 && f.calls == 1);
  for (const auto& event : errors)
    Q4T_CHECK(event.Find("error")->GetString("code") ==
              "input_audio_buffer_failed");
  Q4T_CHECK(Send(s, kClear) && Pcm(s, 13000) && Send(s, kCommit));
  Q4T_CHECK(Finish(s, f.output, &events) && f.calls == 2);
  Q4T_CHECK(
      Events(events, "conversation.item.input_audio_transcription.completed") ==
      0);
  return true;
}

Q4T_TEST(realtime_continuous_rejects_success_before_input_eof) {
  Fixture f;
  f.callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto&) {
    ++f.calls;
    return Completed("cannot be final before EOF");
  };
  RealtimeTranscriptionSession s("sess_bad_executor", f.output, f.callbacks,
                                 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(
      Events(events, "conversation.item.input_audio_transcription.failed") ==
          1 &&
      Events(events, "conversation.item.input_audio_transcription.completed") ==
          0);
  Q4T_CHECK(events.back().Find("error")->GetString("code") ==
            "transcription_result_invalid");
  return true;
}

Q4T_TEST(realtime_continuous_pipeline_changes_and_partial_commit_are_explicit) {
  Fixture f;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto& control,
                            const auto& emit) {
    ++f.calls;
    return StreamFixture(job, control, emit);
  };
  RealtimeTranscriptionSession s("sess_policy", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous));
  Drain(f.output);
  Q4T_CHECK(Send(s, R"({"type":"input_audio_buffer.append","audio":"AQ=="})"));
  Q4T_CHECK(Send(s, kCommit));
  Q4T_CHECK(Send(s, R"({"type":"session.update","session":{"audio":{"input":{
    "transcription":{"delay":"high"}}}}})"));
  auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 2 && events[0].GetString("type") == "error" &&
            events[1].GetString("type") == "error");
  Q4T_CHECK(Send(s, kClear) && Finish(s, f.output, &events));
  Q4T_CHECK(Send(s, R"({"type":"session.update","session":{"audio":{"input":{
    "turn_detection":{"type":"server_vad"}}}}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 && events[0].Find("error")->GetString("code") ==
                                      "realtime_delay_turn_conflict");
  Q4T_CHECK(Send(s, R"({"type":"session.update","session":{"audio":{"input":{
    "transcription":{"delay":"high"}}}}})"));
  events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 &&
            events[0].GetString("type") == "session.updated");
  return true;
}

Q4T_TEST(
    realtime_continuous_aggregate_storage_includes_cancelled_pending_jobs) {
  Fixture f;
  RealtimeTranscriptionSession s("sess_capacity", f.output, f.callbacks, 600);
  Q4T_CHECK(s.Start() && Send(s, kContinuous));
  Q4T_CHECK(Pcm(s, 1) && s.AudioBytes() == 600 * 16000 * 4);
  Q4T_CHECK(Send(s, kClear));
  Drain(f.output);
  Q4T_CHECK(Pcm(s, 1));
  const auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 && events[0].Find("error")->GetString("code") ==
                                      "input_audio_buffer_full");
  Q4T_CHECK(s.QueuedItems() == 1 && s.AudioBytes() == 600 * 16000 * 4 &&
            f.calls == 0);
  s.Cancel();
  s.Join();
  return true;
}

Q4T_TEST(realtime_retrieve_parser_preserves_strict_contract_and_error_id) {
  RealtimeTranscriptionCommand out;
  Q4T_CHECK(ParseRealtimeTranscriptionCommand(
                R"({"type":"conversation.item.retrieve","item_id":"item_a"})",
                4096, &out)
                .ok());
  Q4T_CHECK(out.kind == RealtimeTranscriptionCommandKind::kRetrieve &&
            out.item_id == "item_a");
  for (
      const auto text :
      {R"({"type":"conversation.item.retrieve","event_id":"bad"})",
       R"({"type":"conversation.item.retrieve","event_id":"bad","item_id":null})",
       R"({"type":"conversation.item.retrieve","event_id":"bad","item_id":""})",
       R"({"type":"conversation.item.retrieve","event_id":"bad","item_id":"a","audio":""})"}) {
    const auto reply = ParseRealtimeTranscriptionCommand(text, 4096, &out);
    Q4T_CHECK(!reply.ok() && reply.value.GetString("event_id") == "bad" &&
              out.item_id == "item_a");
  }
  return true;
}

Q4T_TEST(realtime_retrieve_actual_noise_audio_before_and_after_execution) {
  Fixture f;
  std::vector<float> actual;
  const std::string transcript = "Actual \"speech\"\n中文";
  f.callbacks.execute = [&](const auto& job, const auto& bytes, const auto&,
                            const auto&) {
    std::vector<float> legacy;
    if (!DecodeRealtimePcm(job.audio, bytes, 16000, &legacy) ||
        !job.processed_audio ||
        !std::ranges::equal(job.processed_audio->Samples(), legacy))
      return Error(500, "fixture_audio", "Actual model PCM changed");
    actual = legacy;
    ++f.calls;
    return Completed(transcript);
  };
  RealtimeTranscriptionSession s("sess_retrieve", f.output, f.callbacks, 30);
  const auto raw = Signal(4800);
  Q4T_CHECK(s.Start() && Send(s, kNoise) && Raw(s, raw, f.output) &&
            Send(s, kCommit));
  const auto id = CommitId(Drain(f.output));
  Q4T_CHECK(Retrieve(s, id));
  const auto pending = Drain(f.output);
  Q4T_CHECK(pending.size() == 1 &&
            Content(pending[0]).Find("transcript")->IsNull());
  const auto before = Content(pending[0]).GetString("audio");
  Q4T_CHECK(before != Base64(raw));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events) && f.calls == 1);
  std::string expected;
  Q4T_CHECK(EncodeRealtimeAudio(actual, {}, 1 << 20, &expected));
  Q4T_CHECK(before == Base64(expected) && s.AudioBytes() == 0 &&
            s.RetainedAudioBytes() == actual.size() * 4);
  Q4T_CHECK(Retrieve(s, id));
  const auto complete = Drain(f.output);
  Q4T_CHECK(complete.size() == 1 &&
            Content(complete[0]).GetString("audio") == before &&
            Content(complete[0]).GetString("transcript") == transcript);
  // Changing the wire configuration must not reinterpret or replace old PCM.
  Q4T_CHECK(Send(s, R"({"type":"transcription_session.update","session":{
    "input_audio_format":"g711_ulaw","input_audio_noise_reduction":null}})"));
  Drain(f.output);
  Q4T_CHECK(Retrieve(s, id));
  const auto reformatted = Drain(f.output);
  Q4T_CHECK(EncodeRealtimeAudio(actual, {RealtimeAudioEncoding::kMuLaw, 8000},
                                1 << 20, &expected));
  Q4T_CHECK(Content(reformatted[0]).GetString("audio") == Base64(expected));
  Q4T_CHECK(Delete(s, id) && s.RetainedAudioBytes() == 0);
  return true;
}

Q4T_TEST(
    realtime_retrieve_failed_item_keeps_audio_and_deleted_or_foreign_is_absent) {
  Fixture f;
  f.callbacks.execute = [](const auto&, const auto&, const auto&, const auto&) {
    return Error(429, "client_tpm_exhausted", "No quota");
  };
  RealtimeTranscriptionSession s("sess_retrieve", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kAppend) && Send(s, kCommit));
  const auto id = CommitId(Drain(f.output));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events) && Retrieve(s, id));
  auto retrieved = Drain(f.output);
  Q4T_CHECK(Content(retrieved[0]).Find("transcript")->IsNull() &&
            !Content(retrieved[0]).GetString("audio").empty());
  Fixture other;
  RealtimeTranscriptionSession foreign("sess_other", other.output,
                                       other.callbacks, 30);
  Q4T_CHECK(foreign.Start());
  Drain(other.output);
  Q4T_CHECK(Retrieve(foreign, id));
  retrieved = Drain(other.output);
  Q4T_CHECK(retrieved[0].Find("error")->GetString("code") == "item_not_found");
  Q4T_CHECK(Delete(s, id) && s.RetainedAudioBytes() == 0);
  Drain(f.output);
  Q4T_CHECK(Retrieve(s, id));
  retrieved = Drain(f.output);
  Q4T_CHECK(retrieved[0].Find("error")->GetString("code") == "item_not_found" &&
            retrieved[0].Find("error")->GetString("event_id") ==
                "retrieve_client");
  return true;
}

Q4T_TEST(
    realtime_retrieve_continuous_hidden_until_commit_then_same_sealed_pcm) {
  Fixture f;
  std::vector<float> actual;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto& control,
                            const auto& emit) {
    ++f.calls;
    auto reply = StreamFixture(job, control, emit);
    if (reply.ok()) {
      const auto samples = job.processed_audio->Samples();
      actual.assign(samples.begin(), samples.end());
    }
    return reply;
  };
  RealtimeTranscriptionSession s("sess_stream_retrieve", f.output, f.callbacks,
                                 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Until(s, f.output, &events, [&] {
    return Events(events,
                  "conversation.item.input_audio_transcription.delta") == 1;
  }));
  const auto id = events.back().GetString("item_id");
  Q4T_CHECK(Retrieve(s, id));
  const auto hidden = Drain(f.output);
  Q4T_CHECK(hidden.size() == 1 &&
            hidden[0].Find("error")->GetString("code") == "item_not_found");
  Q4T_CHECK(Pcm(s, 1000) && Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK(actual.size() == 9333 && s.AudioBytes() == 0 &&
            s.RetainedAudioBytes() == 30 * 16000 * 4 && Retrieve(s, id));
  std::string expected;
  Q4T_CHECK(EncodeRealtimeAudio(actual, {}, 1 << 20, &expected));
  const auto complete = Drain(f.output);
  Q4T_CHECK(Content(complete[0]).GetString("audio") == Base64(expected) &&
            Content(complete[0]).GetString("transcript") == "piecepiece");
  Q4T_CHECK(Delete(s, id) && s.RetainedAudioBytes() == 0);
  return true;
}

Q4T_TEST(
    realtime_retrieve_large_output_uses_explicit_budget_and_deferred_order) {
  Fixture f;
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 1,
                         kWebSocketMaximumLimit);
  RealtimeTranscriptionSession s("sess_large", output, f.callbacks, 30);
  Q4T_CHECK(s.Start());
  Drain(output);
  Q4T_CHECK(Pcm(s, 20 * 24000) && Send(s, kCommit));
  const auto id = CommitId(Drain(output));
  std::vector<Json> completed;
  Q4T_CHECK(Finish(s, output, &completed));
  Q4T_CHECK(Retrieve(s, id));
  Q4T_CHECK(output.Front().size() > kWebSocketOutputMessageBytes);
  // Keep the first actual response queued. A second retrieval and deletion
  // remain bounded and ordered; deleting does not corrupt either response.
  Q4T_CHECK(Retrieve(s, id) && Delete(s, id) && s.RetainedAudioBytes() == 0);
  Q4T_CHECK(s.Tick());
  const auto first = Drain(output);
  Q4T_CHECK(first.size() == 1 && Content(first[0]).GetString("audio").size() ==
                                     20 * 24000 * 2 * 4 / 3);
  Q4T_CHECK(s.Tick());
  const auto second = Drain(output);
  Q4T_CHECK(second.size() == 1 && Content(second[0]).GetString("audio") ==
                                      Content(first[0]).GetString("audio"));
  Q4T_CHECK(s.Tick());
  const auto deleted = Drain(output);
  Q4T_CHECK(deleted.size() == 1 &&
            deleted[0].GetString("type") == "conversation.item.deleted");
  return true;
}

Q4T_TEST(realtime_retrieve_full_native_text_budget_keeps_json_escaping) {
  Fixture f;
  WebSocketOutput output(kWebSocketOutputMaximumBytes, 64,
                         kWebSocketMaximumLimit);
  const std::string text(4ULL << 20, '\1');
  f.callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto&) {
    ++f.calls;
    return Completed(text);
  };
  RealtimeTranscriptionSession s("sess_full_text", output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kAppend) && Send(s, kCommit));
  const auto id = CommitId(Drain(output));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, output, &events));
  Q4T_CHECK(events.back().GetString("type") ==
                "conversation.item.input_audio_transcription.completed" &&
            events.back().GetString("transcript") == text);
  Q4T_CHECK(Retrieve(s, id));
  const auto result = Drain(output);
  Q4T_CHECK(result.size() == 1 &&
            Content(result[0]).GetString("transcript") == text);
  return true;
}

namespace {
constexpr auto kSemanticVad = R"({"type":"session.update","session":{
  "type":"transcription","audio":{"input":{
    "turn_detection":{"type":"semantic_vad","eagerness":"high"}}}}})";
struct SemanticFixture : Fixture {
  std::atomic<bool> allow_score{true}, score_entered{false};
  std::atomic<bool> hold_cleanup{false}, score_cancelled{false};
  std::atomic<int> accepted{0};
  bool score = true, all_voiced = false;
  size_t frames = 0;
  std::vector<float> final_samples;
  SemanticFixture() {
    callbacks.semantic_ready = true;
    callbacks.acquire = [&](const auto& id) {
      return requests.Register(id, "", Clock::now() + 5s);
    };
    callbacks.vad_step = [&](auto, auto*, float* p) {
      *p = all_voiced || frames < 5 ? 1 : 0;
      ++frames;
      return q4t::Status{};
    };
    callbacks.execute = [&](const auto& job, const auto&, const auto& control,
                            const auto&) {
      ++calls;
      if (!job.stream || !job.semantic)
        return Error(500, "fixture_owner", "Missing semantic stream");
      for (;;) {
        auto input = job.stream->Read(control->deadline(),
                                      [&] { return control->Cancelled(); });
        if (input.kind == AsrStreamReadKind::kEnd) break;
        if (input.kind != AsrStreamReadKind::kReady)
          return Error(409, "fixture_cancelled", "Cancelled");
        if (!job.stream->Complete(input.step))
          return Error(500, "fixture_complete", "Complete");
        if (!score || input.step.final) continue;
        auto child =
            std::make_shared<RequestControl>("child", "", control->deadline());
        RealtimeSemanticTicket ticket;
        if (!job.semantic->TryScore(
                input.step.sequence,
                job.audio.begin_ticks + input.step.samples * 3, child, &ticket))
          continue;
        score_entered = true;
        const auto deadline = Clock::now() + 2s;
        while ((!allow_score || hold_cleanup) && Clock::now() < deadline) {
          if (child->Cancelled()) score_cancelled = true;
          std::this_thread::sleep_for(1ms);
        }
        if (job.semantic->Complete(ticket, 1) ==
            RealtimeSemanticScore::kAccepted)
          ++accepted;
      }
      const auto samples = job.stream->SealedSamples(job.audio.epoch);
      final_samples.assign(samples.begin(), samples.end());
      return Completed("Explicit CPU stream fixture");
    };
  }
};
bool AwaitScore(RealtimeTranscriptionSession& s, SemanticFixture& f,
                std::vector<Json>* events) {
  const auto deadline = Clock::now() + 1s;
  while (Clock::now() < deadline && !f.score_entered) {
    if (!s.Tick()) return false;
    auto pending = Drain(f.output);
    events->insert(events->end(), pending.begin(), pending.end());
    std::this_thread::sleep_for(1ms);
  }
  // The worker can enter before this helper's first poll. Preserve already
  // queued speech/start events in that schedule as well as the delayed one.
  auto pending = Drain(f.output);
  events->insert(events->end(), pending.begin(), pending.end());
  return f.score_entered;
}
size_t EventsOf(const std::vector<Json>& events, std::string_view type) {
  return std::count_if(events.begin(), events.end(), [&](const auto& event) {
    return event.GetString("type") == type;
  });
}
}  // namespace

Q4T_TEST(realtime_semantic_session_waits_for_content_score_then_commits_once) {
  SemanticFixture f;
  f.allow_score = false;
  RealtimeTranscriptionSession s("sess_semantic", f.output, f.callbacks, 30);
  ScopeExit release([&] {
    f.allow_score = true;
    f.hold_cleanup = false;
  });
  Q4T_CHECK(s.Start() && Send(s, kSemanticVad));
  Q4T_CHECK(Raw(s, Signal(30 * 768), f.output));
  std::vector<Json> events;
  Q4T_CHECK(AwaitScore(s, f, &events));
  Q4T_CHECK(EventsOf(events, "input_audio_buffer.committed") == 0 &&
            EventsOf(events, "input_audio_buffer.speech_started") == 1);
  f.allow_score = true;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(f.calls == 1 && f.accepted == 1 && f.released == 1);
  Q4T_CHECK(EventsOf(events, "input_audio_buffer.committed") == 1 &&
            EventsOf(events, "input_audio_buffer.speech_stopped") == 1 &&
            EventsOf(events,
                     "conversation.item.input_audio_transcription.completed") ==
                1);
  Q4T_CHECK(!f.final_samples.empty() && f.final_samples.size() < 16000);
  const auto id = CommitId(events);
  Q4T_CHECK(Retrieve(s, id));
  const auto retrieved = Drain(f.output);
  Q4T_CHECK(retrieved.size() == 1 &&
            Content(retrieved[0]).GetString("transcript") ==
                "Explicit CPU stream fixture");
  return true;
}

Q4T_TEST(realtime_semantic_session_unknown_score_uses_real_audio_maximum) {
  SemanticFixture f;
  f.score = false;
  RealtimeTranscriptionSession s("sess_semantic_bound", f.output, f.callbacks,
                                 30);
  Q4T_CHECK(s.Start() && Send(s, kSemanticVad));
  Q4T_CHECK(Raw(s, Signal(30 * 768), f.output));
  std::vector<Json> events = Drain(f.output);
  for (int i = 0; i < 5; ++i) Q4T_CHECK(s.Tick());
  auto pending = Drain(f.output);
  events.insert(events.end(), pending.begin(), pending.end());
  Q4T_CHECK(EventsOf(events, "input_audio_buffer.committed") == 0);
  Q4T_CHECK(Raw(s, Signal(45 * 768), f.output));
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(f.calls == 1 && f.accepted == 0 && f.released == 1);
  // Five 32-ms voiced frames followed by the configured two real seconds.
  Q4T_CHECK(f.final_samples.size() >= 2160 * 16 &&
            f.final_samples.size() <= 2192 * 16);
  Q4T_CHECK(EventsOf(events, "input_audio_buffer.committed") == 1 &&
            EventsOf(events,
                     "conversation.item.input_audio_transcription.completed") ==
                1);
  return true;
}

Q4T_TEST(
    realtime_semantic_session_clear_cancels_score_but_retains_worker_audio) {
  SemanticFixture f;
  f.allow_score = false;
  f.hold_cleanup = true;
  RealtimeTranscriptionSession s("sess_semantic_clear", f.output, f.callbacks,
                                 30);
  ScopeExit release([&] {
    f.allow_score = true;
    f.hold_cleanup = false;
  });
  Q4T_CHECK(s.Start() && Send(s, kSemanticVad) &&
            Raw(s, Signal(30 * 768), f.output));
  std::vector<Json> events;
  Q4T_CHECK(AwaitScore(s, f, &events));
  const auto held = s.AudioBytes();
  Q4T_CHECK(held > 0 && Send(s, R"({"type":"input_audio_buffer.clear"})"));
  const auto deadline = Clock::now() + 500ms;
  while (!f.score_cancelled && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(f.score_cancelled && s.AudioBytes() > 0 && s.QueuedItems() == 1);
  f.allow_score = true;
  f.hold_cleanup = false;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(s.AudioBytes() == 0 && s.RetainedAudioBytes() == 0 &&
            f.accepted == 0);
  Q4T_CHECK(EventsOf(events, "input_audio_buffer.committed") == 0 &&
            EventsOf(events,
                     "conversation.item.input_audio_transcription.completed") ==
                0);
  return true;
}

Q4T_TEST(realtime_semantic_session_explicit_commit_preserves_exact_tail) {
  SemanticFixture f;
  f.all_voiced = true;
  RealtimeTranscriptionSession s("sess_semantic_explicit", f.output,
                                 f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kSemanticVad));
  const auto raw = Signal(1025);
  Q4T_CHECK(Raw(s, raw, f.output) && Send(s, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  std::vector<float> expected;
  RealtimeNoiseInput processor({}, q4t::audio::NoiseReduction::kOff, 0);
  const auto sink = [&](auto samples) {
    expected.insert(expected.end(), samples.begin(), samples.end());
    return q4t::Status{};
  };
  Q4T_CHECK(
      processor.Append(
          {reinterpret_cast<const uint8_t*>(raw.data()), raw.size()}, sink) &&
      processor.Finish(sink));
  Q4T_CHECK(f.calls == 1 && f.final_samples == expected &&
            EventsOf(events, "input_audio_buffer.committed") == 1);
  Q4T_CHECK(Send(s, R"({"type":"session.update","session":{
    "type":"transcription","audio":{"input":{"turn_detection":null}}}})"));
  auto updated = Drain(f.output);
  Q4T_CHECK(updated.size() == 1 &&
            updated[0].GetString("type") == "session.updated");
  return true;
}

namespace {
// Actual ledger plus the actual input pipeline; only native model compute is
// a bounded CPU fixture. This is ownership/protocol evidence, not ASR quality.
struct InputOwner : IRealtimeTranscriptionOwner {
  RealtimeConversation history{"sess_composed_history"};
  struct Binding {
    RealtimeTranscriptTicket input;
    RealtimeItemTicket conversation;
  };
  std::map<std::string, Binding> items;
  const std::thread::id thread = std::this_thread::get_id();
  bool correct_thread = true, ready = true, reject_reserve = false;
  bool unknown_commit = false, reject_finish = false;
  int reserved = 0, committed = 0, finished = 0, discarded = 0;
  int started = 0, stopped = 0, ready_calls = 0;
  std::vector<bool> automatic;
  void CheckThread() { correct_thread &= std::this_thread::get_id() == thread; }
  ObjectReply Reserve(const RealtimeTranscriptionJob& job) override {
    CheckThread();
    ++reserved;
    if (reject_reserve) return Error(429, "fixture_full", "Ledger full");
    RealtimeItemTicket ticket;
    if (!history.ReserveAudioInput(job.id, false, 8192, &ticket))
      return Error(429, "fixture_full", "Ledger full");
    items.emplace(job.id, Binding{job.transcript, ticket});
    return {};
  }
  ObjectReply Commit(const RealtimeTranscriptionJob& job, bool is_automatic,
                     std::string* previous_id) override {
    CheckThread();
    if (unknown_commit) {
      *previous_id = "unconfirmed_predecessor";
      ObjectReply reply;
      reply.commit_unknown = true;
      return reply;
    }
    auto previous = history.LastId();
    const auto found = items.find(job.id);
    if (found == items.end() || found->second.input != job.transcript ||
        !history.CommitAudioInput(found->second.conversation))
      return Error(500, "fixture_identity", "Input identity changed");
    *previous_id = std::move(previous);
    ++committed;
    automatic.push_back(is_automatic);
    return {};
  }
  ObjectReply Finish(const RealtimeTranscriptionJob& job,
                     const ObjectReply& result) override {
    CheckThread();
    ++finished;
    const auto found = items.find(job.id);
    if (found == items.end() || found->second.input != job.transcript)
      return Error(500, "fixture_identity", "Input identity changed");
    const auto& ticket = found->second.conversation;
    const auto status =
        result.ok() && !reject_finish
            ? history.CompleteAudioInput(ticket, result.value.GetString("text"))
            : history.FailAudioInput(ticket);
    if (!status) return Error(500, "fixture_settlement", status.message());
    return reject_finish ? Error(503, "fixture_retention", "Retention failed")
                         : ObjectReply{};
  }
  void Discard(const RealtimeTranscriptionJob& job) noexcept override {
    CheckThread();
    ++discarded;
    const auto found = items.find(job.id);
    if (found == items.end() || found->second.input != job.transcript) return;
    history.Erase(found->second.conversation);
    items.erase(found);
  }
  bool Speech(bool start, std::string_view, uint64_t) override {
    CheckThread();
    start ? ++started : ++stopped;
    return true;
  }
  bool Ready(const RealtimeTranscriptionJob&) override {
    CheckThread();
    ++ready_calls;
    return ready;
  }
  bool AddText(const char* id) {
    RealtimeItemTicket ticket;
    return bool(history.Create(
        Object({{"type", String("message")},
                {"role", String("user")},
                {"content", Array({Object({{"type", String("input_text")},
                                           {"text", String(id)}})})}}),
        id, {}, &ticket));
  }
};
}  // namespace

Q4T_TEST(realtime_input_owner_manual_commit_joins_actual_conversation) {
  Fixture f;
  InputOwner owner;
  owner.ready = false;
  Q4T_CHECK(owner.AddText("prior_text"));
  f.callbacks.owner = &owner;
  int acquired = 0;
  f.callbacks.acquire = [&](const auto& id) {
    ++acquired;
    return f.requests.Register(id, "", Clock::now() + 1s);
  };
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start());
  Drain(f.output);
  Q4T_CHECK(Send(s, kAppend) && Send(s, kCommit));
  auto events = Drain(f.output);
  Q4T_CHECK(Events(events, "input_audio_buffer.committed") == 1);
  Q4T_CHECK(events[0].GetString("previous_item_id") == "prior_text");
  Q4T_CHECK(owner.reserved == 1 && owner.committed == 1 &&
            owner.automatic == std::vector<bool>{false});
  for (int i = 0; i < 3; ++i) Q4T_CHECK(s.Tick());
  Q4T_CHECK(acquired == 1 && f.calls == 0 && owner.ready_calls == 3);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(owner.history.Capture(nullptr, &snapshot) &&
            snapshot.pending_inputs.size() == 1);
  owner.ready = true;
  Q4T_CHECK(Finish(s, f.output, &events));
  bool ready = false;
  Q4T_CHECK(owner.history.Resolve(&snapshot, &ready) && ready);
  Q4T_CHECK(snapshot.items.array.size() == 2 && owner.finished == 1 &&
            owner.discarded == 0 && f.calls == 1 && f.released == 1);
  Q4T_CHECK(snapshot.items.array[1].GetArray("content")->array[0].GetString(
                "text") == "real executor fixture");
  Q4T_CHECK(owner.correct_thread && s.AudioBytes() == 0);
  return true;
}

Q4T_TEST(realtime_input_owner_reservation_failure_preserves_uncommitted_audio) {
  Fixture f;
  InputOwner owner;
  owner.reject_reserve = true;
  f.callbacks.owner = &owner;
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start());
  Drain(f.output);
  Q4T_CHECK(Send(s, kAppend));
  const auto bytes = s.AudioBytes();
  Q4T_CHECK(Send(s, kCommit));
  auto events = Drain(f.output);
  Q4T_CHECK(Events(events, "error") == 1 &&
            Events(events, "input_audio_buffer.committed") == 0);
  Q4T_CHECK(s.AudioBytes() == bytes && s.QueuedItems() == 0 &&
            s.RetainedAudioBytes() == 0 && owner.history.Items() == 0);
  Q4T_CHECK(owner.discarded == 1 && owner.committed == 0 && f.calls == 0);
  owner.reject_reserve = false;
  Q4T_CHECK(Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK(owner.committed == 1 && owner.finished == 1 && f.calls == 1 &&
            owner.correct_thread);
  return true;
}

Q4T_TEST(realtime_input_owner_unknown_commit_cannot_publish_or_consume) {
  Fixture f;
  InputOwner owner;
  owner.unknown_commit = true;
  f.callbacks.owner = &owner;
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start());
  Drain(f.output);
  Q4T_CHECK(Send(s, kAppend));
  const auto bytes = s.AudioBytes();
  Q4T_CHECK(Send(s, kCommit));
  auto events = Drain(f.output);
  Q4T_CHECK(events.size() == 1 && events[0].GetString("type") == "error");
  Q4T_CHECK(events[0].Find("error")->GetString("code") ==
            "transcription_owner_unknown");
  Q4T_CHECK(owner.history.Items() == 0 && owner.discarded == 1 &&
            s.AudioBytes() == bytes && s.RetainedAudioBytes() == 0);
  owner.unknown_commit = false;
  Q4T_CHECK(Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK(f.calls == 1 && owner.finished == 1 && owner.correct_thread);
  return true;
}

Q4T_TEST(realtime_input_owner_waiting_admission_keeps_original_deadline) {
  Fixture f;
  InputOwner owner;
  owner.ready = false;
  f.callbacks.owner = &owner;
  int acquired = 0;
  f.callbacks.acquire = [&](const auto& id) {
    ++acquired;
    return f.requests.Register(id, "", Clock::now() - 1ms);
  };
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kAppend) && Send(s, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(acquired == 1 && f.calls == 0 && f.released == 1 &&
            owner.finished == 1 && owner.ready_calls == 0);
  Q4T_CHECK(events.back().Find("error")->GetString("code") ==
            "deadline_exceeded");
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty() && owner.history.Items() == 1);
  return true;
}

Q4T_TEST(realtime_input_owner_unknown_asr_result_never_completes_input) {
  Fixture f;
  InputOwner owner;
  f.callbacks.owner = &owner;
  f.callbacks.execute = [&](const auto&, const auto&, const auto&,
                            const auto&) {
    ++f.calls;
    auto result = Completed("unconfirmed text");
    result.commit_unknown = true;
    return result;
  };
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kAppend) && Send(s, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(events.back().Find("error")->GetString("code") ==
            "transcription_result_unknown");
  Q4T_CHECK(owner.finished == 1 && owner.correct_thread && f.calls == 1);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  return true;
}

Q4T_TEST(realtime_input_owner_continuous_commit_keeps_worker_snapshot) {
  Fixture f;
  InputOwner owner;
  f.callbacks.owner = &owner;
  std::string worker_previous;
  f.callbacks.execute = [&](const auto& job, const auto&, const auto& control,
                            const auto& emit) {
    ++f.calls;
    auto result = StreamFixture(job, control, emit);
    worker_previous = job.previous_id;
    return result;
  };
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Until(s, f.output, &events, [&] {
    return Events(events,
                  "conversation.item.input_audio_transcription.delta") == 1;
  }));
  Q4T_CHECK(owner.reserved == 1 && owner.committed == 0 &&
            owner.history.LastId().empty());
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  Q4T_CHECK(owner.AddText("text_during_listening"));
  Q4T_CHECK(Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK(worker_previous.empty());
  for (const auto& e : events)
    if (e.GetString("type") == "input_audio_buffer.committed")
      Q4T_CHECK(e.GetString("previous_item_id") == "text_during_listening");
  Q4T_CHECK(owner.automatic == std::vector<bool>{false} &&
            owner.finished == 1 && owner.discarded == 0 &&
            owner.correct_thread);
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.size() == 2);
  return true;
}

Q4T_TEST(realtime_input_owner_clear_keeps_borrow_until_actual_join) {
  Fixture f;
  InputOwner owner;
  f.callbacks.owner = &owner;
  std::atomic<bool> borrowed{false}, release{false}, intact{false};
  f.callbacks.execute = [&](const auto& job, const auto&, const auto& control,
                            const auto&) {
    ++f.calls;
    const auto read = job.stream->Read(control->deadline());
    if (read.kind != AsrStreamReadKind::kReady)
      return Error(500, "fixture_read", "Borrow unavailable");
    ScopeExit finish([&] { job.stream->Complete(read.step); });
    borrowed = true;
    while (!release && Clock::now() < control->deadline())
      std::this_thread::sleep_for(1ms);
    intact = read.samples[100] == .25f;
    return Error(409, "fixture_cancelled", "Fixture cleared");
  };
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  ScopeExit unlock([&] { release = true; });
  Q4T_CHECK(s.Start() && Send(s, kContinuous) && Pcm(s, 13000));
  std::vector<Json> events;
  Q4T_CHECK(Until(s, f.output, &events, [&] { return borrowed.load(); }));
  Q4T_CHECK(Send(s, kClear));
  Q4T_CHECK(owner.history.Items() == 1 && owner.committed == 0 &&
            s.AudioBytes() > 0 && owner.finished == 0);
  release = true;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(intact && owner.history.Items() == 0 && owner.finished == 1 &&
            s.AudioBytes() == 0 && s.RetainedAudioBytes() == 0);
  return true;
}

Q4T_TEST(realtime_input_owner_vad_speech_precedes_execution_gate) {
  Fixture f;
  InputOwner owner;
  owner.ready = false;
  f.callbacks.owner = &owner;
  size_t frames = 0;
  f.callbacks.vad_step = [&](auto, auto*, float* p) {
    *p = frames == 1 || frames == 2 ? 1 : 0;
    ++frames;
    return q4t::Status{};
  };
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kServerVad) && Audio(s, 6 * 768 + 48));
  std::vector<Json> events;
  Q4T_CHECK(Until(s, f.output, &events, [&] { return owner.committed == 1; }));
  Q4T_CHECK(owner.started == 1 && owner.stopped == 1 && f.calls == 0);
  Q4T_CHECK(owner.automatic == std::vector<bool>{true});
  owner.ready = true;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(owner.finished == 1 && f.calls == 1 && owner.correct_thread);
  return true;
}

Q4T_TEST(realtime_input_owner_semantic_reserves_before_public_commit) {
  SemanticFixture f;
  InputOwner owner;
  f.callbacks.owner = &owner;
  f.allow_score = false;
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  ScopeExit unlock([&] { f.allow_score = true; });
  Q4T_CHECK(s.Start() && Send(s, kSemanticVad) &&
            Raw(s, Signal(30 * 768), f.output));
  std::vector<Json> events;
  Q4T_CHECK(AwaitScore(s, f, &events));
  Q4T_CHECK(owner.reserved == 1 && owner.committed == 0 && owner.started == 1);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  f.allow_score = true;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(owner.automatic == std::vector<bool>{true} && owner.stopped == 1 &&
            owner.finished == 1 && owner.correct_thread);
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_input_owner_delete_waiting_item_releases_both_ledgers) {
  Fixture f;
  InputOwner owner;
  owner.ready = false;
  f.callbacks.owner = &owner;
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kAppend) && Send(s, kCommit));
  auto events = Drain(f.output);
  const auto id = CommitId(events);
  Q4T_CHECK(s.Tick() && f.calls == 0);
  Q4T_CHECK(Delete(s, id));
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(owner.discarded == 1 && owner.finished == 0 &&
            owner.history.Items() == 0 && f.calls == 0 && f.released == 1);
  Q4T_CHECK(s.AudioBytes() == 0 && s.RetainedAudioBytes() == 0 &&
            owner.correct_thread);
  return true;
}

Q4T_TEST(realtime_input_owner_settlement_failure_is_honest_and_isolated) {
  Fixture f;
  InputOwner owner;
  owner.reject_finish = true;
  f.callbacks.owner = &owner;
  RealtimeTranscriptionSession s("sess_input", f.output, f.callbacks, 30);
  Q4T_CHECK(s.Start() && Send(s, kAppend) && Send(s, kCommit));
  std::vector<Json> events;
  Q4T_CHECK(Finish(s, f.output, &events));
  Q4T_CHECK(events.back().Find("error")->GetString("code") ==
            "fixture_retention");
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  owner.reject_finish = false;
  Q4T_CHECK(owner.AddText("healthy_text") && Send(s, kAppend) &&
            Send(s, kCommit) && Finish(s, f.output, &events));
  Q4T_CHECK(owner.finished == 2 && f.calls == 2 && f.released == 2 &&
            owner.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.size() == 2);
  return true;
}
