#include "q4t/server/realtime_conversation_input.h"

#include <algorithm>
#include <chrono>
#include <thread>

#include "q4t/io/base64.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"
#include "../src/server/object_protocol.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr auto kAppend =
    R"({"type":"input_audio_buffer.append","audio":"AQACAA=="})";
constexpr auto kCommit = R"({"type":"input_audio_buffer.commit"})";
constexpr auto kClear = R"({"type":"input_audio_buffer.clear"})";
ObjectReply Complete(std::string text = "actual CPU audio fixture") {
  ObjectReply result;
  result.value = Object({{"text", String(std::move(text))},
                         {"usage", Object({{"type", String("tokens")},
                                           {"input_tokens", Number(23)},
                                           {"output_tokens", Number(7)},
                                           {"total_tokens", Number(30)}})}});
  return result;
}
struct Fixture {
  RealtimeConversation history{"sess_history"};
  RequestRegistry requests;
  RealtimeTranscriptionCallbacks native;
  RealtimeConversationInputCallbacks callbacks;
  std::vector<Json> events;
  std::vector<RealtimeInputCommit> commits;
  std::atomic<int> calls{0}, released{0};
  bool ready = true, allow_emit = true, correct_thread = true;
  int speech_start = 0, speech_stop = 0, interruptions = 0;
  const std::thread::id thread = std::this_thread::get_id();
  Fixture() {
    native.validate = [](const auto&) { return ObjectReply{}; };
    native.acquire = [&](const auto& id) {
      return requests.Register(id, "", Clock::now() + 1s);
    };
    native.release = [&](const auto& control) {
      requests.Remove(control);
      ++released;
    };
    native.execute = [&](const auto&, const auto&, const auto&,
                         const auto& emit) {
      ++calls;
      if (!emit(Object({{"delta", String("actual CPU audio fixture")}})))
        return Error(409, "fixture_cancelled", "Fixture cancelled");
      return Complete();
    };
    callbacks.ready = [&] { return ready; };
    callbacks.emit = [&](const auto& event) {
      correct_thread &= std::this_thread::get_id() == thread;
      if (!allow_emit) return Error(503, "fixture_output_full", "Output full");
      events.push_back(event);
      return ObjectReply{};
    };
    callbacks.speech = [&](bool started, auto, auto, bool interrupt) {
      correct_thread &= std::this_thread::get_id() == thread;
      started ? ++speech_start : ++speech_stop;
      interruptions += interrupt;
      return true;
    };
    callbacks.committed = [&](RealtimeInputCommit notice) {
      correct_thread &= std::this_thread::get_id() == thread;
      commits.push_back(std::move(notice));
      return ObjectReply{};
    };
  }
  size_t Count(std::string_view type) const {
    return std::ranges::count_if(events, [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
};
RealtimeDuplexInput Options(bool public_transcript = true) {
  RealtimeDuplexInput options;
  options.enabled = true;
  options.transcript_events = public_transcript;
  return options;
}
bool Finish(RealtimeConversationInput& input) {
  const auto end = Clock::now() + 1s;
  while (Clock::now() < end) {
    if (!input.Tick()) return false;
    if (!input.PendingItems()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
bool Audio(RealtimeConversationInput& input, size_t samples) {
  const std::string raw(samples * 2, '\0');
  std::string encoded;
  return q4t::io::EncodeBase64(raw, kWebSocketMessageLimit, &encoded) &&
         input.Message(
             OutputJson(Object({{"type", String("input_audio_buffer.append")},
                                {"audio", String(encoded)}})));
}
bool Committed(RealtimeConversationInput& input, const Fixture& fixture) {
  const auto end = Clock::now() + 1s;
  while (fixture.commits.empty() && Clock::now() < end) {
    if (!input.Tick()) return false;
    std::this_thread::sleep_for(1ms);
  }
  return fixture.commits.size() == 1;
}
bool AddText(RealtimeConversation& history, const char* id) {
  RealtimeItemTicket ticket;
  return bool(history.Create(
      Object({{"type", String("message")},
              {"role", String("user")},
              {"content", Array({Object({{"type", String("input_text")},
                                         {"text", String(id)}})})}}),
      id, {}, &ticket));
}
}  // namespace

Q4T_TEST(realtime_composed_input_emits_ga_items_without_duplicate_session) {
  Fixture f;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options()).ok() && input.Tick());
  Q4T_CHECK(f.events.empty());
  Q4T_CHECK(AddText(f.history, "prior"));
  Q4T_CHECK(input.Message(kAppend) && input.Message(kCommit) && Finish(input));
  Q4T_CHECK(f.Count("input_audio_buffer.committed") == 1 &&
            f.Count("conversation.item.added") == 1 &&
            f.Count("conversation.item.done") == 1 &&
            f.Count("conversation.item.created") == 0);
  Q4T_CHECK(f.Count("session.created") == 0 && f.Count("session.updated") == 0);
  Q4T_CHECK(f.commits.size() == 1 && !f.commits[0].automatic &&
            !f.commits[0].settings.RespondOnCommit(false));
  for (const auto& event : f.events) {
    ChatPlanError error;
    Q4T_CHECK(
        ValidateRealtimeShape(event, RealtimeShape::kServerEvent, &error));
    if (event.Has("previous_item_id"))
      Q4T_CHECK(event.GetString("previous_item_id") == "prior");
  }
  Q4T_CHECK(f.calls == 1 && f.released == 1 && f.correct_thread);
  return true;
}

Q4T_TEST(realtime_composed_input_private_transcript_still_drives_context) {
  Fixture f;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options(false)).ok());
  Q4T_CHECK(input.Message(kAppend) && input.Message(kCommit) && Finish(input));
  for (const auto& event : f.events)
    Q4T_CHECK(!event.GetString("type").starts_with(
        "conversation.item.input_audio_transcription."));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "actual CPU audio fixture");
  Json item;
  Q4T_CHECK(input.Retrieve(f.commits[0].ticket, &item).ok());
  const auto& part = item.GetArray("content")->array[0];
  Q4T_CHECK(!part.Has("transcript") && !part.GetString("audio").empty());
  Q4T_CHECK(f.calls == 1 && input.AudioBytes() == 0);
  return true;
}

