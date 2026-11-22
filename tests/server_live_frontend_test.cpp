#include "q4t/server/live_frontend.h"
#include "q4t/server/live_client_context.h"
#include "q4t/server/live_native_backend.h"
#include "q4t/server/live_responses_settings.h"
#include "q4t/server/responses_output.h"
#include "q4t/server/chat_prompt.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>

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

ChatPlanConfig NativeConfig() {
  ChatPlanConfig config;
  config.model = "q4t-test";
  config.max_len = 8192;
  config.structured = true;
  config.native_tts_enabled = true;
  config.native_asr_enabled = true;
  return config;
}
struct NativeFixture {
  Fixture f;
  ChatPlanConfig planning = NativeConfig();
  WebSocketOutput output{1ULL << 20, 64};
  LiveSessionCallbacks callbacks;
  std::unique_ptr<LiveSession> session;
  std::vector<Json> events;
  size_t creations = 0, publications = 0;
  bool fail_publish = false;
  LiveResponsesNative responses;
  LiveDecisionNative decisions;
  ObjectStore* recording_objects = nullptr;
  ~NativeFixture() {
    if (session) {
      session->Cancel();
      session->Join();
    }
  }
  NativeFixture() {
    callbacks.validate_responses = [this](const Json& settings,
                                          std::stop_token) {
      ChatPlanError error;
      const auto status = CheckLiveResponsesSettings(settings, planning,
                                                     "local:anonymous", &error);
      return status ? ObjectReply{}
                    : Error(error.http_status, error.code, status.message());
    };
    callbacks.configuration.default_voice = String("vivian");
    callbacks.configuration.default_instructions = "BASE instructions";
    callbacks.configuration.count_text = [](auto, size_t* count, auto*) {
      *count = 4;
      return q4t::Status{};
    };
    callbacks.configuration.count_history = [](const auto&, size_t* count,
                                               auto*) {
      *count = 10;
      return q4t::Status{};
    };
    callbacks.configuration.validate = [this](const auto& value, auto* error) {
      NativeLivePlan plan;
      return PlanNativeLiveClient(
          value, planning, &plan, error, bool(responses.execute),
          bool(decisions.execute), recording_objects != nullptr);
    };
    callbacks.create = [this](const Json& configuration,
                              const LiveEventSink& emit,
                              std::unique_ptr<ILiveSessionBackend>* out) {
      ++creations;
      NativeLivePlan plan;
      ChatPlanError error;
      const auto status = PlanNativeLiveClient(
          configuration, planning, &plan, &error, bool(responses.execute),
          bool(decisions.execute), recording_objects != nullptr);
      if (!status)
        return Error(error.http_status, error.code, status.message());
      auto native = f.native;
      native.emit = emit;
      const auto previous = native.prepare;
      native.prepare = [this, previous](const auto& job) {
        RealtimeChatPlan prepared;
        ChatPlanError failure;
        const auto checked =
            PlanRealtimeChat(job.settings, job.response, job.snapshot.items,
                             planning, &prepared, &failure, nullptr, true);
        return checked ? previous(job)
                       : Error(failure.http_status, failure.code,
                               checked.message());
      };
      LiveClientContextCallbacks context;
      context.count_text = callbacks.configuration.count_text;
      context.emit = [this, emit](Json event) {
        ++publications;
        return fail_publish
                   ? Error(503, "fixture_sink", "Publication uncertain", true)
                   : emit(std::move(event));
      };
      std::unique_ptr<LiveRecordingCapture> capture;
      if (configuration.GetBool("store"))
        capture = std::make_unique<LiveRecordingCapture>(
            *recording_objects, "client:test", "live_bound", plan.format,
            [] { return false; });
      auto selected = std::make_unique<NativeLiveClientBackend>(
          "live_bound", std::move(native), std::move(context), 30,
          kWebSocketMessageLimit, responses, decisions, std::move(capture));
      auto* owner = selected.get();
      *out = std::move(selected);
      return owner->Prepare(configuration, std::move(plan));
    };
  }
  bool Read() {
    while (!output.Front().empty()) {
      const auto frame = output.Front();
      WebSocketDecoder decoder(false);
      size_t consumed = 0;
      Q4T_CHECK(decoder.Consume({reinterpret_cast<const uint8_t*>(frame.data()),
                                 frame.size()},
                                &consumed) == WebSocketRead::kEvent);
      Q4T_CHECK(consumed == frame.size());
      Json event;
      Q4T_CHECK(q4t::io::ParseJson(std::string(decoder.Event().payload), &event,
                                   true));
      ChatPlanError error;
      Q4T_CHECK(ValidateLiveWireShape(event, LiveShape::kServerEvent, &error));
      events.push_back(std::move(event));
      output.Acknowledge();
    }
    return true;
  }
  bool Start(Json configuration = Object({{"model", String("q4t-test")}})) {
    if (!session) {
      session = std::make_unique<LiveSession>("live_bound", 1795200000, output,
                                              callbacks);
      Q4T_CHECK(session->Start());
    }
    return Send(Object({{"type", String("session.start")},
                        {"session", std::move(configuration)}}));
  }
  bool Send(Json event) {
    return session->Message(WebSocketOpcode::kText, OutputJson(event)) &&
           Read();
  }
  bool Command(std::string type) {
    return Send(Object({{"type", String(std::move(type))}}));
  }
  bool Append(std::string kind, std::string text, std::string delegation = {}) {
    return Send(Object(
        {{"type", String("session." + kind + ".append")},
         {"content", String(std::move(text))},
         {"delegation_id",
          delegation.empty() ? Json() : String(std::move(delegation))}}));
  }
  bool Tone() {
    f.frames = 0;
    f.voiced = true;
    return Send(
        Object({{"type", String("session.input_audio.append")},
                {"audio", String(Base64(std::string(24000, '\x15')))}}));
  }
  bool Wait(const std::function<bool()>& predicate) {
    const auto deadline = Clock::now() + 2s;
    while (Clock::now() < deadline) {
      Q4T_CHECK(session->Tick() && Read());
      if (predicate()) return true;
      std::this_thread::sleep_for(1ms);
    }
    return false;
  }
  size_t Count(std::string_view type) const {
    return std::ranges::count_if(events, [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
  Json Last(std::string_view type) const {
    for (auto it = events.rbegin(); it != events.rend(); ++it)
      if (it->GetString("type") == type) return *it;
    return {};
  }
  bool Close() {
    return Command("session.close") &&
           Wait([&] { return session->Finalized(); }) &&
           Count("session.closed") == 1;
  }
};
}  // namespace

Q4T_TEST(live_native_optional_startup_types_preserve_roles_and_prompt_order) {
  NativeFixture f;
  auto initial = Array();
  for (const auto* role : {"developer", "user", "assistant"}) {
    initial.array.push_back(Object(
        {{"role", String(role)},
         {"content", Array({Object({{"text", String(std::string(role) +
                                                    " unique text")}})})}}));
  }
  Json config;
  ChatPlanError error;
  Q4T_CHECK(ResolveLiveConfiguration(
      Object({{"model", String("q4t-test")}, {"input", initial}}),
      f.callbacks.configuration, &config, &error));
  NativeLivePlan mapped;
  Q4T_CHECK(PlanNativeLiveClient(config, f.planning, &mapped, &error));
  Q4T_CHECK(mapped.input.array.size() == 3);
  Q4T_CHECK(mapped.input.array[0].GetString("role") == "system");
  Q4T_CHECK(mapped.input.array[1].GetString("role") == "user");
  Q4T_CHECK(mapped.input.array[2].GetString("role") == "assistant");
  RealtimeChatPlan plan;
  Q4T_CHECK(PlanRealtimeChat(mapped.settings, Object(), mapped.input,
                             f.planning, &plan, &error, nullptr, true));
  const auto& generation = plan.generation;
  std::string prompt;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(
      generation.chat.Request(generation.generation_request),
      generation.chat.constraints, &prompt, &media, &error.param, &error.code,
      nullptr, true, generation.chat.async_tool_call_ids,
      generation.chat.responses_tool_media, generation.chat.HeaderTools(),
      generation.chat.tool_call_formats, generation.chat.prepared_source_data));
  Q4T_CHECK(media.empty());
  const auto developer = prompt.find("developer unique text");
  const auto user = prompt.find("user unique text");
  const auto assistant = prompt.find("assistant unique text");
  Q4T_CHECK(developer != prompt.npos && developer < user && user < assistant &&
            assistant != prompt.npos);
  const auto accepted = OutputJson(config);
  Set(&Field(initial.array[1], "content").array[0], "type",
      String("output_text"));
  Q4T_CHECK(!ResolveLiveConfiguration(
      Object({{"model", String("q4t-test")}, {"input", initial}}),
      f.callbacks.configuration, &config, &error));
  Q4T_CHECK(OutputJson(config) == accepted);
  return true;
}

Q4T_TEST(live_native_wire_audio_mapping_preserves_native_speech_contract) {
  NativeFixture f;
  for (const auto* type : {"audio/pcm", "audio/pcmu", "audio/pcma"}) {
    for (const int rate : {8000, 16000, 24000}) {
      Json configuration;
      ChatPlanError error;
      const auto requested = Object(
          {{"model", String("q4t-test")},
           {"audio", Object({{"format", Object({{"type", String(type)},
                                                {"rate", Number(rate)}})}})}});
      const bool allowed =
          std::string(type) == "audio/pcm" ? rate != 8000 : rate == 8000;
      Q4T_CHECK(ResolveLiveConfiguration(requested, f.callbacks.configuration,
                                         &configuration, &error)
                    .ok() == allowed);
      if (!allowed) continue;
      NativeLivePlan plan;
      Q4T_CHECK(PlanNativeLiveClient(configuration, f.planning, &plan, &error));
      Q4T_CHECK(plan.format.rate == uint32_t(rate));
      const auto* audio = plan.settings.Find("audio")->Find("output");
      Q4T_CHECK(audio->Find("format")->GetInt("rate") == 24000);
      Q4T_CHECK(audio->Find("format")->GetString("type") == "audio/pcm");
      Q4T_CHECK(audio->GetString("voice") == "vivian");
      Q4T_CHECK(plan.settings.GetArray("tools")->array.size() == 1);
      Q4T_CHECK(plan.settings.GetArray("tools")->array[0].GetString("name") ==
                kLiveDelegateTool);
    }
  }
  return true;
}

Q4T_TEST(live_native_unconnected_selection_never_starts_or_publishes) {
  NativeFixture f;
  Q4T_CHECK(f.Start(
      Object({{"model", String("q4t-test")}, {"store", Boolean(true)}})));
  Q4T_CHECK(f.creations == 0 && f.Count("session.started") == 0);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "live_recording_disabled");
  Q4T_CHECK(f.Start(Object({{"model", String("unknown-native-model")}})));
  Q4T_CHECK(f.creations == 0 && f.Count("session.started") == 0);
  Q4T_CHECK(f.Start() && f.Count("session.started") == 1 && f.creations == 1);
  Q4T_CHECK(f.f.asr == 0 && f.f.models == 0 && f.Close());
  return true;
}

