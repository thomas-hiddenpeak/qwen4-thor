#include "q4t/server/live_frontend.h"

#include <algorithm>

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Settings(std::string instructions = "old") {
  return Object({{"type", String("realtime")},
                 {"model", String("q4t-test")},
                 {"instructions", String(std::move(instructions))},
                 {"output_modalities", Array({String("audio")})}});
}
Json User(std::string text) {
  return Object(
      {{"type", String("message")},
       {"role", String("user")},
       {"content", Array({Object({{"type", String("input_text")},
                                  {"text", String(std::move(text))}})})}});
}
std::string Base64(std::string_view bytes) {
  std::string encoded;
  if (!q4t::io::EncodeBase64(bytes, kWebSocketMessageLimit, &encoded))
    throw std::runtime_error("CPU base64 failure");
  return encoded;
}
struct Fixture {
  LiveFrontendNative native;
  RequestRegistry requests;
  std::atomic<size_t> asr{0}, models{0}, model_released{0}, asr_released{0};
  std::atomic<bool> hold_model{false}, hold_asr{false};
  std::atomic<bool> model_alive{false}, asr_alive{false};
  std::atomic<bool> overlap{false}, early_release{false};
  bool fail_asr = false, fail_model = false, fail_completion = false;
  bool bad_prepare = false, bad_input = false, voiced = false;
  bool owner_thread = true;
  std::atomic<size_t> prepare_calls{0};
  size_t frames = 0, completions = 0, event_ids = 0;
  uint32_t rate = 24000;
  const std::thread::id owner = std::this_thread::get_id();
  std::vector<Json> events, outputs;
  std::vector<RealtimeConversationJob> snapshots;
  std::vector<RealtimeGenerationResult> results;
  std::unique_ptr<LiveFrontend> frontend;
  Fixture() {
    native.input.validate = [this](const auto&) {
      return bad_input ? Error(503, "fixture_input", "Input unavailable")
                       : ObjectReply{};
    };
    native.input.vad_step = [this](auto, auto*, float* probability) {
      *probability = voiced && (frames == 1 || frames == 2) ? 1.f : 0.f;
      ++frames;
      return q4t::Status{};
    };
    native.input.acquire = [this](const auto& id) {
      return requests.Register(id, "", Clock::now() + 3s);
    };
    native.input.release = [this](const auto& control) {
      if (asr_alive) early_release = true;
      owner_thread &= std::this_thread::get_id() == owner;
      requests.Remove(control);
      ++asr_released;
    };
    native.input.execute = [this](const auto&, const auto&, const auto& control,
                                  const auto& emit) {
      asr_alive = true;
      if (model_alive) overlap = true;
      ++asr;
      while (hold_asr && !control->Cancelled())
        std::this_thread::sleep_for(1ms);
      ObjectReply result;
      if (control->Cancelled()) {
        result = Error(409, control->Reason(), "ASR cancelled");
      } else if (fail_asr) {
        result = Error(503, "fixture_asr_failure", "ASR failed");
      } else if (!emit(Object({{"delta", String("read project")}}))) {
        result = Error(503, "fixture_asr_sink", "ASR output failed");
      } else {
        result.value =
            Object({{"text", String("read project")},
                    {"usage", Object({{"type", String("tokens")},
                                      {"input_tokens", Number(13)},
                                      {"output_tokens", Number(7)},
                                      {"total_tokens", Number(20)}})}});
      }
      asr_alive = false;
      return result;
    };
    native.prepare = [this](const auto& job) {
      // May run on the frontend worker. Count is observed after actual join.
      ++prepare_calls;
      return bad_prepare || job.settings.GetString("instructions") == "reject"
                 ? Error(400, "fixture_prepare", "Settings rejected")
                 : ObjectReply{};
    };
    native.acquire = [this](const auto& id) {
      return requests.Register(id, "", Clock::now() + 3s);
    };
    native.release = [this](const auto& control) {
      if (model_alive) early_release = true;
      owner_thread &= std::this_thread::get_id() == owner;
      requests.Remove(control);
      ++model_released;
    };
    native.execute = [this](const auto& job, const auto& control,
                            const auto& delta, const auto& speech) {
      model_alive = true;
      if (asr_alive) overlap = true;
      snapshots.push_back(job);
      ++models;
      while (hold_model && !control->Cancelled())
        std::this_thread::sleep_for(1ms);
      RealtimeGenerationResult result;
      RealtimeResponseUsage usage;
      usage.total = {11, 7};
      usage.input_text = 11;
      usage.output_text = 3;
      usage.output_audio = 4;
      result.usage = usage;
      if (control->Cancelled()) {
        result.reply = Error(409, control->Reason(), "Model cancelled");
      } else if (fail_model) {
        result.reply = Error(503, "fixture_model_failure", "Model failed");
      } else {
        ResponseDelta text;
        text.text = "answer";
        result.reply = delta(text);
        RealtimeSpeechEvent audio;
        audio.bytes = std::string(4800, '\0');
        if (result.reply.ok()) result.reply = speech(audio);
        audio = {};
        audio.kind = RealtimeSpeechEvent::Kind::kDone;
        audio.transcript = "answer";
        audio.receipt = {2400, 2400, 4800};
        if (result.reply.ok()) result.reply = speech(audio);
        result.message = Object(
            {{"role", String("assistant")}, {"content", String("answer")}});
      }
      model_alive = false;
      return result;
    };
    native.emit = [this](Json event) {
      owner_thread &= std::this_thread::get_id() == owner;
      if (event.GetString("type") != "session.output_audio.delta")
        Set(&event, "event_id",
            String("event_cpu_" + std::to_string(++event_ids)));
      ChatPlanError error;
      auto status = ValidateLiveShape(event, LiveShape::kServerEvent, &error);
      if (!status) return Error(500, "fixture_shape", error.param);
      events.push_back(std::move(event));
      return ObjectReply{};
    };
    native.completed = [this](const auto&, const auto& result,
                              const auto& output) {
      owner_thread &= std::this_thread::get_id() == owner;
      if (model_alive || model_released <= completions) early_release = true;
      ++completions;
      results.push_back(result);
      outputs.push_back(output);
      if (fail_completion) {
        auto reply = Error(503, "fixture_handoff", "Unknown handoff result");
        reply.commit_unknown = true;
        return reply;
      }
      return ObjectReply{};
    };
  }
  ObjectReply Start(const Json& initial = Array()) {
    frontend = std::make_unique<LiveFrontend>("live_cpu", native, 30);
    return frontend->Prepare(Settings(), initial,
                             {RealtimeAudioEncoding::kPcm16, rate});
  }
  bool Wait(const std::function<bool()>& predicate) {
    const auto deadline = Clock::now() + 2s;
    while (Clock::now() < deadline) {
      const auto reply = frontend->Tick();
      if (!reply.ok()) {
        std::fprintf(stderr, "Frontend tick: %s %s\n", reply.code.c_str(),
                     reply.ClientMessage().c_str());
        return false;
      }
      if (predicate()) return true;
      std::this_thread::sleep_for(1ms);
    }
    return false;
  }
  bool Tone() {
    frames = 0;
    voiced = true;
    return frontend->AppendAudio(Base64(std::string(rate, '\x15'))).ok();
  }
  size_t Count(std::string_view type) const {
    return std::count_if(events.begin(), events.end(), [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
};
}  // namespace

Q4T_TEST(live_frontend_composes_actual_input_history_and_speech_workers) {
  Fixture f;
  f.rate = 16000;
  Q4T_CHECK(f.Start().ok() && f.events.empty() && f.models == 0 && f.asr == 0);
  Q4T_CHECK(f.Tone() && f.Wait([&] { return f.completions == 1; }));
  Q4T_CHECK(f.asr == 1 && f.models == 1 && f.results[0].reply.ok());
  Q4T_CHECK(f.snapshots[0].native_input);
  Q4T_CHECK(f.snapshots[0].snapshot.pending_inputs.empty());
  Q4T_CHECK(f.snapshots[0].snapshot.items.array.size() == 1);
  Q4T_CHECK(OutputJson(f.snapshots[0].snapshot.items).find("read project") !=
            std::string::npos);
  Q4T_CHECK(f.Count("session.input_transcript.delta") == 1);
  Q4T_CHECK(f.Count("session.output_audio.delta") > 0);
  Q4T_CHECK(f.Count("session.output_transcript.delta") == 1);
  Q4T_CHECK(f.frontend->HistoryItems() == 2);
  Q4T_CHECK(f.frontend->QueuedBytes() == 0 && f.frontend->QueuedTurns() == 0);
  Q4T_CHECK(!f.overlap && !f.early_release && f.owner_thread);
  Q4T_CHECK(f.frontend->Close().ok() &&
            f.Wait([&] { return f.frontend->Drained(); }));
  return true;
}

Q4T_TEST(live_frontend_freezes_queued_settings_and_history) {
  Fixture f;
  Q4T_CHECK(f.Start(Array({User("original")})).ok());
  Q4T_CHECK(f.frontend->Respond("first").ok());
  Q4T_CHECK(f.frontend->Create(User("later")).ok());
  Q4T_CHECK(f.frontend->Settings(Settings("new")).ok());
  Q4T_CHECK(f.frontend->Respond("second").ok());
  Q4T_CHECK(f.Wait([&] { return f.completions == 2; }));
  Q4T_CHECK(f.snapshots[0].settings.GetString("instructions") == "old");
  Q4T_CHECK(f.snapshots[1].settings.GetString("instructions") == "new");
  Q4T_CHECK(f.snapshots[0].snapshot.items.array.size() == 1);
  Q4T_CHECK(f.snapshots[1].snapshot.items.array.size() == 2);
  Q4T_CHECK(OutputJson(f.snapshots[0].snapshot.items).find("later") ==
            std::string::npos);
  Q4T_CHECK(f.asr == 0 && f.models == 2 && f.model_released == 2);
  return true;
}

Q4T_TEST(live_frontend_barge_in_joins_generation_before_asr_admission) {
  Fixture f;
  f.hold_model = true;
  Q4T_CHECK(f.Start(Array({User("start")})).ok());
  Q4T_CHECK(f.frontend->Respond().ok() &&
            f.Wait([&] { return f.models == 1; }));
  Q4T_CHECK(f.model_alive && f.model_released == 0);
  Q4T_CHECK(f.Tone() && f.Wait([&] { return f.asr == 1; }));
  Q4T_CHECK(f.model_released == 1 && !f.model_alive);
  f.hold_model = false;
  Q4T_CHECK(f.Wait([&] { return f.completions == 2; }));
  Q4T_CHECK(f.results[0].state == RealtimeResponseEnd::State::kCancelled);
  Q4T_CHECK(f.results[0].usage->total.output == 7);
  Q4T_CHECK(f.results[1].reply.ok() && f.asr == 1 && f.models == 2);
  Q4T_CHECK(!f.overlap && !f.early_release);
  return true;
}

Q4T_TEST(live_frontend_close_drains_owned_work_without_starting_queue) {
  Fixture f;
  f.hold_model = true;
  Q4T_CHECK(f.Start(Array({User("start")})).ok());
  Q4T_CHECK(f.frontend->Respond().ok() &&
            f.Wait([&] { return f.models == 1; }));
  Q4T_CHECK(f.frontend->Respond().ok() && f.frontend->QueuedTurns() == 1);
  Q4T_CHECK(f.frontend->AppendAudio(Base64("1234")).ok());
  Q4T_CHECK(f.frontend->Close().ok() && !f.frontend->Drained());
  Q4T_CHECK(f.frontend->QueuedTurns() == 0 && !f.frontend->Respond().ok());
  Q4T_CHECK(!f.frontend->AppendAudio(Base64("12")).ok());
  Q4T_CHECK(!f.frontend->Mute(true).ok());
  f.hold_model = false;
  Q4T_CHECK(f.Wait([&] { return f.frontend->Drained(); }));
  Q4T_CHECK(f.models == 1 && f.asr == 1 && f.completions == 1);
  Q4T_CHECK(f.results[0].reply.ok() && f.model_released == 1 &&
            f.asr_released == 1);
  Q4T_CHECK(!f.overlap && !f.early_release);
  return true;
}

Q4T_TEST(live_frontend_cancel_joins_each_native_borrower_once) {
  for (bool input : {false, true}) {
    Fixture f;
    f.hold_model = !input;
    f.hold_asr = input;
    Q4T_CHECK(f.Start(Array({User("start")})).ok());
    if (input) {
      Q4T_CHECK(f.Tone() && f.Wait([&] { return f.asr == 1; }));
    } else {
      Q4T_CHECK(f.frontend->Respond().ok() &&
                f.Wait([&] { return f.models == 1; }));
    }
    f.frontend->Cancel();
    f.frontend->Join();
    f.frontend->Join();
    Q4T_CHECK(!f.model_alive && !f.asr_alive && !f.early_release);
    Q4T_CHECK(f.model_released == size_t(!input) &&
              f.asr_released == size_t(input));
    Q4T_CHECK(!f.frontend->Drained() && !f.frontend->Tick().ok());
    Q4T_CHECK(f.Count("session.closed") == 0 && f.completions == 0);
  }
  return true;
}

Q4T_TEST(live_frontend_failed_asr_never_executes_stale_context) {
  Fixture f;
  f.fail_asr = true;
  Q4T_CHECK(f.Start().ok() && f.Tone());
  Q4T_CHECK(f.Wait(
      [&] { return f.asr_released == 1 && f.frontend->QueuedTurns() == 0; }));
  Q4T_CHECK(f.models == 0 && f.Count("error") > 0);
  Q4T_CHECK(f.Count("session.input_transcript.delta") == 0);
  f.fail_asr = false;
  Q4T_CHECK(f.Tone() && f.Wait([&] { return f.completions == 1; }));
  Q4T_CHECK(f.asr == 2 && f.models == 1 && f.results[0].reply.ok());
  Q4T_CHECK(f.snapshots[0].snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(live_frontend_model_failure_keeps_usage_and_next_turn_usable) {
  Fixture f;
  f.fail_model = true;
  Q4T_CHECK(f.Start(Array({User("start")})).ok() && f.frontend->Respond().ok());
  Q4T_CHECK(f.Wait([&] { return f.completions == 1; }));
  Q4T_CHECK(!f.results[0].reply.ok() && f.results[0].usage->total.output == 7);
  Q4T_CHECK(f.Count("error") == 1 &&
            f.Count("session.output_transcript.delta") == 0);
  f.fail_model = false;
  Q4T_CHECK(f.frontend->Respond().ok() &&
            f.Wait([&] { return f.completions == 2; }));
  Q4T_CHECK(f.results[1].reply.ok() && f.models == 2 && f.model_released == 2);
  Q4T_CHECK(f.Count("session.output_transcript.delta") == 1);
  return true;
}

Q4T_TEST(live_frontend_queue_and_invalid_transactions_preserve_history) {
  Fixture f;
  Q4T_CHECK(f.Start(Array({User("start")})).ok());
  for (size_t i = 0; i < LiveFrontend::kQueuedTurns; ++i)
    Q4T_CHECK(f.frontend->Respond().ok());
  const auto bytes = f.frontend->QueuedBytes();
  Q4T_CHECK(bytes <= LiveFrontend::kQueuedTurns * LiveFrontend::kSnapshotBytes);
  Q4T_CHECK(!f.frontend->Create(User("not committed"), true).ok());
  Q4T_CHECK(!f.frontend->Create(Object()).ok());
  Q4T_CHECK(!f.frontend->Settings(Settings("reject")).ok());
  Q4T_CHECK(f.frontend->HistoryItems() == 1 &&
            f.frontend->QueuedBytes() == bytes);
  Q4T_CHECK(
      f.Wait([&] { return f.completions == LiveFrontend::kQueuedTurns; }));
  Q4T_CHECK(f.frontend->Respond().ok() &&
            f.Wait([&] { return f.completions == 5; }));
  Q4T_CHECK(f.snapshots.back().settings.GetString("instructions") == "old");
  Q4T_CHECK(!f.overlap && !f.early_release);
  return true;
}

Q4T_TEST(live_frontend_uncertain_completion_is_not_replayed) {
  Fixture f;
  f.fail_completion = true;
  Q4T_CHECK(f.Start(Array({User("start")})).ok() && f.frontend->Respond().ok());
  bool failed = false;
  const auto deadline = Clock::now() + 2s;
  while (!failed && Clock::now() < deadline) {
    failed = !f.frontend->Tick().ok();
    std::this_thread::sleep_for(1ms);
  }
  Q4T_CHECK(failed && f.completions == 1 && f.models == 1);
  Q4T_CHECK(!f.frontend->Tick().ok() && !f.frontend->Respond().ok());
  f.frontend->Join();
  Q4T_CHECK(f.completions == 1 && f.model_released == 1 && !f.early_release);
  return true;
}

Q4T_TEST(live_frontend_preparation_failure_never_executes_or_publishes) {
  for (int mode = 0; mode < 5; ++mode) {
    Fixture f;
    if (mode == 0) f.bad_prepare = true;
    if (mode == 1) f.bad_input = true;
    if (mode == 2) f.native.completed = {};
    if (mode == 3) f.rate = 44100;
    const auto initial = mode == 4 ? Array({Object()}) : Array();
    Q4T_CHECK(!f.Start(initial).ok());
    Q4T_CHECK(f.models == 0 && f.asr == 0 && f.events.empty());
    Q4T_CHECK(f.model_released == 0 && f.asr_released == 0);
    Q4T_CHECK(!f.frontend->Respond().ok() && !f.frontend->Tick().ok());
  }
  return true;
}