Q4T_TEST(realtime_composed_input_failure_keeps_healthy_text_and_honest_error) {
  Fixture f;
  f.native.execute = [&](const auto&, const auto&, const auto&, const auto&) {
    ++f.calls;
    return Error(503, "asr_unavailable", "ASR fixture unavailable");
  };
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options(false)).ok() && input.Message(kAppend) &&
            input.Message(kCommit) && Finish(input));
  Q4T_CHECK(f.Count("error") == 1 && f.events.back().Find("error")->GetString(
                                         "code") == "asr_unavailable");
  Q4T_CHECK(!input.Failed() && AddText(f.history, "healthy_text"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.size() == 1 && f.history.Items() == 2);
  Q4T_CHECK(snapshot.items.array[0].GetString("id") == "healthy_text");
  Q4T_CHECK(f.calls == 1 && f.released == 1);
  return true;
}

Q4T_TEST(realtime_composed_input_delete_waiter_preserves_generation_boundary) {
  Fixture f;
  f.ready = false;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options()).ok() && input.Message(kAppend) &&
            input.Message(kCommit) && input.Tick());
  Q4T_CHECK(f.commits.size() == 1 && f.calls == 0);
  const auto ticket = f.commits[0].ticket;
  RealtimeConversationSnapshot frozen;
  Q4T_CHECK(f.history.Capture(nullptr, &frozen));
  Q4T_CHECK(input.Owns(ticket) && input.Erase(ticket).ok());
  Q4T_CHECK(Finish(input) && !input.Owns(ticket));
  Q4T_CHECK(f.Count("conversation.item.deleted") == 0);
  bool ready = true;
  Q4T_CHECK(!f.history.Resolve(&frozen, &ready) && !ready);
  Json kept = String("unchanged");
  Q4T_CHECK(!input.Retrieve(ticket, &kept).ok() && kept.str == "unchanged");
  Q4T_CHECK(input.RetainedAudioBytes() == 0 && input.AudioBytes() == 0 &&
            f.released == 1 && f.calls == 0);
  return true;
}