Q4T_TEST(live_native_concrete_session_runs_input_speech_mute_and_close) {
  NativeFixture f;
  Q4T_CHECK(f.Start() && f.Count("session.started") == 1);
  Q4T_CHECK(f.Command("session.input_audio.mute"));
  Q4T_CHECK(f.Count("session.input_audio.muted") == 1);
  Q4T_CHECK(f.Tone() && f.f.asr == 0);
  Q4T_CHECK(f.Command("session.input_audio.unmute"));
  Q4T_CHECK(f.Count("session.input_audio.unmuted") == 1);
  Q4T_CHECK(f.Tone() && f.Wait([&] { return f.f.model_released == 1; }));
  Q4T_CHECK(f.f.asr_released == 1 && f.f.models == 1);
  Q4T_CHECK(f.Count("session.input_transcript.delta") == 1);
  Q4T_CHECK(f.Count("session.output_audio.delta") > 0);
  Q4T_CHECK(f.Count("session.output_transcript.delta") == 1);
  Q4T_CHECK(f.Close());
  Q4T_CHECK(!f.f.overlap && !f.f.early_release && f.f.owner_thread);
  Q4T_CHECK(!f.f.model_alive && !f.f.asr_alive);
  return true;
}

Q4T_TEST(live_native_close_quiet_tail_has_no_extra_work_or_transcript) {
  NativeFixture f;
  f.f.native.input.vad_step = [](auto samples, auto*, float* probability) {
    *probability = std::ranges::any_of(samples,
                                       [](float sample) {
                                         return sample > .01f || sample < -.01f;
                                       })
                       ? 1.f
                       : 0.f;
    return q4t::Status{};
  };
  Q4T_CHECK(f.Start());
  std::string raw(6000, '\x15');
  raw.append(18000, '\0');
  Q4T_CHECK(f.Send(Object({{"type", String("session.input_audio.append")},
                           {"audio", String(Base64(raw))}})));
  Q4T_CHECK(f.Wait([&] { return f.f.model_released == 1; }));
  Q4T_CHECK(f.Close() && f.Count("session.closed") == 1);
  Q4T_CHECK(f.f.asr == 1 && f.f.asr_released == 1 && f.f.models == 1);
  Q4T_CHECK(f.Count("session.input_transcript.delta") == 1);
  Q4T_CHECK(f.Count("session.output_transcript.delta") == 1);
  Q4T_CHECK(!f.Count("error") && !f.f.overlap && !f.f.early_release);
  Q4T_CHECK(!f.f.model_alive && !f.f.asr_alive);
  return true;
}

