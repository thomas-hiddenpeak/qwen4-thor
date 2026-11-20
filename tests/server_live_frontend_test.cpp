#include "q4t/server/live_frontend.h"
#include "q4t/server/live_client_context.h"

#include <algorithm>

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/http_response.h"
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
  std::atomic<bool> delegate_next{false};
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
      } else if (delegate_next.exchange(false)) {
        ResponseDelta call;
        call.kind = ResponseDelta::Kind::kTool;
        call.call = {0, detail::NewRequestId("call_"),
                     std::string(kLiveDelegateTool),
                     R"({"task":"Read project resources"})"};
        result.reply = delta(call);
        result.message = Object(
            {{"role", String("assistant")},
             {"content", String("")},
             {"tool_calls",
              Array({Object(
                  {{"id", String(call.call.id)},
                   {"type", String("function")},
                   {"function", Object({{"name", String(call.call.name)},
                                        {"arguments",
                                         String(call.call.arguments)}})}})})}});
        result.usage->output_text = 7;
        result.usage->output_audio = 0;
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
  ObjectReply Start(const Json& initial = Array(), Json settings = Settings()) {
    frontend = std::make_unique<LiveFrontend>("live_cpu", native, 30);
    return frontend->Prepare(std::move(settings), initial,
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
struct ContextFixture {
  Fixture f;
  std::unique_ptr<LiveClientContext> context;
  Clock::time_point now = Clock::now();
  size_t tokens = 1, count_calls = 0;
  bool fail_emit = false, fail_count = false;
  bool Start(Json settings = LiveClientFrontendSettings(Settings())) {
    const auto completed = f.native.completed;
    f.native.completed = [this, completed](const auto& job, const auto& result,
                                           const auto& output) {
      auto reply = completed(job, result, output);
      return reply.ok() ? context->Complete(job, result, output) : reply;
    };
    Q4T_CHECK(f.Start(Array(), settings).ok());
    LiveClientContextCallbacks callbacks;
    callbacks.count_text = [this](auto, size_t* out, ChatPlanError* error) {
      ++count_calls;
      if (fail_count) {
        *error = {"fixture_tokenizer", "content", 503};
        return q4t::Status::Fail("Actual CPU counter unavailable");
      }
      *out = tokens;
      return q4t::Status{};
    };
    callbacks.emit = [this](Json event) {
      if (fail_emit) return Error(503, "fixture_sink", "Output not accepted");
      return f.native.emit(std::move(event));
    };
    callbacks.now = [this] { return now; };
    context = std::make_unique<LiveClientContext>(
        "live_cpu", *f.frontend, std::move(settings), std::move(callbacks));
    return context->Valid();
  }
  ObjectReply Append(std::string kind, std::string text,
                     std::string delegation = {},
                     std::string id = "context_cpu") {
    return context->Append(
        Object({{"type", String("session." + kind + ".append")},
                {"content", String(std::move(text))},
                {"delegation_id",
                 delegation.empty() ? Json() : String(std::move(delegation))},
                {"event_id", String(std::move(id))}}));
  }
  bool Delegate() {
    const auto completed = f.completions;
    f.delegate_next = true;
    Q4T_CHECK(f.frontend->Respond().ok());
    return f.Wait([&] { return f.completions == completed + 1; });
  }
  std::string LastDelegation() const {
    for (auto it = f.events.rbegin(); it != f.events.rend(); ++it)
      if (it->GetString("type") == "session.delegation.created")
        return it->Find("delegation")->GetString("id");
    return {};
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

Q4T_TEST(live_client_context_instructions_persist_and_ids_require_real_owner) {
  ContextFixture c;
  Q4T_CHECK(c.Start());
  const auto original = c.context->Settings().GetString("instructions");
  Q4T_CHECK(c.Append("instructions", "Remember the project name.").ok());
  Q4T_CHECK(c.f.Wait([&] { return c.f.completions == 1; }));
  Q4T_CHECK(c.f.snapshots[0].settings.GetString("instructions") ==
            original + "\n\nRemember the project name.");
  Q4T_CHECK(c.f.snapshots[0].response.GetString("tool_choice") == "none");
  Q4T_CHECK(c.f.Count("session.instructions.appended") == 1);
  const auto history = c.f.frontend->HistoryItems();
  Q4T_CHECK(!c.Append("thinking", "Unowned context", "foreign").ok());
  Q4T_CHECK(c.f.frontend->HistoryItems() == history && c.count_calls == 1);
  Q4T_CHECK(c.Delegate() && c.context->Known(c.LastDelegation()));
  Q4T_CHECK(
      c.Append("thinking", "Confirmed application context", c.LastDelegation())
          .ok());
  Q4T_CHECK(c.context->Settings().GetString("instructions") ==
            original + "\n\nRemember the project name.");
  return true;
}

Q4T_TEST(
    live_client_context_uses_token_counter_and_bounded_persistent_storage) {
  ContextFixture c;
  Q4T_CHECK(c.Start());
  c.tokens = 500;
  Q4T_CHECK(c.Append("thinking", std::string(8192, 'x')).ok());
  const auto history = c.f.frontend->HistoryItems();
  const auto events = c.f.events.size();
  c.tokens = 501;
  Q4T_CHECK(!c.Append("thinking", "short bytes are not token counts").ok());
  c.fail_count = true;
  Q4T_CHECK(!c.Append("instructions", "No fake count on failure").ok());
  Q4T_CHECK(c.f.frontend->HistoryItems() == history &&
            c.f.events.size() == events);
  Q4T_CHECK(c.f.models == 0 && c.count_calls == 3);
  ContextFixture bounded;
  auto settings = LiveClientFrontendSettings(Settings());
  Set(&settings, "instructions",
      String(std::string(LiveClientContext::kInstructionsBytes - 3, 'x')));
  Q4T_CHECK(bounded.Start(settings));
  Q4T_CHECK(!bounded.Append("instructions", "beyond").ok());
  Q4T_CHECK(bounded.context->Settings().GetString("instructions") ==
            settings.GetString("instructions"));
  Q4T_CHECK(bounded.f.frontend->QueuedTurns() == 0 && bounded.f.models == 0);
  return true;
}

Q4T_TEST(live_client_context_thinking_is_silent_commentary_runs_real_worker) {
  ContextFixture c;
  Q4T_CHECK(c.Start() && c.Append("thinking", "Lookup is in progress").ok());
  Q4T_CHECK(c.f.frontend->QueuedTurns() == 0 && c.f.models == 0);
  Q4T_CHECK(c.Append("commentary", "The project uses native serving").ok());
  Q4T_CHECK(c.f.frontend->QueuedTurns() == 1);
  Q4T_CHECK(c.f.Wait([&] { return c.f.completions == 1; }));
  Q4T_CHECK(c.f.models == 1 && c.f.results[0].reply.ok());
  Q4T_CHECK(c.f.snapshots[0].snapshot.items.array.size() == 2);
  Q4T_CHECK(c.f.snapshots[0].response.GetString("tool_choice") == "none");
  Q4T_CHECK(c.f.Count("session.thinking.appended") == 1);
  Q4T_CHECK(c.f.Count("session.commentary.appended") == 1);
  Q4T_CHECK(c.f.Count("session.output_transcript.delta") == 1);
  for (const auto& event : c.f.events)
    if (event.GetString("type").ends_with(".appended")) {
      Q4T_CHECK(event.GetString("client_event_id") == "context_cpu");
      Q4T_CHECK(event.Find("start_ms")->number == event.Find("end_ms")->number);
    }
  return true;
}

Q4T_TEST(
    live_client_delegation_metadata_and_native_receipt_never_claim_task_done) {
  ContextFixture c;
  Q4T_CHECK(c.Start() && c.context->AcknowledgementDelay(1000).ok());
  Q4T_CHECK(c.Delegate());
  Q4T_CHECK(c.context->Delegations() == 1 &&
            c.context->Known(c.LastDelegation()));
  Q4T_CHECK(c.f.Count("session.delegation.created") == 1);
  const auto& event = c.f.events.back();
  Q4T_CHECK(event.GetString("type") == "session.delegation.created");
  Q4T_CHECK(event.Find("delegation")->object.size() == 3);
  Q4T_CHECK(!event.Has("task") && !event.Has("arguments"));
  Q4T_CHECK(c.f.Count("session.output_audio.delta") == 0);
  Q4T_CHECK(c.context->Tick().ok() && c.f.frontend->QueuedTurns() == 0);
  Q4T_CHECK(
      !c.context->Complete(c.f.snapshots[0], c.f.results[0], c.f.outputs[0])
           .ok());
  Q4T_CHECK(c.context->Delegations() == 1);
  Q4T_CHECK(c.f.frontend->Respond().ok() &&
            c.f.Wait([&] { return c.f.completions == 2; }));
  bool found = false;
  for (const auto& item : c.f.snapshots[1].snapshot.items.array) {
    if (item.GetString("type") != "function_call_output") continue;
    Json receipt;
    Q4T_CHECK(q4t::io::ParseJson(item.GetString("output"), &receipt));
    Q4T_CHECK(receipt.GetString("status") == "created");
    Q4T_CHECK(receipt.Find("task_completed")->IsBool() &&
              !receipt.GetBool("task_completed"));
    Q4T_CHECK(receipt.GetString("delegation_id") == c.LastDelegation());
    found = true;
  }
  Q4T_CHECK(found && c.f.models == 2);
  return true;
}

Q4T_TEST(live_client_delegation_rejects_partial_foreign_and_mismatched_calls) {
  ContextFixture c;
  Q4T_CHECK(c.Start() && c.Delegate());
  const auto events = c.f.events.size();
  for (int mode = 0; mode < 8; ++mode) {
    auto job = c.f.snapshots[0];
    auto result = c.f.results[0];
    auto output = c.f.outputs[0];
    const auto id = "call_negative_" + std::to_string(mode);
    auto& call = Field(result.message, "tool_calls").array[0];
    Set(&call, "id", String(id));
    Set(&output.array[0], "call_id", String(id));
    if (mode == 0) job.snapshot.owner = "foreign";
    if (mode == 1)
      Set(&Field(call, "function"), "name", String("foreign_tool"));
    if (mode == 2) output = Array();
    if (mode == 3) Set(&output.array[0], "arguments", String("{}"));
    if (mode == 4) Set(&output.array[0], "status", String("incomplete"));
    if (mode == 5) Set(&job.response, "tool_choice", String("none"));
    if (mode == 6 || mode == 7) {
      auto args = OutputJson(
          Object({{"task", String(mode == 6 ? " " : std::string(4097, 'x'))}}));
      Set(&Field(call, "function"), "arguments", String(args));
      Set(&output.array[0], "arguments", String(args));
    }
    Q4T_CHECK(!c.context->Complete(job, result, output).ok());
  }
  for (auto state : {RealtimeResponseEnd::State::kIncomplete,
                     RealtimeResponseEnd::State::kCancelled,
                     RealtimeResponseEnd::State::kFailed}) {
    auto result = c.f.results[0];
    result.state = state;
    Q4T_CHECK(
        c.context->Complete(c.f.snapshots[0], result, c.f.outputs[0]).ok());
  }
  Q4T_CHECK(c.context->Delegations() == 1 && c.f.events.size() == events);
  return true;
}

Q4T_TEST(live_client_ack_deadlines_freeze_and_any_context_cancels_all) {
  ContextFixture c;
  Q4T_CHECK(c.Start() && c.context->AcknowledgementDelay(1000).ok());
  Q4T_CHECK(c.Delegate());
  const auto first = c.LastDelegation();
  Q4T_CHECK(c.context->AcknowledgementDelay(2000).ok() && c.Delegate());
  const auto second = c.LastDelegation();
  Q4T_CHECK(first != second && c.context->Delegations() == 2);
  Q4T_CHECK(c.context->AcknowledgementDelay(0).ok());
  c.now += 900ms;
  Q4T_CHECK(c.context->Tick().ok() && c.f.frontend->QueuedTurns() == 0);
  c.now += 100ms;
  Q4T_CHECK(c.context->Tick().ok() && c.f.frontend->QueuedTurns() == 1);
  Q4T_CHECK(c.Append("thinking", "Both tasks are in progress", second).ok());
  Q4T_CHECK(c.f.frontend->QueuedTurns() == 0);
  c.now += 3s;
  Q4T_CHECK(c.context->Tick().ok() && c.f.frontend->QueuedTurns() == 0);
  Q4T_CHECK(c.context->Known(first) && c.context->Known(second) &&
            c.f.models == 2);
  return true;
}

Q4T_TEST(live_client_delegation_accepts_multiple_real_context_updates) {
  ContextFixture c;
  Q4T_CHECK(c.Start() && c.Delegate());
  const auto id = c.LastDelegation();
  Q4T_CHECK(c.Append("thinking", "First progress update", id).ok());
  Q4T_CHECK(c.Append("commentary", "First confirmed result", id).ok());
  Q4T_CHECK(c.Append("commentary", "Second confirmed result", id).ok());
  Q4T_CHECK(c.f.Wait([&] { return c.f.completions == 3; }));
  Q4T_CHECK(c.context->Known(id) && c.context->Delegations() == 1);
  Q4T_CHECK(c.f.Count("session.commentary.appended") == 2);
  Q4T_CHECK(c.f.Count("session.output_transcript.delta") == 2);
  Q4T_CHECK(c.f.snapshots[1].response.GetString("tool_choice") == "none");
  Q4T_CHECK(c.f.snapshots[2].response.GetString("tool_choice") == "none");
  return true;
}

Q4T_TEST(
    live_client_context_unknown_publication_never_replays_committed_state) {
  ContextFixture c;
  Q4T_CHECK(c.Start());
  c.fail_emit = true;
  const auto reply = c.Append("thinking", "Only one history mutation");
  Q4T_CHECK(!reply.ok() && reply.commit_unknown);
  Q4T_CHECK(c.f.frontend->HistoryItems() == 1 && c.f.events.empty());
  c.fail_emit = false;
  Q4T_CHECK(!c.Append("thinking", "Only one history mutation").ok());
  Q4T_CHECK(!c.context->Tick().ok());
  Q4T_CHECK(c.f.frontend->HistoryItems() == 1 && c.f.models == 0);
  ContextFixture full;
  Q4T_CHECK(full.Start() && full.Delegate());
  while (full.f.frontend->HistoryItems() < RealtimeConversation::kMaximumItems)
    Q4T_CHECK(full.f.frontend->Create(User("retained")).ok());
  auto result = full.f.results[0];
  auto output = full.f.outputs[0];
  Set(&Field(result.message, "tool_calls").array[0], "id", String("call_full"));
  Set(&output.array[0], "call_id", String("call_full"));
  const auto events = full.f.events.size();
  Q4T_CHECK(!full.context->Complete(full.f.snapshots[0], result, output).ok());
  Q4T_CHECK(full.f.events.size() == events && !full.context->Tick().ok());
  return true;
}

Q4T_TEST(
    live_client_context_close_cancels_pending_ack_without_invented_completion) {
  ContextFixture c;
  Q4T_CHECK(c.Start() && c.Delegate() && c.context->Tick().ok());
  Q4T_CHECK(c.f.frontend->QueuedTurns() == 1);
  c.context->Close();
  Q4T_CHECK(c.f.frontend->QueuedTurns() == 0);
  Q4T_CHECK(c.f.frontend->Close().ok() &&
            c.f.Wait([&] { return c.f.frontend->Drained(); }));
  Q4T_CHECK(!c.Append("commentary", "late", c.LastDelegation()).ok());
  Q4T_CHECK(
      c.context->Complete(c.f.snapshots[0], c.f.results[0], c.f.outputs[0])
          .ok());
  Q4T_CHECK(c.context->Tick().ok() && c.f.models == 1);
  Q4T_CHECK(c.f.Count("session.output_audio.delta") == 0);
  Q4T_CHECK(c.f.Count("session.closed") == 0);
  return true;
}

Q4T_TEST(live_frontend_response_options_and_context_queue_are_atomic) {
  Fixture f;
  Q4T_CHECK(f.Start().ok());
  std::vector<std::string> ids;
  for (size_t i = 0; i < LiveFrontend::kQueuedTurns; ++i) {
    auto reply = f.frontend->Respond();
    Q4T_CHECK(reply.ok());
    ids.push_back(reply.value.GetString("response_id"));
  }
  Q4T_CHECK(!f.frontend->Settings(Settings("not committed"), true).ok());
  Q4T_CHECK(f.frontend->CancelResponse(ids[0]));
  Q4T_CHECK(!f.frontend->CancelResponse(ids[0]));
  const auto items = f.frontend->HistoryItems();
  Q4T_CHECK(
      !f.frontend
           ->Create(User("rolled back"), true, {}, String("invalid response"))
           .ok());
  Q4T_CHECK(f.frontend->HistoryItems() == items);
  Q4T_CHECK(f.frontend
                ->Settings(Settings("committed"), true, "settings_cpu",
                           Object({{"tool_choice", String("none")}}))
                .ok());
  Q4T_CHECK(
      f.Wait([&] { return f.completions == LiveFrontend::kQueuedTurns; }));
  Q4T_CHECK(f.snapshots.back().settings.GetString("instructions") ==
            "committed");
  Q4T_CHECK(f.snapshots.back().response.GetString("tool_choice") == "none");
  for (size_t i = 0; i + 1 < f.snapshots.size(); ++i)
    Q4T_CHECK(f.snapshots[i].settings.GetString("instructions") == "old");
  return true;
}