Q4T_TEST(realtime_composed_input_settings_do_not_rewrite_an_accepted_turn) {
  Fixture f;
  f.ready = false;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options(false)).ok() && input.Message(kAppend) &&
            input.Message(kCommit));
  auto next = Options(true);
  Q4T_CHECK(input.Configure(next).ok());
  f.ready = true;
  Q4T_CHECK(Finish(input));
  Q4T_CHECK(f.Count("conversation.item.input_audio_transcription.completed") ==
            0);
  Q4T_CHECK(input.Message(kAppend) && input.Message(kCommit) && Finish(input));
  Q4T_CHECK(f.Count("conversation.item.input_audio_transcription.completed") ==
                1 &&
            f.calls == 2);
  Q4T_CHECK(!f.commits[0].settings.transcript_events &&
            f.commits[1].settings.transcript_events);
  return true;
}

Q4T_TEST(realtime_composed_input_vad_freezes_context_at_commit) {
  Fixture f;
  f.ready = false;
  size_t frames = 0;
  f.native.vad_step = [&](auto, auto*, float* probability) {
    *probability = frames == 1 || frames == 2 ? 1 : 0;
    ++frames;
    return q4t::Status{};
  };
  auto settings = Options(false);
  settings.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  settings.processing.request.chunking.threshold = .5;
  settings.processing.request.chunking.prefix_padding_ms = 16;
  settings.processing.request.chunking.silence_duration_ms = 32;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(AddText(f.history, "original"));
  Q4T_CHECK(input.Configure(settings).ok() && Audio(input, 6 * 768 + 48));
  const auto end = Clock::now() + 1s;
  while (f.commits.empty() && Clock::now() < end) Q4T_CHECK(input.Tick());
  Q4T_CHECK(f.commits.size() == 1 && f.calls == 0 && f.commits[0].automatic &&
            f.commits[0].context_status.ok());
  Q4T_CHECK(f.speech_start == 1 && f.speech_stop == 1 && f.interruptions == 1);
  Q4T_CHECK(AddText(f.history, "future"));
  f.ready = true;
  Q4T_CHECK(Finish(input));
  auto frozen = std::move(f.commits[0].context);
  bool ready = false;
  Q4T_CHECK(f.history.Resolve(&frozen, &ready) && ready);
  Q4T_CHECK(frozen.items.array.size() == 2 &&
            frozen.items.array[0].GetString("id") == "original");
  return true;
}

Q4T_TEST(realtime_composed_input_fatal_processor_does_not_poison_text) {
  Fixture f;
  int vad_calls = 0;
  f.native.vad_step = [&](auto, auto*, float*) {
    ++vad_calls;
    return q4t::Status::Fail("CPU VAD fixture failed");
  };
  auto settings = Options();
  settings.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(settings).ok() && Audio(input, 768));
  // A 24kHz frame needs the resampler's actual lookahead before the native
  // 16kHz processor is invoked. First prove the fault has not yet run.
  Q4T_CHECK(input.Tick() && !input.Failed() && vad_calls == 0);
  Q4T_CHECK(Audio(input, 48));
  Q4T_CHECK(input.Tick() && input.Failed() && vad_calls == 1);
  Q4T_CHECK(f.Count("error") >= 1 && f.calls == 0);
  Q4T_CHECK(input.Message(kClear) && AddText(f.history, "healthy_text"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.size() == 1);
  settings.enabled = false;
  Q4T_CHECK(input.Configure(settings).ok());
  Q4T_CHECK(f.calls == 0 && f.correct_thread);
  return true;
}

Q4T_TEST(realtime_composed_input_captures_prompt_before_public_commit) {
  Fixture f;
  int frames = 0, captures = 0;
  std::string setting = "at commit";
  f.native.vad_step = [&](auto, auto*, float* probability) {
    *probability = ++frames <= 3 ? .9f : 0.f;
    return q4t::Status{};
  };
  f.callbacks.capture_response = [&](RealtimePromptSelection* selected) {
    ++captures;
    selected->settings = Object({{"instructions", String(setting)}});
    selected->prompt = PromptSnapshot{PromptApi::kRealtime, "client:fixture",
                                      Object({{"version", String(setting)}}),
                                      Object({{"input", Array()}})};
    return ObjectReply{};
  };
  const auto emit = f.callbacks.emit;
  f.callbacks.emit = [&](const Json& event) {
    if (event.GetString("type") == "input_audio_buffer.committed")
      setting = "after commit";
    return emit(event);
  };
  auto settings = Options();
  settings.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  settings.processing.request.chunking.silence_duration_ms = 32;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(settings).ok() && Audio(input, 6 * 768 + 48) &&
            Committed(input, f) && Finish(input));
  Q4T_CHECK(captures == 1 && setting == "after commit" &&
            f.commits.size() == 1);
  const auto& response = *f.commits[0].response;
  Q4T_CHECK(response.settings.GetString("instructions") == "at commit" &&
            response.prompt->reference.GetString("version") == "at commit");
  return true;
}