Q4T_TEST(live_native_update_keeps_appended_instructions_and_snapshot_context) {
  NativeFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.Append("instructions", "APPENDED instruction"));
  Q4T_CHECK(f.Wait([&] { return f.f.model_released == 1; }));
  Q4T_CHECK(f.Send(Object(
      {{"type", String("session.update")},
       {"session", Object({{"delegation", Object({{"type", String("client")},
                                                  {"acknowledgement_delay_ms",
                                                   Number(1000)}})}})}})));
  Q4T_CHECK(f.Count("session.updated") == 1);
  Q4T_CHECK(f.Append("thinking", "FACT checked silently"));
  Q4T_CHECK(f.f.models == 1);
  Q4T_CHECK(f.Append("commentary", "Say the checked fact"));
  Q4T_CHECK(f.Wait([&] { return f.f.model_released == 2; }));
  Q4T_CHECK(f.f.snapshots.size() == 2);
  for (const auto& job : f.f.snapshots) {
    const auto instructions = job.settings.GetString("instructions");
    Q4T_CHECK(instructions.find("BASE instructions") != std::string::npos);
    Q4T_CHECK(instructions.find("APPENDED instruction") != std::string::npos);
    Q4T_CHECK(job.response.GetString("tool_choice") == "none");
  }
  Q4T_CHECK(OutputJson(f.f.snapshots[0].snapshot.items).find("FACT checked") ==
            std::string::npos);
  Q4T_CHECK(OutputJson(f.f.snapshots[1].snapshot.items).find("FACT checked") !=
            std::string::npos);
  Q4T_CHECK(f.Close());
  return true;
}

Q4T_TEST(live_native_client_delegation_has_real_handoff_and_result_context) {
  NativeFixture f;
  Q4T_CHECK(f.Start());
  f.f.delegate_next = true;
  Q4T_CHECK(f.Tone() && f.Wait([&] { return f.f.model_released == 1; }));
  Q4T_CHECK(f.Count("session.delegation.created") == 1);
  const auto delegation = f.Last("session.delegation.created");
  const auto id = delegation.Find("delegation")->GetString("id");
  Q4T_CHECK(!id.empty());
  Q4T_CHECK(OutputJson(delegation).find("Read project resources") ==
            std::string::npos);
  Q4T_CHECK(f.Append("thinking", "Actual project result", id));
  Q4T_CHECK(f.Append("commentary", "Summarize the real result", id));
  Q4T_CHECK(f.Wait([&] { return f.f.model_released == 2; }));
  const auto& items = f.f.snapshots[1].snapshot.items;
  bool found = false;
  for (const auto& item : items.array) {
    if (item.GetString("type") != "function_call_output") continue;
    Json receipt;
    Q4T_CHECK(q4t::io::ParseJson(item.GetString("output"), &receipt, true));
    Q4T_CHECK(!receipt.GetBool("task_completed"));
    Q4T_CHECK(receipt.GetString("delegation_id") == id);
    found = true;
  }
  Q4T_CHECK(found && OutputJson(items).find("Actual project result") !=
                         std::string::npos);
  Q4T_CHECK(f.Close() && f.f.models == 2);
  return true;
}

Q4T_TEST(live_native_partial_prepare_is_cleaned_before_known_retry) {
  NativeFixture f;
  f.f.bad_input = true;
  Q4T_CHECK(f.Start() && f.creations == 1);
  Q4T_CHECK(f.Count("session.started") == 0 && f.Count("error") == 1);
  Q4T_CHECK(f.f.models == 0 && f.f.asr == 0);
  f.f.bad_input = false;
  Q4T_CHECK(f.Start() && f.creations == 2);
  Q4T_CHECK(f.Count("session.started") == 1 && f.Close());
  return true;
}

Q4T_TEST(live_native_uncertain_publication_terminates_without_replay) {
  NativeFixture f;
  Q4T_CHECK(f.Start());
  f.fail_publish = true;
  Q4T_CHECK(!f.Append("instructions", "Durable but uncertain publication"));
  f.session->Join();
  Q4T_CHECK(f.publications == 1 && f.f.models == 0);
  Q4T_CHECK(!f.session->Tick() && !f.session->Finalized());
  Q4T_CHECK(f.Count("session.closed") == 0 && f.creations == 1);
  Q4T_CHECK(!f.Command("session.close") && f.publications == 1);
  return true;
}

Q4T_TEST(live_native_disconnect_joins_the_active_borrower_without_completion) {
  NativeFixture f;
  Q4T_CHECK(f.Start());
  f.f.hold_model = true;
  Q4T_CHECK(f.Append("commentary", "Native active generation"));
  Q4T_CHECK(f.Wait([&] { return f.f.model_alive.load(); }));
  f.session->Cancel();
  f.session->Join();
  Q4T_CHECK(f.f.model_released == 1 && !f.f.model_alive);
  Q4T_CHECK(!f.f.early_release && !f.session->Finalized());
  Q4T_CHECK(f.Count("session.closed") == 0);
  return true;
}

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

Q4T_TEST(live_fixture_frontend_validates_before_cancel_and_keeps_usage) {
  Fixture f;
  auto now = Clock::now();
  f.native.now = [&] { return now; };
  f.hold_model = true;
  Q4T_CHECK(f.Start().ok() && f.frontend->Respond().ok());
  Q4T_CHECK(f.Wait([&] { return f.model_alive.load(); }));
  Q4T_CHECK(f.frontend->Respond().ok());
  for (const auto& bad : {std::string("%bad"), Base64("abc")}) {
    Q4T_CHECK(!f.frontend->PlayFixture(bad, "bad").ok());
    Q4T_CHECK(f.frontend->Tick().ok() && f.model_alive);
    Q4T_CHECK(f.frontend->QueuedTurns() == 1 && !f.frontend->FixtureActive());
  }
  Q4T_CHECK(
      f.frontend->PlayFixture(Base64(std::string(960, '\x15')), "play").ok());
  Q4T_CHECK(f.frontend->FixtureActive() && f.frontend->QueuedTurns() == 0);
  Q4T_CHECK(f.Wait([&] { return f.model_released == 1; }));
  Q4T_CHECK(f.results.size() == 1 &&
            f.results[0].state == RealtimeResponseEnd::State::kCancelled);
  Q4T_CHECK(f.results[0].usage && f.results[0].usage->total.input == 11);
  Q4T_CHECK(!f.early_release && !f.overlap && !f.model_alive);
  Q4T_CHECK(!f.frontend->PlayFixture(Base64("abcd"), "second").ok());
  Q4T_CHECK(f.Count("session.output_audio.fixture.playing") == 1);
  now += 20ms;
  Q4T_CHECK(f.frontend->Tick().ok() && !f.frontend->FixtureActive());
  Q4T_CHECK(f.Count("session.output_audio.fixture.played") == 1 &&
            f.models == 1);
  Q4T_CHECK(f.frontend->Close().ok() &&
            f.Wait([&] { return f.frontend->Drained(); }));
  return true;
}