Q4T_TEST(realtime_composed_input_bad_capture_keeps_owned_input_without_retry) {
  for (bool oversized : {false, true}) {
    Fixture f;
    int frames = 0, captures = 0;
    f.native.vad_step = [&](auto, auto*, float* probability) {
      *probability = ++frames <= 3 ? .9f : 0.f;
      return q4t::Status{};
    };
    f.callbacks.capture_response = [&](RealtimePromptSelection* selected) {
      ++captures;
      ObjectReply reply;
      if (oversized) {
        selected->settings = Object({{"instructions", String("bounded")}});
        // Actual retained capacity, not JSON length, must hit the budget.
        Field(selected->settings, "instructions").str.reserve(97ULL << 20);
      } else {
        reply.commit_unknown = true;
      }
      return reply;
    };
    auto settings = Options();
    settings.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
    settings.processing.request.chunking.silence_duration_ms = 32;
    RealtimeConversationInput input("sess_asr", f.history, f.native,
                                    f.callbacks, 30);
    Q4T_CHECK(input.Configure(settings).ok() && Audio(input, 6 * 768 + 48) &&
              Committed(input, f) && Finish(input));
    Q4T_CHECK(captures == 1 && f.commits.size() == 1 && f.calls == 1);
    const auto& notice = f.commits[0];
    Q4T_CHECK(
        (notice.context_status.commit_unknown || !notice.context_status.ok()) &&
        !notice.response);
    RealtimeConversationSnapshot snapshot;
    Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot) &&
              snapshot.items.array.size() == 1);
    Q4T_CHECK(input.Tick() && captures == 1);
  }
  return true;
}

Q4T_TEST(realtime_composed_input_recovery_preserves_audio_context_and_clock) {
  Fixture f;
  auto now = Clock::now();
  f.callbacks.now = [&] { return now; };
  bool fault = true;
  std::vector<std::string> contexts;
  const auto execute = f.native.execute;
  f.native.execute = [&, execute](const auto& job, const auto& bytes,
                                  const auto& control, const auto& emit) {
    contexts.push_back(job.settings.request.previous_transcript);
    return execute(job, bytes, control, emit);
  };
  f.native.vad_step = [&](auto, auto*, float* probability) {
    *probability = 0;
    return fault ? q4t::Status::Fail("Injected input fault") : q4t::Status{};
  };
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options()).ok() && Audio(input, 24) &&
            input.Message(kCommit) && Finish(input));
  const auto original = f.commits.at(0).ticket;
  Json audio_before;
  Q4T_CHECK(input.Retrieve(original, &audio_before).ok());
  auto vad = Options();
  vad.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  Q4T_CHECK(input.Configure(vad).ok() && Audio(input, 768 + 48));
  Q4T_CHECK(input.Tick() && input.Failed());
  const auto ticks = input.AcceptedTicks(), bytes = input.RetainedAudioBytes();
  Q4T_CHECK(ticks == (24 + 768 + 48) * 2 && f.calls == 1);
  fault = false;
  now += 4999ms;
  Q4T_CHECK(input.Tick() && input.Failed() && f.calls == 1);
  now += 1ms;
  Q4T_CHECK(input.Tick() && !input.Failed() && f.calls == 1 &&
            input.AcceptedTicks() == ticks &&
            input.RetainedAudioBytes() == bytes);
  Json audio_after;
  Q4T_CHECK(input.Retrieve(original, &audio_after).ok() &&
            OutputJson(audio_after) == OutputJson(audio_before));
  // New input starts only when new bytes arrive; no ASR call was replayed.
  Q4T_CHECK(input.Configure(Options()).ok() && Audio(input, 24) &&
            input.Message(kCommit) && Finish(input));
  Q4T_CHECK(f.calls == 2 && f.commits.size() == 2 &&
            f.commits[1].ticket.generation > original.generation &&
            input.AcceptedTicks() == ticks + 48);
  Q4T_CHECK(contexts.size() == 2 && contexts[0].empty() &&
            contexts[1] == "actual CPU audio fixture" &&
            input.RetainedAudioBytes() == 2 * bytes);
  Q4T_CHECK(f.Count("session.created") == 0 && f.Count("session.updated") == 0);
  return true;
}

Q4T_TEST(realtime_composed_input_recovery_is_two_attempts_per_minute) {
  Fixture f;
  auto now = Clock::now();
  f.callbacks.now = [&] { return now; };
  bool unavailable = false;
  int validations = 0;
  f.native.validate = [&](const auto&) {
    ++validations;
    ObjectReply reply;
    reply.commit_unknown = unavailable;
    return reply;
  };
  f.native.vad_step = [](auto, auto*, float*) {
    return q4t::Status::Fail("Injected input fault");
  };
  auto vad = Options();
  vad.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(vad).ok() && Audio(input, 768 + 48) &&
            input.Tick());
  Q4T_CHECK(input.Failed());
  unavailable = true;
  const auto baseline = validations;
  now += 5s;
  Q4T_CHECK(input.Tick() && input.Failed() && validations == baseline + 1);
  for (size_t n = 0; n < 100; ++n) Q4T_CHECK(input.Tick());
  Q4T_CHECK(validations == baseline + 1);
  now += 5s;
  Q4T_CHECK(input.Tick() && input.Failed() && validations == baseline + 2);
  now += 5s;
  Q4T_CHECK(input.Tick() && input.Failed() && validations == baseline + 2);
  Q4T_CHECK(input.Configure(vad).code == "realtime_input_cooldown" &&
            validations == baseline + 2);
  now += 50s;  // 60 seconds from the first actual recovery attempt.
  unavailable = false;
  Q4T_CHECK(input.Tick() && !input.Failed() && validations == baseline + 4 &&
            f.calls == 0);
  return true;
}

Q4T_TEST(realtime_composed_input_disconnect_never_recovers_or_replays) {
  Fixture f;
  auto now = Clock::now();
  f.callbacks.now = [&] { return now; };
  int validations = 0;
  f.native.validate = [&](const auto&) {
    ++validations;
    return ObjectReply{};
  };
  f.native.vad_step = [](auto, auto*, float*) {
    return q4t::Status::Fail("Injected input fault");
  };
  auto vad = Options();
  vad.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(vad).ok() && Audio(input, 768 + 48) &&
            input.Tick());
  Q4T_CHECK(input.Failed());
  const auto baseline = validations;
  input.Cancel();
  input.Join();
  now += 60s;
  Q4T_CHECK(!input.Tick() && !input.Configure(vad).ok() &&
            validations == baseline && f.calls == 0);
  return true;
}

Q4T_TEST(realtime_composed_input_rejects_pipeline_changes_while_borrowed) {
  Fixture f;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options()).ok() && input.Message(kAppend));
  auto off = Options();
  off.enabled = false;
  Q4T_CHECK(!input.Configure(off).ok());
  auto changed = Options();
  changed.processing.format = {RealtimeAudioEncoding::kMuLaw, 8000};
  Q4T_CHECK(!input.Configure(changed).ok());
  Q4T_CHECK(input.Message(kClear) && input.Configure(changed).ok());
  Q4T_CHECK(f.Count("session.updated") == 0 && f.calls == 0);
  return true;
}

Q4T_TEST(realtime_composed_input_unconfirmed_schedule_is_not_replayed) {
  Fixture f;
  int scheduled = 0;
  f.callbacks.committed = [&](RealtimeInputCommit) {
    ++scheduled;
    ObjectReply result;
    result.commit_unknown = true;
    return result;
  };
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options()).ok() && input.Message(kAppend) &&
            input.Message(kCommit) && Finish(input));
  Q4T_CHECK(scheduled == 1 && f.Count("error") == 1 && f.calls == 1);
  Q4T_CHECK(f.Count("conversation.item.input_audio_transcription.completed") ==
            1);
  Q4T_CHECK(input.Tick() && input.Tick() && scheduled == 1 && f.calls == 1);
  return true;
}