Q4T_TEST(live_fixture_waits_asr_borrower_and_pauses_new_model_work) {
  Fixture f;
  auto now = Clock::now();
  f.native.now = [&] { return now; };
  f.hold_asr = true;
  Q4T_CHECK(f.Start().ok() && f.Tone());
  Q4T_CHECK(f.Wait([&] { return f.asr_alive.load(); }));
  Q4T_CHECK(f.frontend->PlayFixture(Base64(std::string(960, '\x15'))).ok());
  Q4T_CHECK(f.frontend->AppendAudio(Base64(std::string(24000, '\x42'))).ok());
  now += 1s;
  for (int i = 0; i < 20; ++i) Q4T_CHECK(f.frontend->Tick().ok());
  Q4T_CHECK(f.asr == 1 && f.asr_released == 0 && f.models == 0);
  Q4T_CHECK(!f.Count("session.output_audio.delta"));
  f.hold_asr = false;
  Q4T_CHECK(f.Wait([&] { return f.asr_released == 1; }));
  Q4T_CHECK(f.frontend->FixtureActive() &&
            !f.Count("session.output_audio.fixture.played"));
  now += 20ms;
  Q4T_CHECK(f.frontend->Tick().ok() && !f.frontend->FixtureActive());
  Q4T_CHECK(f.frontend->Tick().ok() && f.asr == 1 && f.models == 0);
  Q4T_CHECK(!f.overlap && !f.early_release);
  Q4T_CHECK(f.frontend->Close().ok() &&
            f.Wait([&] { return f.frontend->Drained(); }));
  return true;
}

Q4T_TEST(live_fixture_drops_input_preserves_user_mute_and_resumes) {
  Fixture f;
  auto now = Clock::now();
  f.native.now = [&] { return now; };
  f.native.input.vad_step = [](auto samples, auto*, float* probability) {
    *probability =
        std::ranges::any_of(
            samples, [](float value) { return value > .01f || value < -.01f; })
            ? 1.f
            : 0.f;
    return q4t::Status{};
  };
  std::string captured;
  auto execute = f.native.input.execute;
  f.native.input.execute = [&](const auto& job, const auto& spans,
                               const auto& control, const auto& emit) {
    for (auto span : spans)
      captured.append(reinterpret_cast<const char*>(span.data()), span.size());
    return execute(job, spans, control, emit);
  };
  Q4T_CHECK(f.Start().ok() && f.frontend->Mute(true, "user_mute").ok());
  Q4T_CHECK(f.frontend->AppendAudio(Base64("\x42")).ok());
  Q4T_CHECK(f.frontend->PlayFixture(Base64(std::string(960, '\x33'))).ok());
  Q4T_CHECK(f.frontend->AppendAudio(Base64(std::string(24000, '\x42'))).ok());
  Q4T_CHECK(f.frontend->Tick().ok());
  now += 20ms;
  Q4T_CHECK(f.frontend->Tick().ok() && !f.frontend->FixtureActive());
  std::string speech(6000, '\x15');
  speech.append(18000, '\0');
  Q4T_CHECK(f.frontend->AppendAudio(Base64(speech)).ok());
  for (int i = 0; i < 60; ++i) Q4T_CHECK(f.frontend->Tick().ok());
  Q4T_CHECK(f.asr == 0 && f.models == 0);
  Q4T_CHECK(f.Count("session.input_audio.muted") == 1 &&
            !f.Count("session.input_audio.unmuted"));
  Q4T_CHECK(f.frontend->Mute(false, "user_unmute").ok());
  Q4T_CHECK(f.frontend->AppendAudio(Base64(speech)).ok());
  Q4T_CHECK(f.Wait([&] { return f.model_released == 1; }));
  Q4T_CHECK(f.asr == 1 && f.asr_released == 1);
  Q4T_CHECK(captured.find('\x15') != std::string::npos);
  Q4T_CHECK(captured.find('\x42') == std::string::npos &&
            captured.find('\x33') == std::string::npos);
  Q4T_CHECK(f.frontend->Close().ok() &&
            f.Wait([&] { return f.frontend->Drained(); }));
  Q4T_CHECK(f.asr == 1 && f.models == 1);
  return true;
}

Q4T_TEST(live_fixture_context_rejection_is_atomic_and_ack_waits) {
  ContextFixture c;
  c.f.native.now = [&] { return c.now; };
  Q4T_CHECK(c.Start() && c.context->AcknowledgementDelay(5).ok());
  Q4T_CHECK(c.Delegate());
  const auto history = c.f.frontend->HistoryItems();
  const auto counters = c.count_calls;
  Q4T_CHECK(c.f.frontend->PlayFixture(Base64(std::string(960, '\x15'))).ok());
  Q4T_CHECK(c.f.frontend->Tick().ok());
  c.now += 10ms;
  for (auto kind : {"instructions", "thinking", "commentary"})
    Q4T_CHECK(!c.Append(kind, "Do not accept during fixture").ok());
  Q4T_CHECK(c.f.frontend->HistoryItems() == history &&
            c.count_calls == counters);
  Q4T_CHECK(c.context->Tick().ok() && c.f.frontend->QueuedTurns() == 0);
  Q4T_CHECK(c.f.models == 1 && c.f.frontend->FixtureActive());
  c.now += 10ms;
  Q4T_CHECK(c.f.frontend->Tick().ok() && !c.f.frontend->FixtureActive());
  Q4T_CHECK(c.context->Tick().ok() && c.f.frontend->QueuedTurns() == 1);
  Q4T_CHECK(c.f.Wait([&] { return c.f.completions == 2; }));
  Q4T_CHECK(c.f.models == 2 &&
            c.f.Count("session.output_audio.fixture.played") == 1);
  return true;
}

Q4T_TEST(live_fixture_native_wire_formats_and_normal_close_are_real) {
  for (const auto& format :
       {Object({{"type", String("audio/pcm")}, {"rate", Number(24000)}}),
        Object({{"type", String("audio/pcm")}, {"rate", Number(16000)}}),
        Object({{"type", String("audio/pcmu")}, {"rate", Number(8000)}}),
        Object({{"type", String("audio/pcma")}, {"rate", Number(8000)}})}) {
    NativeFixture f;
    Q4T_CHECK(f.Start(Object({{"model", String("q4t-test")},
                              {"audio", Object({{"format", format}})}})));
    Q4T_CHECK(
        f.Send(Object({{"type", String("session.output_audio.fixture.play")},
                       {"audio", String(Base64(std::string(960, '\x15')))},
                       {"event_id", String("native_fixture")}})));
    Q4T_CHECK(f.Close());
    Q4T_CHECK(f.Count("session.output_audio.fixture.playing") == 1 &&
              f.Count("session.output_audio.fixture.played") == 1);
    std::string output;
    for (const auto& event : f.events) {
      if (event.GetString("type") != "session.output_audio.delta") continue;
      std::string bytes;
      Q4T_CHECK(
          DecodeRealtimeAudioBase64(event.GetString("delta"), &bytes).ok());
      output += bytes;
    }
    const auto width = format.GetString("type") == "audio/pcm" ? 2 : 1;
    Q4T_CHECK(output.size() == size_t(format.GetInt("rate") / 50 * width));
    Q4T_CHECK(f.events.back().GetString("type") == "session.closed");
    Q4T_CHECK(f.Last("session.output_audio.fixture.played")
                  .GetString("client_event_id") == "native_fixture");
    Q4T_CHECK(f.f.asr == 0 && f.f.models == 0 && !f.Count("error"));
  }
  return true;
}