Q4T_TEST(realtime_composed_input_stopped_transport_keeps_rollback_owned) {
  Fixture f;
  f.ready = false;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(Options()).ok() && input.Message(kAppend));
  f.allow_emit = false;
  Q4T_CHECK(!input.Message(kCommit));
  input.Cancel();
  input.Join();
  Q4T_CHECK(f.calls == 0);
  Q4T_CHECK(!input.Tick() && !input.Message(kAppend));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  return true;
}

namespace {
RealtimeDuplexInput IdleOptions() {
  auto options = Options();
  options.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  options.processing.request.chunking.threshold = .5;
  options.idle_timeout_ms = 5000;
  return options;
}
void SilentVad(Fixture& f) {
  f.native.vad_step = [](auto, auto*, float* probability) {
    *probability = 0;
    return q4t::Status{};
  };
}
}  // namespace

Q4T_TEST(realtime_input_idle_waits_playback_then_emits_one_empty_owned_turn) {
  Fixture f;
  auto now = Clock::now();
  f.callbacks.now = [&] { return now; };
  SilentVad(f);
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(IdleOptions()).ok());
  now += 60s;
  Q4T_CHECK(input.Tick() && f.events.empty());  // No prior response.
  input.ResponseFinished(2 * kRealtimeTicksPerSecond);
  now += 6999ms;
  Q4T_CHECK(input.Tick() && f.events.empty());
  now += 1ms;
  Q4T_CHECK(input.Tick() && Finish(input));
  Q4T_CHECK(f.Count("input_audio_buffer.timeout_triggered") == 1 &&
            f.Count("input_audio_buffer.committed") == 1 &&
            f.Count("conversation.item.done") == 1 && f.calls == 0 &&
            f.released == 0 && input.AcceptedTicks() == 0);
  Q4T_CHECK(f.events[0].GetString("type") ==
                "input_audio_buffer.timeout_triggered" &&
            f.events[0].GetInt("audio_start_ms") == 0 &&
            f.events[0].GetInt("audio_end_ms") == 0 &&
            f.events[0].GetString("item_id") == f.commits[0].ticket.id);
  Q4T_CHECK(f.commits[0].automatic &&
            f.commits[0].settings.RespondOnCommit(true));
  for (const auto& event : f.events) {
    ChatPlanError error;
    Q4T_CHECK(
        ValidateRealtimeWireShape(event, RealtimeShape::kServerEvent, &error));
    Q4T_CHECK(!event.GetString("type").starts_with(
        "conversation.item.input_audio_transcription."));
  }
  Json item;
  Q4T_CHECK(input.Retrieve(f.commits[0].ticket, &item).ok() &&
            item.GetArray("content")->array[0].GetString("audio").empty());
  now += 60s;
  Q4T_CHECK(input.Tick() && f.commits.size() == 1 &&
            input.Erase(f.commits[0].ticket).ok());
  return true;
}

Q4T_TEST(realtime_input_idle_retains_actual_silent_audio_for_native_asr) {
  Fixture f;
  auto now = Clock::now();
  f.callbacks.now = [&] { return now; };
  SilentVad(f);
  std::atomic<bool> actual_audio{false};
  f.native.execute = [&](const auto& job, const auto& bytes, const auto&,
                         const auto&) {
    ++f.calls;
    actual_audio = job.idle_timeout && job.audio.samples == 48000 &&
                   bytes[0].size() + bytes[1].size() == 96000 &&
                   job.processed_audio->Samples().size() == 32000;
    return Complete("quiet speech missed by the CPU VAD fixture");
  };
  auto options = IdleOptions();
  options.transcript_events = false;
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(options).ok());
  input.ResponseFinished(0);
  Q4T_CHECK(input.Tick() && Audio(input, 48000));
  for (int i = 0; i < 180; ++i) Q4T_CHECK(input.Tick());
  Q4T_CHECK(f.calls == 0 && input.AudioBytes() == 96000);
  now += 5000ms;
  Q4T_CHECK(input.Tick() && Finish(input));
  Q4T_CHECK(actual_audio && f.calls == 1 && f.released == 1 &&
            f.Count("input_audio_buffer.timeout_triggered") == 1);
  Q4T_CHECK(f.events[0].GetInt("audio_start_ms") == 0 &&
            f.events[0].GetInt("audio_end_ms") == 2000);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "quiet speech missed by the CPU VAD fixture");
  Q4T_CHECK(f.Count("conversation.item.input_audio_transcription.completed") ==
            0);
  return true;
}