Q4T_TEST(live_fixture_disconnect_has_no_played_or_closed_claim) {
  NativeFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(
      f.Send(Object({{"type", String("session.output_audio.fixture.play")},
                     {"audio", String(Base64(std::string(48000, '\x15')))}})));
  Q4T_CHECK(f.session->Tick() && f.Read());
  Q4T_CHECK(f.Count("session.output_audio.fixture.playing") == 1 &&
            f.Count("session.output_audio.delta") > 0);
  f.session->Cancel();
  f.session->Join();
  Q4T_CHECK(!f.session->Finalized());
  Q4T_CHECK(!f.Count("session.output_audio.fixture.played") &&
            !f.Count("session.closed"));
  Q4T_CHECK(f.f.asr == 0 && f.f.models == 0);
  return true;
}

namespace {
Json WorkerConfig() {
  return Object(
      {{"model", String("q4t-test")},
       {"delegation",
        Object({{"type", String("responses")},
                {"acknowledgement_delay_ms", Number(600000)},
                {"responses",
                 Object({{"model", String("q4t-test")},
                         {"instructions",
                          String("private worker instructions")}})}})}});
}
class DelegateFixture {
 public:
  std::atomic<unsigned> runs{0}, releases{0};
  std::atomic<bool> hold{false}, entered{false}, exited{false};
  std::vector<Json> requests;
  bool lookup = true;
  NativeFixture f;
  DelegateFixture() {
    f.responses.acquire = f.f.native.acquire;
    f.responses.release = [this](const auto& control) {
      f.f.requests.Remove(control);
      ++releases;
    };
    f.responses.execute = [this](const Json& settings, Json history,
                                 const auto& control, auto sink) {
      const auto index = ++runs;
      requests.push_back(std::move(history));
      entered = true;
      while (hold && !control->Cancelled()) std::this_thread::sleep_for(1ms);
      LiveResponsesResult result;
      if (control->Cancelled()) {
        result.reply = Error(409, control->Reason(), "Cancelled CPU Responses");
        exited = true;
        return result;
      }
      NativeResponsesStream capture(std::move(sink));
      ResponsesOutput output;
      std::string wire;
      if (!output.Init(settings, "resp_native_" + std::to_string(index), 1,
                       false) ||
          !capture.Begin() || !output.Start(&wire) || !capture.Append(wire))
        throw std::runtime_error("CPU stream start");
      ResponseDelta delta;
      if (lookup && index == 1) {
        delta.kind = ResponseDelta::Kind::kTool;
        delta.tool_start = true;
        delta.call = {0, "call_read", "read_project", "{}"};
      } else {
        delta.kind = ResponseDelta::Kind::kContent;
        delta.text = "rev426";
      }
      if (!output.Append(delta, "null", &wire) || !capture.Append(wire) ||
          !output.Finish(false, 1, {12, 0, 7, 0, 0}, 2, &wire) ||
          !capture.Append(wire))
        throw std::runtime_error("CPU stream finish");
      capture.Seal();
      result.reply = capture.Error();
      result.terminal = capture.Response();
      exited = true;
      return result;
    };
  }
  bool Item(Json item) {
    return f.Send(Object(
        {{"type", String("response.item.create")}, {"item", std::move(item)}}));
  }
};
}  // namespace
Q4T_TEST(live_bound_responses_immediate_create_tool_result_and_spoken_return) {
  DelegateFixture d;
  Q4T_CHECK(d.f.Start(WorkerConfig()) && d.f.Count("session.started") == 1);
  Q4T_CHECK(d.Item(User("Find the project version")) &&
            d.f.Command("response.create"));
  Q4T_CHECK(d.f.Wait([&] { return d.releases.load() == 1; }));
  Q4T_CHECK(d.f.Count("session.delegation.created") == 1 &&
            d.f.Count("error") == 0 && d.f.f.models == 0);
  Q4T_CHECK(d.f.Last("session.delegation.created")
                .Find("delegation")
                ->GetString("response_id") == "resp_native_1");
  Q4T_CHECK(d.f.Command("response.create"));
  Q4T_CHECK(d.f.Last("error").Find("error")->GetString("code") ==
            "function_call_outputs_required");
  Q4T_CHECK(d.Item(Object({{"type", String("function_call_output")},
                           {"call_id", String("call_read")},
                           {"output", String("rev426")}})));
  Q4T_CHECK(d.f.Command("response.create") &&
            d.f.Wait([&] { return d.f.f.model_released.load() == 1; }));
  Q4T_CHECK(d.runs == 2 && d.releases == 2 && d.requests[1].array.size() == 3);
  Q4T_CHECK(d.requests[1].array.back().GetString("output") == "rev426" &&
            d.requests[1].array[1].GetString("call_id") == "call_read");
  Q4T_CHECK(d.f.f.snapshots.size() == 1 &&
            d.f.f.snapshots[0].response.GetString("tool_choice") == "none");
  const auto voice = OutputJson(d.f.f.snapshots[0].snapshot.items);
  Q4T_CHECK(voice.find("rev426") != voice.npos &&
            voice.find("private worker instructions") == voice.npos);
  Q4T_CHECK(d.f.Close() && d.f.Count("session.delegation.created") == 2);
  return true;
}
Q4T_TEST(live_fork_normal_close_captures_separate_tool_and_frontend_history) {
  const auto* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
  auto directory =
      std::filesystem::absolute(root ? root : ".q4t-work/fork-cpu");
  std::filesystem::create_directories(directory);
  ObjectStoreOptions options;
  options.directory =
      (directory / NewObjectId(ObjectKind::kLiveForkState)).string();
  ObjectStore objects(options);
  DelegateFixture d;
  d.f.recording_objects = &objects;
  auto config = WorkerConfig();
  Set(&config, "store", Boolean(true));
  Q4T_CHECK(d.f.Start(config) && d.f.Count("session.started") == 1);
  Q4T_CHECK(d.f.Append("thinking", "private frontend only"));
  Q4T_CHECK(d.Item(User("look up project")) && d.f.Command("response.create"));
  Q4T_CHECK(d.f.Wait([&] { return d.releases.load() == 1; }));
  Q4T_CHECK(d.Item(Object({{"type", String("function_call_output")},
                           {"call_id", String("call_read")},
                           {"output", String("rev426")}})));
  // A submitted tool result belongs to history even when no continuation ran.
  Q4T_CHECK(d.f.Close() && d.runs == 1 && !d.f.Count("error"));
  auto state = ReadLiveForkState(
      objects, {"client:test", Clock::now() + 3s, {}}, "live_bound");
  Q4T_CHECK(state.ok());
  const auto* response = state.value.Find("responses");
  Q4T_CHECK(
      response && response->GetArray("history") &&
      OutputJson(*response->GetArray("history")).find("look up project") !=
          std::string::npos);
  const auto* turn = response->Find("turn");
  Q4T_CHECK(turn && turn->GetBool("completed") &&
            turn->GetArray("output")->array.front().GetString("call_id") ==
                "call_read" &&
            turn->GetArray("results")->array.front().GetString("output") ==
                "rev426");
  Q4T_CHECK(OutputJson(*response).find("private frontend only") ==
                std::string::npos &&
            OutputJson(*state.value.GetArray("frontend_history"))
                    .find("private frontend only") != std::string::npos);
  Q4T_CHECK(!state.value.Has("worker") && d.runs == 1 && d.releases == 1);
  return true;
}