Q4T_TEST(realtime_input_idle_controls_and_null_timeout_do_not_auto_respond) {
  for (bool disabled : {false, true}) {
    Fixture f;
    auto now = Clock::now();
    f.callbacks.now = [&] { return now; };
    SilentVad(f);
    auto options = IdleOptions();
    options.create_response = options.interrupt_response = false;
    RealtimeConversationInput input("sess_asr", f.history, f.native,
                                    f.callbacks, 30);
    Q4T_CHECK(input.Configure(options).ok());
    input.ResponseFinished(0);
    if (disabled) {
      options.idle_timeout_ms.reset();
      Q4T_CHECK(input.Configure(options).ok());
    }
    now += 5s;
    Q4T_CHECK(input.Tick() && Finish(input));
    Q4T_CHECK(f.commits.size() == (disabled ? 0 : 1));
    if (!disabled) Q4T_CHECK(!f.commits[0].settings.RespondOnCommit(true));
    Q4T_CHECK(f.calls == 0 && f.interruptions == 0);
  }
  return true;
}

Q4T_TEST(realtime_input_idle_clear_fault_and_disconnect_never_replay_timer) {
  for (int mode = 0; mode < 3; ++mode) {
    Fixture f;
    auto now = Clock::now();
    f.callbacks.now = [&] { return now; };
    bool fault = mode == 1;
    f.native.vad_step = [&](auto, auto*, float* probability) {
      *probability = 0;
      return fault ? q4t::Status::Fail("Idle VAD fixture fault")
                   : q4t::Status{};
    };
    RealtimeConversationInput input("sess_asr", f.history, f.native,
                                    f.callbacks, 30);
    Q4T_CHECK(input.Configure(IdleOptions()).ok());
    input.ResponseFinished(0);
    Q4T_CHECK(input.Tick());
    if (mode == 0) {
      Q4T_CHECK(input.Message(kClear));
    } else if (mode == 1) {
      Q4T_CHECK(Audio(input, 768 + 48));
      for (int i = 0; i < 10 && !input.Failed(); ++i) Q4T_CHECK(input.Tick());
      Q4T_CHECK(input.Failed());
      fault = false;
    } else {
      input.Cancel();
      input.Join();
    }
    now += 60s;
    Q4T_CHECK(input.Tick() == (mode != 2));
    if (mode == 1) Q4T_CHECK(!input.Failed() && input.Tick());
    Q4T_CHECK(f.Count("input_audio_buffer.timeout_triggered") == 0 &&
              f.calls == 0);
  }
  return true;
}

Q4T_TEST(realtime_input_idle_drains_vad_backlog_before_timeout) {
  Fixture f;
  auto now = Clock::now();
  f.callbacks.now = [&] { return now; };
  size_t frames = 0;
  f.native.vad_step = [&](auto, auto*, float* probability) {
    *probability = ++frames > 8 ? .9f : 0.f;
    return q4t::Status{};
  };
  RealtimeConversationInput input("sess_asr", f.history, f.native, f.callbacks,
                                  30);
  Q4T_CHECK(input.Configure(IdleOptions()).ok());
  input.ResponseFinished(0);
  Q4T_CHECK(input.Tick() && Audio(input, 10 * 768 + 48));
  now += 5s;
  for (int i = 0; i < 40; ++i) Q4T_CHECK(input.Tick());
  Q4T_CHECK(frames >= 10 && f.speech_start == 1 &&
            f.Count("input_audio_buffer.timeout_triggered") == 0 &&
            f.calls == 0);
  return true;
}