Q4T_TEST(live_bound_spoken_task_uses_responses_target_and_separate_context) {
  DelegateFixture d;
  d.lookup = false;
  d.f.f.delegate_next = true;
  Q4T_CHECK(d.f.Start(WorkerConfig()) && d.f.Tone());
  Q4T_CHECK(d.f.Wait([&] { return d.f.f.model_released.load() >= 2; }));
  Q4T_CHECK(d.runs == 1 && d.releases == 1 &&
            d.f.Count("session.delegation.created") == 1);
  const auto delegated =
      *d.f.Last("session.delegation.created").Find("delegation");
  Q4T_CHECK(delegated.GetString("target") == "responses" &&
            delegated.GetString("response_id") == "resp_native_1");
  Q4T_CHECK(d.requests[0].array.size() == 1 &&
            OutputJson(d.requests[0]).find("Read project resources") !=
                std::string::npos);
  Q4T_CHECK(d.f.Append("thinking", "bad id", delegated.GetString("id")) &&
            d.f.Count("error") == 1);
  Q4T_CHECK(d.f.Close());
  return true;
}
Q4T_TEST(live_bound_fixture_interrupts_owned_backend_before_playing) {
  DelegateFixture d;
  d.hold = true;
  Q4T_CHECK(d.f.Start(WorkerConfig()) && d.Item(User("Hold")) &&
            d.f.Command("response.create"));
  Q4T_CHECK(d.f.Wait([&] { return d.entered.load(); }));
  Q4T_CHECK(
      d.f.Send(Object({{"type", String("session.output_audio.fixture.play")},
                       {"audio", String(Base64(std::string(960, 'x')))}})));
  Q4T_CHECK(d.f.Wait(
      [&] { return d.f.Count("session.output_audio.fixture.played") == 1; }));
  Q4T_CHECK(d.exited && d.releases == 1 &&
            d.f.Count("session.delegation.created") == 0 && d.f.f.models == 0);
  Q4T_CHECK(d.f.Close());
  return true;
}

namespace {
Json DecisionAdd(Json item, std::string event = "add") {
  return Object({{"type", String("decision.item.create")},
                 {"event_id", String(std::move(event))},
                 {"item", std::move(item)}});
}
Json DecisionDelete(std::string id) {
  return Object({{"type", String("decision.item.delete")},
                 {"item_id", String(std::move(id))},
                 {"event_id", String("del")}});
}
}  // namespace

Q4T_TEST(live_decision_items_preserve_typed_input_order_and_snapshot) {
  LiveDecisionItems items;
  std::vector<Json> published;
  const LiveEventSink emit = [&](Json event) {
    published.push_back(std::move(event));
    return ObjectReply{};
  };
  auto first = User("项目版本");
  Set(&first, "id", Json{});
  Q4T_CHECK(items.Apply(DecisionAdd(first), emit).ok());
  const auto generated = published.back().Find("item")->GetString("id");
  Q4T_CHECK(!generated.empty() &&
            published.back().GetString("client_event_id") == "add");
  auto second = Object(
      {{"role", String("user")},
       {"id", String("input-2")},
       {"content",
        Array({Object({{"type", String("input_json")},
                       {"data", Object({{"verified", Boolean(true)}})}})})}});
  Q4T_CHECK(items.Apply(DecisionAdd(second), emit).ok());
  const auto before = items.Snapshot();
  Q4T_CHECK(before.array.size() == 2);
  Q4T_CHECK(before.array[0].GetString("id") == generated &&
            OutputJson(before.array[1]) == OutputJson(second));
  Q4T_CHECK(items.Apply(DecisionDelete(generated), emit).ok());
  Q4T_CHECK(published.back().GetString("type") == "decision.item.deleted" &&
            published.back().GetString("client_event_id") == "del");
  const auto after = items.Snapshot();
  Q4T_CHECK(after.array.size() == 1 && before.array.size() == 2 &&
            OutputJson(after.array[0]) == OutputJson(second));
  return true;
}

Q4T_TEST(live_decision_items_keep_owner_and_do_not_start_model_work) {
  NativeFixture a, b;
  Q4T_CHECK(a.Start() && b.Start());
  auto call = Object({{"id", String("my-call")},
                      {"type", String("function_call")},
                      {"call_id", String("call_1")},
                      {"name", String("lookup")},
                      {"arguments", String("{}")}});
  Q4T_CHECK(a.Send(DecisionAdd(call)));
  Q4T_CHECK(a.Count("decision.item.created") == 1 &&
            OutputJson(*a.Last("decision.item.created").Find("item")) ==
                OutputJson(call));
  Q4T_CHECK(a.Send(DecisionAdd(call, "duplicate")) &&
            a.Last("error").GetString("client_event_id") == "duplicate");
  Q4T_CHECK(b.Send(DecisionDelete("my-call")) &&
            b.Last("error").Find("error")->GetString("code") ==
                "item_not_found");
  Q4T_CHECK(a.Send(DecisionDelete("my-call")) &&
            a.Count("decision.item.deleted") == 1);
  Q4T_CHECK(a.f.models == 0 && b.f.models == 0 && a.f.asr == 0 &&
            b.f.asr == 0 && a.Close() && b.Close());
  return true;
}

Q4T_TEST(live_decision_items_recover_capacity_and_reject_uncertain_writes) {
  LiveDecisionItems items;
  size_t events = 0;
  const LiveEventSink emit = [&](Json) {
    ++events;
    return ObjectReply{};
  };
  auto add = [&](std::string id, std::string content) {
    auto item = User(std::move(content));
    Set(&item, "id", String(std::move(id)));
    return items.Apply(DecisionAdd(std::move(item)), emit);
  };
  Q4T_CHECK(add("oversize", std::string(8ULL << 20, 'a')).http_status == 429);
  for (size_t i = 0; i < 128; ++i) Q4T_CHECK(add(std::to_string(i), "短").ok());
  Q4T_CHECK(add("over", "full").http_status == 429 && events == 128);
  Q4T_CHECK(items.Apply(DecisionDelete("0"), emit).ok() &&
            add("over", "ok").ok());
  const auto failed = items.Apply(DecisionDelete("over"), [](Json) {
    return Error(503, "sink_failed", "Wire delivery failed");
  });
  Q4T_CHECK(failed.commit_unknown && items.Snapshot().array.size() == 127);
  Q4T_CHECK(items.Apply(DecisionDelete("over"), emit).code == "item_not_found");
  return true;
}

Q4T_TEST(live_decision_items_validate_tool_identifiers_and_image_sources) {
  NativeFixture f;
  Q4T_CHECK(f.Start());
  auto call = Object({{"type", String("function_call")},
                      {"name", String("a.b")},
                      {"call_id", String("call")},
                      {"arguments", String("{}")}});
  Q4T_CHECK(f.Send(DecisionAdd(call)));
  const std::string unicode(62, 'x');
  Set(&call, "call_id", String(unicode + "中文"));
  Q4T_CHECK(f.Send(DecisionAdd(call)));
  Set(&call, "call_id", String(unicode + "中文字"));
  Q4T_CHECK(f.Send(DecisionAdd(call)) &&
            f.Last("error").Find("error")->GetString("param") ==
                "item.call_id");
  Set(&call, "call_id", String("id"));
  Set(&call, "namespace", String("a.b"));
  Q4T_CHECK(f.Send(DecisionAdd(call)) &&
            f.Last("error").Find("error")->GetString("param") ==
                "item.namespace");
  auto custom = Object({{"type", String("custom_tool_call")},
                        {"name", String("a.b")},
                        {"call_id", String("id")},
                        {"input", String("input")}});
  Q4T_CHECK(f.Send(DecisionAdd(custom)) &&
            f.Last("error").Find("error")->GetString("param") == "item.name");
  auto result =
      Object({{"type", String("function_call_output")},
              {"call_id", String("id")},
              {"output",
               Array({Object({{"type", String("input_image")},
                              {"image_url", String("file:///invalid")}})})}});
  Q4T_CHECK(f.Send(DecisionAdd(result)) &&
            f.Last("error").Find("error")->GetString("param") ==
                "item.output[0].image_url");
  Set(&Field(result, "output").array[0], "image_url",
      String("https://example.org/image"));
  Q4T_CHECK(f.Send(DecisionAdd(result)) &&
            f.Count("decision.item.created") == 3);
  Q4T_CHECK(f.f.models == 0 && f.Close());
  return true;
}

namespace {
struct DecisionFixture {
  std::atomic<size_t> starts{0}, releases{0};
  std::atomic<bool> hold{false}, fail{false};
  std::vector<Json> requests;
  NativeFixture f;
  DecisionFixture() {
    f.decisions.acquire = f.f.native.acquire;
    f.decisions.release = [this](const auto& control) {
      f.f.requests.Remove(control);
      ++releases;
    };
    f.decisions.execute = [this](const Json& request, const auto& control) {
      requests.push_back(request);
      ++starts;
      while (hold && !control->Cancelled()) std::this_thread::sleep_for(1ms);
      if (control->Cancelled())
        return Error(409, control->Reason(), "cancelled");
      if (fail) return Error(429, "client_rpm_exhausted", "Rate limit");
      ObjectReply reply;
      auto answers = Array();
      for (const auto& q : request.Find("questions")->array)
        answers.array.push_back(Object({{"type", String("predicate")},
                                        {"name", *q.Find("name")},
                                        {"probability", Number(1)}}));
      if (!BuildDecisionResult(request, answers, {17, answers.array.size()},
                               &reply.value))
        return Error(500, "fixture_failed", "Decision fixture failed");
      return reply;
    };
  }
  Json Questions(std::string name) {
    return Array({Object({{"name", String(std::move(name))},
                          {"type", String("predicate")},
                          {"instructions", String("Check project data")}})});
  }
  Json Config(bool delegate, bool triggers = false) {
    auto result = Object(
        {{"model", String("q4t-test")},
         {"input",
          Array({Object(
              {{"role", String("user")},
               {"content",
                Array({Object({{"text", String("startup record")}})})}})})}});
    if (delegate)
      Set(&result, "delegation",
          Object(
              {{"type", String("decisions")},
               {"acknowledgement_delay_ms", Number(600000)},
               {"decisions", Object({{"model", String("q4t-test")},
                                     {"questions", Questions("manual")}})}}));
    if (triggers) {
      auto list = Array();
      for (const auto* name : {"input", "output"})
        list.array.push_back(Object(
            {{"type", String("decisions")},
             {"event_type",
              String(std::string("session.") + name + "_transcript.delta")},
             {"decisions", Object({{"questions", Questions(name)}})}}));
      Set(&result, "triggers", std::move(list));
    }
    return result;
  }
  bool Create(std::string id) {
    return f.Send(Object({{"type", String("decision.create")},
                          {"event_id", String(std::move(id))}}));
  }
};
}  // namespace

Q4T_TEST(
    live_decision_manual_snapshots_match_client_id_and_support_linked_context) {
  DecisionFixture d;
  Q4T_CHECK(d.f.Start(d.Config(true)) && d.f.Count("session.started") == 1);
  Q4T_CHECK(
      d.f.Send(Object({{"type", String("decision.item.create")},
                       {"item", Object({{"id", String("version")},
                                        {"role", String("user")},
                                        {"content", String("rev426")}})}})));
  Q4T_CHECK(d.Create("review") && d.f.Count("session.delegation.created") == 1);
  const auto event = d.f.Last("session.delegation.created");
  const auto id = event.Find("delegation")->GetString("id");
  Q4T_CHECK(event.GetString("client_event_id") == "review" &&
            event.Find("delegation")->GetString("target") == "decisions" &&
            !event.Find("delegation")->Has("response_id"));
  Q4T_CHECK(d.f.Send(Object({{"type", String("decision.item.delete")},
                             {"item_id", String("version")}})));
  Q4T_CHECK(d.Create("review") && d.f.Last("error").Find("error")->GetString(
                                      "code") == "duplicate_event_id");
  Q4T_CHECK(d.f.Wait([&] { return d.releases == 1; }));
  const auto decision = d.f.Last("decision.event");
  Q4T_CHECK(decision.GetString("delegation_id") == id &&
            decision.Find("trigger_id")->IsNull() &&
            decision.Find("source_event_id")->IsNull() &&
            !decision.Has("client_event_id") &&
            decision.Find("event")
                    ->Find("decision")
                    ->Find("usage")
                    ->GetInt("total_tokens") == 18);
  Q4T_CHECK(d.requests[0].Find("input")->array.size() == 2 &&
            d.requests[0].Find("input")->array.back().GetString("content") ==
                "rev426" &&
            d.f.f.models == 0);
  Q4T_CHECK(d.f.Append("thinking", "reviewed", id) &&
            d.f.Count("session.thinking.appended") == 1);
  Q4T_CHECK(d.f.Append("thinking", "wrong", "foreign") &&
            d.f.Count("error") == 2);
  Q4T_CHECK(d.Create("next") && d.f.Close() && d.releases == 2 &&
            d.requests[1].Find("input")->array.size() == 1);
  return true;
}

Q4T_TEST(live_decision_triggers_link_to_actual_transcripts_in_client_mode) {
  DecisionFixture d;
  Q4T_CHECK(d.f.Start(d.Config(false, true)) && d.Create("wrong-mode") &&
            d.f.Last("error").Find("error")->GetString("code") ==
                "invalid_delegation_mode");
  Q4T_CHECK(d.f.Tone() && d.f.Wait([&] { return d.releases == 2; }));
  Q4T_CHECK(d.f.Count("session.delegation.created") == 0 &&
            d.f.Count("decision.event") == 2);
  std::set<std::string> ids;
  for (const auto& event : d.f.events) {
    if (event.GetString("type") != "decision.event") continue;
    const auto source = d.f.Last(event.GetString("source_event_type"));
    Q4T_CHECK(!source.IsNull() &&
              event.GetString("source_event_id") ==
                  source.GetString("event_id") &&
              !event.GetString("trigger_id").empty() &&
              ids.insert(event.GetString("trigger_id")).second &&
              event.Find("delegation_id")->IsNull());
  }
  Q4T_CHECK(d.requests[0].Find("input")->array.back().GetString("content") ==
                "read project" &&
            d.requests[1].Find("input")->array.back().GetString("content") ==
                "answer" &&
            d.f.f.models == 1);
  Q4T_CHECK(d.f.Close());
  return true;
}

Q4T_TEST(live_decision_voice_handoff_never_reports_unconfirmed_results) {
  DecisionFixture d;
  d.f.f.delegate_next = true;
  Q4T_CHECK(d.f.Start(d.Config(true)) && d.f.Tone() &&
            d.f.Wait([&] { return d.releases == 1; }));
  const auto created = d.f.Last("session.delegation.created");
  Q4T_CHECK(created.Find("delegation")->GetString("target") == "decisions" &&
            d.f.Last("decision.event").GetString("delegation_id") ==
                created.Find("delegation")->GetString("id"));
  Q4T_CHECK(d.f.f.models == 1 &&
            d.f.Count("session.output_transcript.delta") == 0 &&
            OutputJson(d.requests[0]).find("Read project resources") !=
                std::string::npos);
  Q4T_CHECK(d.f.Append("commentary", "The version is confirmed",
                       created.Find("delegation")->GetString("id")) &&
            d.f.Wait([&] { return d.f.f.model_released == 2; }) &&
            d.f.Count("session.output_transcript.delta") == 1);
  Q4T_CHECK(d.f.Close());
  return true;
}

Q4T_TEST(live_decision_bad_update_or_failed_worker_preserves_healthy_session) {
  DecisionFixture d;
  Q4T_CHECK(d.f.Start(d.Config(true)));
  auto patch = Object({{"type", String("decisions")},
                       {"decisions", Object({{"questions", Array()}})}});
  Q4T_CHECK(d.f.Send(Object({{"type", String("session.update")},
                             {"session", Object({{"delegation", patch}})}})));
  Q4T_CHECK(d.f.Count("error") == 1 && d.f.Count("session.updated") == 0);
  d.fail = true;
  Q4T_CHECK(d.Create("limited") && d.f.Wait([&] { return d.releases == 1; }));
  Q4T_CHECK(d.f.Last("error").GetString("client_event_id") == "limited" &&
            d.f.Last("error").Find("error")->GetString("code") ==
                "client_rpm_exhausted" &&
            d.f.Count("decision.event") == 0);
  d.fail = false;
  Q4T_CHECK(d.Create("retry_new") &&
            d.f.Wait([&] { return d.releases == 2; }) &&
            d.f.Count("decision.event") == 1 && d.f.Close());
  return true;
}

Q4T_TEST(
    live_decision_disconnect_releases_running_work_and_discards_pending_jobs) {
  DecisionFixture d;
  d.hold = true;
  Q4T_CHECK(d.f.Start(d.Config(true)) && d.Create("first") &&
            d.f.Wait([&] { return d.starts == 1; }));
  for (auto id : {"two", "three", "four", "overflow"}) Q4T_CHECK(d.Create(id));
  Q4T_CHECK(d.f.Last("error").Find("error")->GetString("code") ==
            "live_decision_capacity");
  d.f.session->Cancel();
  d.f.session->Join();
  Q4T_CHECK(d.starts == 1 && d.releases == 1 &&
            d.f.Count("decision.event") == 0 && !d.f.session->Finalized());
  return true;
}

Q4T_TEST(live_decision_serializes_same_session_voice_and_classification_work) {
  DecisionFixture d;
  d.hold = true;
  Q4T_CHECK(d.f.Start(d.Config(true)) && d.Create("first") &&
            d.f.Wait([&] { return d.starts == 1; }));
  Q4T_CHECK(d.f.Append("commentary", "Speak after evaluation") &&
            d.f.f.models == 0);
  for (int i = 0; i < 5; ++i) Q4T_CHECK(d.f.session->Tick() && d.f.Read());
  Q4T_CHECK(d.f.f.models == 0);
  d.hold = false;
  Q4T_CHECK(d.f.Wait([&] { return d.f.f.model_released == 1; }) &&
            d.releases == 1 && d.f.Count("error") == 0 && d.f.Close());
  return true;
}

Q4T_TEST(live_recording_storage_failure_keeps_voice_and_normal_close) {
  ObjectStoreOptions options;
  // A real unusable storage path produces a worker error, independent of ASR.
  options.directory = "/dev/null/q4t-recordings";
  ObjectStore objects(options);
  NativeFixture f;
  f.recording_objects = &objects;
  Q4T_CHECK(f.Start(
      Object({{"model", String("q4t-test")}, {"store", Boolean(true)}})));
  Q4T_CHECK(f.Count("session.started") == 1);
  Q4T_CHECK(f.Wait([&] { return f.Count("error") == 1; }));
  Q4T_CHECK(f.Last("error").Find("error")->GetString("param") ==
            "session.store");
  Q4T_CHECK(f.Tone() && f.Wait([&] { return f.f.model_released == 1; }));
  Q4T_CHECK(f.Count("session.output_audio.delta") && f.f.asr_released == 1);
  Q4T_CHECK(f.Close() && f.Count("error") == 1 &&
            f.Count("session.closed") == 1);
  return true;
}

Q4T_TEST(live_recording_permission_without_session_opt_in_never_opens_storage) {
  ObjectStoreOptions options;
  options.directory = "/dev/null/q4t-recordings";
  ObjectStore objects(options);
  NativeFixture f;
  f.recording_objects = &objects;
  Q4T_CHECK(f.Start() && f.Count("session.started") == 1 &&
            !f.Last("session.started").Find("session")->GetBool("store"));
  Q4T_CHECK(f.Close() && !f.Count("error") &&
            objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(live_media_native_plan_uses_private_pcm_without_exposing_wire_format) {
  NativeFixture f;
  ChatPlanError error;
  Json resolved;
  NativeLivePlan plan;
  auto context = f.callbacks.configuration;
  context.transport = LiveTransport::kWebRtc;
  context.validate = [&](const Json& config, ChatPlanError* failure) {
    return PlanNativeLiveClient(config, f.planning, &plan, failure, true, true,
                                false, context.transport);
  };
  auto request = Object({{"model", String("q4t-test")}});
  Q4T_CHECK(ResolveLiveConfiguration(request, context, &resolved, &error));
  Q4T_CHECK(!resolved.Find("audio")->Has("format") &&
            plan.format.encoding == RealtimeAudioEncoding::kPcm16 &&
            plan.format.rate == 24000);
  Q4T_CHECK(!PlanNativeLiveClient(resolved, f.planning, &plan, &error));
  Set(&Field(resolved, "audio"), "format",
      Object({{"type", String("audio/pcm")}, {"rate", Number(24000)}}));
  Q4T_CHECK(!PlanNativeLiveClient(resolved, f.planning, &plan, &error, true,
                                  true, false, context.transport) &&
            error.param == "session.audio.format");
  return true;
}
