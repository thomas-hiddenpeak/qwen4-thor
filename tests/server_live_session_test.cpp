#include "q4t/server/live_session.h"
#include "q4t/server/live_media_call.h"
#include "q4t/io/base64.h"

#include <algorithm>
#include <stdexcept>
#include <thread>

#include "q4t/server/live_responses_settings.h"

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using q4t::Status;
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("Live lifecycle test JSON");
  return value;
}
struct BackendState {
  size_t created = 0, updates = 0, closes = 0, cancelled = 0, joined = 0;
  bool reject_start = false, unknown_start = false, reject_update = false;
  bool drained = false, ignore_output_failure = false;
  Json configuration;
  std::vector<Json> commands, outgoing;
  std::vector<bool> ready;
  LiveEventSink emit;
};
class Backend final : public ILiveSessionBackend {
 public:
  explicit Backend(BackendState& state) : state_(state) {}
  ObjectReply Update(const Json& next) override {
    ++state_.updates;
    if (state_.reject_update)
      return Error(503, "fixture_backend_unavailable", "Backend unavailable");
    state_.configuration = next;
    return {};
  }
  void ResponsesReady(bool ready) noexcept override {
    state_.ready.push_back(ready);
  }
  ObjectReply Command(const Json& event) override {
    state_.commands.push_back(event);
    const auto type = event.GetString("type");
    if (type == "session.input_audio.mute" ||
        type == "session.input_audio.unmute") {
      auto output = Object({{"type", String(type + "d")}});
      if (event.Has("event_id"))
        Set(&output, "client_event_id", String(event.GetString("event_id")));
      return state_.emit(std::move(output));
    }
    return {};
  }
  ObjectReply Tick() override {
    auto outgoing = std::move(state_.outgoing);
    for (auto& event : outgoing) {
      const auto result = state_.emit(std::move(event));
      if (!result.ok() && !state_.ignore_output_failure) return result;
    }
    return {};
  }
  ObjectReply Close() override {
    ++state_.closes;
    return {};
  }
  bool Drained() const override { return state_.drained; }
  void Cancel() noexcept override { ++state_.cancelled; }
  void Join() noexcept override { ++state_.joined; }

 private:
  BackendState& state_;
};
struct Fixture {
  WebSocketOutput output;
  Clock::time_point now = Clock::time_point(100s);
  BackendState backend;
  LiveSessionCallbacks callbacks;
  LiveSessionLimits limits;
  std::vector<Json> events;
  std::unique_ptr<LiveSession> session;
  explicit Fixture(size_t frames = 64) : output(1ULL << 20, frames) {
    callbacks.configuration.default_voice = String("vivian");
    callbacks.configuration.default_instructions = "Native conversation";
    callbacks.configuration.count_text = [](auto, size_t* count, auto*) {
      *count = 4;
      return Status{};
    };
    callbacks.configuration.count_history = [](const auto&, size_t* count,
                                               auto*) {
      *count = 0;
      return Status{};
    };
    callbacks.configuration.validate = [](const auto&, auto*) {
      return Status{};
    };
    callbacks.now = [this] { return now; };
    callbacks.create = [this](const Json& configuration,
                              const LiveEventSink& emit,
                              std::unique_ptr<ILiveSessionBackend>* out) {
      ++backend.created;
      backend.emit = emit;
      *out = std::make_unique<Backend>(backend);
      if (backend.reject_start || backend.unknown_start)
        return Error(503, "fixture_start_failed", "Startup failed",
                     backend.unknown_start);
      backend.configuration = configuration;
      return ObjectReply{};
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
      auto event = Parse(std::string(decoder.Event().payload));
      ChatPlanError error;
      Q4T_CHECK(ValidateLiveWireShape(event, LiveShape::kServerEvent, &error));
      events.push_back(std::move(event));
      output.Acknowledge();
    }
    return true;
  }
  bool Start() {
    session = std::make_unique<LiveSession>("live_fixture", 1795200000, output,
                                            callbacks, limits);
    return session->Start();
  }
  bool Send(const std::string& event, bool read = true) {
    return session->Message(WebSocketOpcode::kText, event) && (!read || Read());
  }
  bool Begin(bool read = true) {
    return Start() && Send(R"({"type":"session.start","event_id":"start_1",
      "session":{"model":"native"}})",
                           read);
  }
  bool Tick() { return session->Tick() && Read(); }
  bool Until(const std::function<bool()>& predicate) {
    const auto deadline = Clock::now() + 3s;
    while (!predicate() && Clock::now() < deadline) {
      if (!Tick()) return false;
      std::this_thread::sleep_for(1ms);
    }
    return predicate();
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
};
}  // namespace

Q4T_TEST(live_session_start_order_and_stable_native_identity) {
  Fixture f;
  Q4T_CHECK(f.Start() && f.output.Bytes() == 0 && f.backend.created == 0);
  Q4T_CHECK(
      f.Send(R"({"type":"session.input_audio.mute","event_id":"early"})"));
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "session_not_started");
  Q4T_CHECK(f.Send(R"({"type":"session.start","event_id":"start_1",
    "session":{"model":"native"}})"));
  Q4T_CHECK(f.backend.created == 1 && f.Count("session.started") == 1);
  const auto started = f.Last("session.started");
  Q4T_CHECK(started.GetString("client_event_id") == "start_1");
  Q4T_CHECK(started.Find("session")->GetString("id") == "live_fixture");
  Q4T_CHECK(started.Find("session")->GetString("status") == "active");
  Q4T_CHECK(started.Find("session")->GetInt("expires_at") == 1795200000);
  Q4T_CHECK(f.Send(R"({"type":"session.start","session":{"model":"native"}})"));
  Q4T_CHECK(f.backend.created == 1 && f.Count("session.started") == 1);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "session_already_started");
  return true;
}

Q4T_TEST(live_session_failed_partial_start_cleans_before_known_retry) {
  Fixture f;
  f.backend.reject_start = true;
  Q4T_CHECK(f.Begin());
  Q4T_CHECK(f.Count("session.started") == 0 && f.Count("error") == 1);
  Q4T_CHECK(f.backend.cancelled == 1 && f.backend.joined == 1);
  f.backend.reject_start = false;
  Q4T_CHECK(f.Send(R"({"type":"session.start","session":{"model":"native"}})"));
  Q4T_CHECK(f.backend.created == 2 && f.Count("session.started") == 1);
  Fixture uncertain;
  uncertain.backend.unknown_start = true;
  Q4T_CHECK(!uncertain.Begin());
  Q4T_CHECK(uncertain.backend.created == 1 &&
            uncertain.backend.cancelled == 1 && uncertain.backend.joined == 1 &&
            !uncertain.session->Finalized());
  Q4T_CHECK(!uncertain.Send(
      R"({"type":"session.start","session":{"model":"native"}})"));
  Q4T_CHECK(uncertain.backend.created == 1);
  return true;
}

Q4T_TEST(live_session_updates_publish_only_after_atomic_backend_acceptance) {
  Fixture f;
  Q4T_CHECK(f.Begin());
  Q4T_CHECK(f.Send(R"({"type":"session.update","session":{"model":"other"}})"));
  Q4T_CHECK(f.backend.updates == 0 && f.Count("session.updated") == 0);
  Q4T_CHECK(f.Send(R"({"type":"session.update","event_id":"update_1","session":{
    "delegation":{"type":"client","acknowledgement_delay_ms":90}}})"));
  Q4T_CHECK(f.backend.updates == 1 && f.Count("session.updated") == 1);
  f.backend.reject_update = true;
  Q4T_CHECK(
      f.Send(R"({"type":"session.update","event_id":"update_bad","session":{
    "delegation":{"type":"client","acknowledgement_delay_ms":5}}})"));
  Q4T_CHECK(f.Count("session.updated") == 1);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("client_event_id") ==
            "update_bad");
  Q4T_CHECK(f.backend.configuration.Find("delegation")
                ->GetInt("acknowledgement_delay_ms") == 90);
  Q4T_CHECK(f.Send(R"({"type":"session.close"})"));
  f.backend.drained = true;
  Q4T_CHECK(f.Tick());
  Q4T_CHECK(f.Last("session.closed")
                .Find("session")
                ->Find("delegation")
                ->GetInt("acknowledgement_delay_ms") == 90);
  return true;
}

Q4T_TEST(live_session_commands_reach_owner_and_bad_events_do_not) {
  Fixture f;
  Q4T_CHECK(f.Begin());
  Q4T_CHECK(
      f.Send(R"({"type":"session.input_audio.mute","event_id":"mute_1"})"));
  Q4T_CHECK(f.Send(R"({"type":"session.input_audio.unmute"})"));
  Q4T_CHECK(f.backend.commands.size() == 2 &&
            f.Count("session.input_audio.muted") == 1 &&
            f.Count("session.input_audio.unmuted") == 1);
  Q4T_CHECK(f.Last("session.input_audio.muted").GetString("client_event_id") ==
            "mute_1");
  Q4T_CHECK(f.Send(R"({"type":"input_audio_buffer.append","audio":"AAAA"})"));
  Q4T_CHECK(f.Send(R"({"type":"session.close","type":"session.close"})"));
  Q4T_CHECK(f.session->Message(WebSocketOpcode::kBinary, "bytes") && f.Read());
  Q4T_CHECK(f.backend.commands.size() == 2 && f.backend.closes == 0 &&
            f.Count("error") == 3);
  return true;
}

Q4T_TEST(live_session_usage_is_monotonic_active_duration_not_sample_sum) {
  Fixture f;
  Q4T_CHECK(f.Begin());
  Q4T_CHECK(f.Send(R"({"type":"session.input_audio.append","audio":"AAAA"})"));
  f.now += 2s;
  Q4T_CHECK(f.Tick());
  Q4T_CHECK(
      f.Last("session.usage.updated").Find("usage")->GetNumber("seconds") == 2);
  Q4T_CHECK(f.Send(R"({"type":"session.input_audio.mute"})"));
  f.now += 3s;
  Q4T_CHECK(f.Tick());
  Q4T_CHECK(
      f.Last("session.usage.updated").Find("usage")->GetNumber("seconds") == 5);
  Q4T_CHECK(f.Count("session.usage.updated") == 2);
  f.now -= 1s;
  Q4T_CHECK(!f.Tick() && !f.session->Finalized() && f.backend.cancelled == 1);
  return true;
}

Q4T_TEST(live_session_close_waits_for_actual_drain_and_final_usage) {
  Fixture f;
  Q4T_CHECK(f.Begin());
  Q4T_CHECK(f.Send(R"({"type":"session.close","event_id":"close_1"})"));
  Q4T_CHECK(f.backend.closes == 1 && !f.session->Finalized());
  Q4T_CHECK(f.Send(R"({"type":"response.create","event_id":"late"})"));
  Q4T_CHECK(f.backend.commands.empty());
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "session_closing");
  f.now += 1250ms;
  Q4T_CHECK(f.Tick() && f.Count("session.closed") == 0 &&
            f.backend.joined == 0);
  f.backend.drained = true;
  Q4T_CHECK(f.Tick() && f.session->Finalized() && f.backend.joined == 1);
  auto closed = f.Last("session.closed");
  Q4T_CHECK(closed.GetString("reason") == "close_requested");
  Q4T_CHECK(closed.GetString("client_event_id") == "close_1");
  Q4T_CHECK(closed.Find("usage")->GetNumber("seconds") == 1.25);
  Q4T_CHECK(closed.Find("session")->GetString("status") == "active");
  f.now += 1s;
  Q4T_CHECK(f.Tick() && f.Count("session.closed") == 1);
  Q4T_CHECK(f.backend.cancelled == 0);
  return true;
}

Q4T_TEST(live_session_disconnect_joins_without_inventing_closed) {
  Fixture f;
  Q4T_CHECK(f.Begin());
  f.session->Cancel();
  f.session->Join();
  f.session->Cancel();
  f.session->Join();
  Q4T_CHECK(!f.Tick() && !f.session->Finalized());
  f.session.reset();
  Q4T_CHECK(f.backend.cancelled == 1 && f.backend.joined == 1);
  Q4T_CHECK(f.Count("session.closed") == 0);
  return true;
}

Q4T_TEST(live_session_expiry_and_drain_timeout_keep_distinct_outcomes) {
  for (const bool drained : {false, true}) {
    Fixture f;
    f.limits.lifetime = 5s;
    f.limits.drain = 1s;
    Q4T_CHECK(f.Begin());
    f.now += 6s;
    f.backend.drained = drained;
    Q4T_CHECK(f.Tick());
    Q4T_CHECK(f.backend.closes == 1 && f.session->Finalized() == drained);
    if (drained) {
      Q4T_CHECK(f.Last("session.closed").GetString("reason") == "expired");
    } else {
      f.now += 1s;
      Q4T_CHECK(!f.Tick() && f.Count("session.closed") == 0);
      Q4T_CHECK(f.backend.cancelled == 1);
    }
  }
  Fixture waiting;
  waiting.limits.startup = 1s;
  Q4T_CHECK(waiting.Start());
  waiting.now += 1s;
  Q4T_CHECK(!waiting.Tick() && waiting.backend.created == 0);
  return true;
}

Q4T_TEST(live_session_backend_cannot_forge_lifecycle_or_ignore_bad_output) {
  for (const auto* raw :
       {R"({"type":"session.closed"})",
        R"({"type":"session.usage.updated","usage":{"seconds":99}})",
        R"({"type":"session.output_audio.delta","delta":12})"}) {
    Fixture f;
    Q4T_CHECK(f.Begin());
    f.backend.outgoing.push_back(Parse(raw));
    f.backend.ignore_output_failure = true;
    Q4T_CHECK(!f.Tick() && !f.session->Finalized());
    Q4T_CHECK(f.Count("session.closed") == 0 &&
              f.Count("session.usage.updated") == 0);
    Q4T_CHECK(f.backend.cancelled == 1);
  }
  return true;
}

Q4T_TEST(
    live_session_output_backpressure_is_bounded_and_no_backend_is_not_success) {
  Fixture f(1);
  f.limits.deferred_events = 2;
  Q4T_CHECK(f.Begin(false));
  const auto event = Parse(R"({"type":"error","error":{
    "code":"fixture","message":"bounded","type":"server_error"}})");
  f.backend.outgoing.assign(3, event);
  Q4T_CHECK(!f.session->Tick() && f.session->DeferredBytes() == 0);
  Q4T_CHECK(!f.session->Finalized() && f.backend.cancelled == 1);
  Fixture missing;
  missing.callbacks.create = {};
  Q4T_CHECK(!missing.Start());
  Fixture absent;
  absent.callbacks.create = [](const Json&, const LiveEventSink&, auto*) {
    return ObjectReply{};
  };
  Q4T_CHECK(absent.Begin() && absent.Count("session.started") == 0);
  Q4T_CHECK(absent.Last("error").Find("error")->GetString("code") ==
            "live_backend_missing");
  return true;
}

Q4T_TEST(live_responses_validation_ack_error_and_corrected_same_session) {
  Fixture f;
  f.callbacks.validate_responses = [](const Json& settings, std::stop_token) {
    ChatPlanConfig config;
    config.model = "native";
    ChatPlanError error;
    const auto status =
        CheckLiveResponsesSettings(settings, config, "client:a", &error);
    if (status) return ObjectReply{};
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  };
  Q4T_CHECK(f.Start());
  Q4T_CHECK(
      f.Send(R"({"type":"session.start","event_id":"bad_skills","session":{
    "model":"native","delegation":{"type":"responses","responses":{"model":"native",
    "tools":[{"type":"shell","environment":{"type":"container_auto","skills":[
    {"type":"skill_reference","skill_id":"sk_1","version":1}]}}]}}}})"));
  Q4T_CHECK(f.events.size() == 1 && f.Count("session.started") == 1);
  Q4T_CHECK(f.Send(R"({"type":"response.create","event_id":"early"})"));
  Q4T_CHECK(f.Count("error") == 0);
  Q4T_CHECK(f.backend.commands.empty());
  Q4T_CHECK(f.Until([&] {
    return f.Last("error").GetString("client_event_id") == "bad_skills";
  }));
  const auto report = *f.Last("error").Find("error");
  Q4T_CHECK(report.GetString("code") == "responses_validation_failed");
  Q4T_CHECK(report.GetString("client_event_id") == "bad_skills");
  Q4T_CHECK(report.GetString("param") ==
            "tools[0].environment.skills[0].version");
  Q4T_CHECK(report.GetString("message").find("string") != std::string::npos);
  Q4T_CHECK(f.backend.configuration.Find("delegation")
                ->Find("responses")
                ->GetArray("tools")
                ->array.size() == 1);
  Q4T_CHECK(f.Send(R"({"type":"session.update","event_id":"fix","session":{
    "delegation":{"type":"responses","responses":{"tools":[]}}}})"));
  Q4T_CHECK(f.Last("session.updated").GetString("client_event_id") == "fix");
  Q4T_CHECK(f.Until([&] { return f.backend.ready.back(); }));
  Q4T_CHECK(f.Send(R"({"type":"response.create","event_id":"good"})"));
  Q4T_CHECK(f.backend.commands.size() == 1 &&
            f.backend.commands[0].GetString("event_id") == "good");
  return true;
}

Q4T_TEST(live_responses_old_validation_cannot_authorize_new_configuration) {
  std::atomic<bool> entered{false}, release{false};
  Fixture f;
  f.callbacks.validate_responses = [&](const Json& settings,
                                       std::stop_token stop) {
    if (settings.GetString("model") == "old") {
      entered.store(true);
      while (!release.load() && !stop.stop_requested())
        std::this_thread::sleep_for(1ms);
      return ObjectReply{};
    }
    throw std::runtime_error("fixture-only-private-marker");
  };
  Q4T_CHECK(f.Start() &&
            f.Send(R"({"type":"session.start","session":{"model":"native",
    "delegation":{"type":"responses","responses":{"model":"old"}}}})"));
  Q4T_CHECK(f.Until([&] { return entered.load(); }));
  Q4T_CHECK(f.Send(R"({"type":"session.update","event_id":"bad","session":{
    "delegation":{"type":"responses","responses":{"model":"new"}}}})"));
  Q4T_CHECK(f.Send(
      R"({"type":"session.input_audio.mute","event_id":"independent"})"));
  Q4T_CHECK(f.Count("session.input_audio.muted") == 1);
  release.store(true);
  Q4T_CHECK(f.Until([&] { return f.Count("error") == 1; }));
  Q4T_CHECK(
      std::ranges::none_of(f.backend.ready, [](bool value) { return value; }));
  Q4T_CHECK(f.Last("error").GetString("client_event_id") == "bad");
  Q4T_CHECK(OutputJson(f.Last("error")).find("private-marker") ==
            std::string::npos);
  return true;
}

Q4T_TEST(live_responses_backpressure_before_commit_and_close_drains_checker) {
  std::atomic<unsigned> calls{0};
  std::atomic<bool> done{false};
  Fixture f;
  f.callbacks.validate_responses = [&](const Json&, std::stop_token stop) {
    ++calls;
    while (!stop.stop_requested()) std::this_thread::sleep_for(1ms);
    done.store(true);
    return ObjectReply{};
  };
  Q4T_CHECK(f.Start() &&
            f.Send(R"({"type":"session.start","session":{"model":"native",
    "delegation":{"type":"responses","responses":{"model":"num0"}}}})"));
  Q4T_CHECK(f.Until([&] { return calls.load() == 1; }));
  for (unsigned i = 1; i <= 8; ++i) {
    const auto event =
        std::string(R"({"type":"session.update","event_id":"update_)") +
        std::to_string(i) +
        R"(","session":{"delegation":{"type":"responses","responses":{"model":"num)" +
        std::to_string(i) + R"("}}}})";
    Q4T_CHECK(f.Send(event));
  }
  Q4T_CHECK(f.Count("session.updated") == 7 && f.backend.updates == 7);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "live_validation_capacity");
  Q4T_CHECK(f.Last("error").GetString("client_event_id") == "update_8");
  Q4T_CHECK(f.Send(R"({"type":"session.close","event_id":"finish"})"));
  f.backend.drained = true;
  Q4T_CHECK(f.Until([&] { return f.session->Finalized(); }));
  Q4T_CHECK(done.load() && calls.load() == 1 && f.Count("session.closed") == 1);
  Q4T_CHECK(f.Last("session.closed")
                .Find("session")
                ->Find("delegation")
                ->Find("responses")
                ->GetString("model") == "num7");
  return true;
}

Q4T_TEST(
    live_sip_accept_uses_dedicated_startup_contract_before_owner_creation) {
  Fixture f;
  f.callbacks.configuration.transport = LiveTransport::kSip;
  Q4T_CHECK(f.Start());
  for (auto extra :
       {R"("client":{"data_channel":{}})",
        R"("audio":{"format":{"type":"audio/pcm","rate":24000}})"}) {
    auto invalid =
        Parse(std::string(R"({"type":"live","model":"native",)") + extra + "}");
    auto reply = f.session->StartMedia(invalid);
    Q4T_CHECK(reply.http_status == 400 && f.backend.created == 0);
  }
  for (const auto invalid :
       {R"({"model":"native"})", R"({"type":"realtime","model":"native"})"}) {
    Q4T_CHECK(f.session->StartMedia(Parse(invalid)).http_status == 400 &&
              f.backend.created == 0);
  }
  Q4T_CHECK(
      f.session
          ->StartMedia(Parse(
              R"({"type":"live","model":"native","audio":{"output":{"voice":"vivian"}}})"))
          .ok());
  Q4T_CHECK(f.backend.created == 1 && !f.backend.configuration.Has("type") &&
            f.Read());
  Q4T_CHECK(!f.session->StartMedia(Parse(R"({"model":"native"})")).ok() &&
            f.backend.created == 1);
  return true;
}

Q4T_TEST(live_media_http_start_returns_errors_and_one_owned_backend) {
  Fixture f;
  f.callbacks.configuration.transport = LiveTransport::kWebRtc;
  const auto sink = [](char, auto) { return true; };
  LiveMediaCall invalid("live_bad", 1000, false, f.callbacks, f.limits, sink);
  auto reply = invalid.Start(Parse(R"({"model":"native","audio":{"format":{
    "type":"audio/pcm","rate":24000}}})"));
  Q4T_CHECK(reply.http_status == 400 && f.backend.created == 0);
  LiveMediaCall call("live_once", 1000, false, f.callbacks, f.limits, sink);
  Q4T_CHECK(call.Start(Parse(R"({"model":"native"})")).ok());
  Q4T_CHECK(!call.Start(Parse(R"({"model":"native"})")).ok());
  Q4T_CHECK(f.backend.created == 1);
  Q4T_CHECK(!call.Receive('P', "aa") && !call.Receive('D', "{}"));
  Q4T_CHECK(call.Receive('V', {}) && !call.Receive('R', {}));
  Q4T_CHECK(call.Receive('P', "aa") && f.backend.commands.size() == 1);
  return true;
}

Q4T_TEST(live_media_frontend_authority_and_bidirectional_audio_after_close) {
  Fixture f;
  f.callbacks.configuration.transport = LiveTransport::kWebRtc;
  std::vector<std::pair<char, std::string>> packets;
  LiveMediaCall call("live_media", 1000, true, f.callbacks, f.limits,
                     [&](char type, auto data) {
                       packets.emplace_back(type, data);
                       return true;
                     });
  auto config = Parse(R"({"model":"native","client":{"data_channel":{
    "allowed_client_events":["session.input_audio.unmute"],
    "allowed_server_events":[{"type":"session.started"},{"type":"error"}]}}})");
  Q4T_CHECK(call.Start(config).ok() && call.Tick(f.now) && packets.empty());
  Q4T_CHECK(call.Receive('V', {}) && call.Tick(f.now) && packets.empty());
  Q4T_CHECK(call.Receive('R', {}) && call.Tick(f.now) && packets.size() == 1);
  Q4T_CHECK(Parse(packets[0].second).Find("session")->GetString("id") == "live_media");
  for (auto message : {R"({"type":"session.input_audio.mute","event_id":"deny"})",
                       R"({"type":"session.input_audio.append","audio":"YWE=","event_id":"deny"})"}) {
    Q4T_CHECK(call.Receive('D', message) && call.Tick(f.now));
    const auto event = Parse(packets.back().second);
    Q4T_CHECK(event.GetString("type") == "error" &&
              event.Find("error")->GetString("code") == "live_client_event_forbidden" &&
              event.GetString("client_event_id") == "deny");
  }
  Q4T_CHECK(f.backend.commands.empty());
  Q4T_CHECK(call.Receive('D', R"({"type":"session.input_audio.unmute"})"));
  Q4T_CHECK(call.Tick(f.now) && packets.size() == 3 && f.backend.commands.size() == 1);
  Q4T_CHECK(call.Receive('C', {}) && call.Receive('P', "aa"));
  Q4T_CHECK(f.backend.commands.back().GetString("audio") == "YWE=");
  std::string encoded, pcm(1920, 'z');
  Q4T_CHECK(q4t::io::EncodeBase64(pcm, 4096, &encoded));
  f.backend.outgoing.push_back(Object({{"type", String("session.output_audio.delta")},
                                      {"delta", String(std::move(encoded))}}));
  Q4T_CHECK(call.Tick(f.now) && packets.size() == 4);
  Q4T_CHECK(call.Tick(f.now + 19ms) && packets.size() == 4);
  Q4T_CHECK(call.Tick(f.now + 20ms) && packets.size() == 5);
  Q4T_CHECK(packets[3].first == 'P' && packets[4].first == 'P' &&
            packets[3].second + packets[4].second == pcm);
  return true;
}

Q4T_TEST(live_sideband_three_second_replay_and_independent_readers) {
  using Read = LiveSideband::Read;
  const auto origin = Clock::time_point(100s);
  LiveSideband stream;
  stream.Publish("expired", origin);
  stream.Publish("boundary", origin + 1s);
  stream.Publish("recent", origin + 3s);
  uint64_t first = 0, second = 0;
  Q4T_CHECK(stream.Attach(&first, origin + 4s).ok() && first == 1);
  std::shared_ptr<const std::string> bytes;
  Q4T_CHECK(stream.Next(first++, &bytes) == Read::kEvent && *bytes == "boundary");
  Q4T_CHECK(stream.Attach(&second, origin + 4s + 1ns).ok() && second == 2);
  stream.Publish("new", origin + 5s);
  Q4T_CHECK(stream.Next(first++, &bytes) == Read::kEvent && *bytes == "recent");
  Q4T_CHECK(stream.Next(second++, &bytes) == Read::kEvent && *bytes == "recent");
  stream.Finish(true);
  Q4T_CHECK(stream.Next(second++, &bytes) == Read::kEvent && *bytes == "new");
  Q4T_CHECK(stream.Next(second, &bytes) == Read::kCompleted);
  Q4T_CHECK(stream.Attach(&first, origin + 6s).http_status == 404);
  return true;
}

Q4T_TEST(live_sideband_pressure_never_publishes_an_incomplete_replay) {
  using Read = LiveSideband::Read;
  LiveSideband stream;
  const auto now = Clock::now();
  uint64_t slow = 0, current = 0;
  std::shared_ptr<const std::string> bytes;
  Q4T_CHECK(stream.Attach(&slow, now).ok());
  for (int i = 0; i < 2049; ++i) stream.Publish("event", now);
  Q4T_CHECK(stream.Next(slow, &bytes) == Read::kLost);
  Q4T_CHECK(stream.Attach(&current, now + 3s).http_status == 503);
  stream.Publish("recovered", now + 3s + 1ns);
  Q4T_CHECK(stream.Attach(&current, now + 3s + 1ns).ok());
  Q4T_CHECK(stream.Next(current++, &bytes) == Read::kEvent && *bytes == "recovered");
  for (int i = 0; i < 8; ++i)
    Q4T_CHECK(stream.Enqueue(WebSocketOpcode::kText, std::to_string(i)));
  Q4T_CHECK(!stream.HasSpace(1) && !stream.Enqueue(WebSocketOpcode::kText, "extra"));
  for (int i = 0; i < 8; ++i) {
    const auto command = stream.Pop();
    Q4T_CHECK(command && command->text == std::to_string(i));
  }
  Q4T_CHECK(!stream.Pop() && stream.HasSpace(1));
  stream.Finish(false);
  Q4T_CHECK(stream.Next(current, &bytes) == Read::kLost);
  return true;
}

Q4T_TEST(live_sideband_commands_share_the_owner_and_reflect_raw_muted_input) {
  Fixture f;
  auto sideband = std::make_shared<LiveSideband>();
  f.callbacks.configuration.transport = LiveTransport::kWebRtc;
  std::vector<std::string> frontend;
  LiveMediaCall call("live_media", 1000, true, f.callbacks, f.limits,
                     [&](char, auto text) { frontend.emplace_back(text); return true; }, sideband);
  Q4T_CHECK(call.Start(Parse(R"({"model":"native","client":{"data_channel":{
    "allowed_client_events":[],"allowed_server_events":[]}}})")).ok());
  Q4T_CHECK(call.Receive('V', {}) && call.Receive('R', {}));
  uint64_t next;
  Q4T_CHECK(sideband->Attach(&next, Clock::now()).ok());
  for (auto event : {R"({"type":"session.input_audio.mute","event_id":"trusted"})",
                      R"({"type":"session.input_audio.append","audio":"AAE=","event_id":"bad_audio"})",
                      R"({"type":"session.start","session":{"model":"native"},"event_id":"bad_start"})",
                      R"({"type":"transport.dtmf.send","event":"1","event_id":"not_sip"})"}) {
    Q4T_CHECK(sideband->Enqueue(WebSocketOpcode::kText, event) && call.Tick(f.now));
  }
  Q4T_CHECK(call.Receive('P', "ab") && f.backend.commands.size() == 2);
  Q4T_CHECK(f.backend.commands[0].GetString("event_id") == "trusted");
  Q4T_CHECK(f.backend.commands[1].GetString("audio") == "YWI=");
  sideband->OutputAudio(48001, "abcd");
  std::shared_ptr<const std::string> text;
  std::vector<Json> events;
  while (sideband->Next(next, &text) == LiveSideband::Read::kEvent) {
    auto event = Parse(*text);
    ChatPlanError error;
    Q4T_CHECK(ValidateLiveWireShape(event, LiveShape::kSidebandServer, &error));
    events.push_back(std::move(event));
    ++next;
  }
  Q4T_CHECK(frontend.empty() && events.size() == 7);
  Q4T_CHECK(events[0].GetString("type") == "session.started");
  Q4T_CHECK(events[1].GetString("type") == "session.input_audio.muted");
  for (int i = 2; i < 5; ++i) Q4T_CHECK(events[i].GetString("type") == "error");
  Q4T_CHECK(events[5].GetString("type") == "session.input_audio.append" &&
            events[5].GetString("audio") == "YWI=" && events[5].object.size() == 2);
  Q4T_CHECK(events[6].GetString("delta") == "YWJjZA==" && !events[6].Has("event_id"));
  Q4T_CHECK(events[6].Find("start_ms")->number > 1000 &&
            events[6].Find("end_ms")->number > events[6].Find("start_ms")->number);
  return true;
}

Q4T_TEST(live_sip_dtmf_waits_for_media_and_preserves_client_ids) {
  Fixture f;
  std::string wire;
  f.callbacks.configuration.transport = LiveTransport::kSip;
  f.callbacks.send_dtmf = [&](char key) {
    wire += key;
    return true;
  };
  Q4T_CHECK(
      f.Start() &&
      f.session->StartMedia(Parse(R"({"type":"live","model":"native"})")).ok());
  const auto key = [&](char digit, const char* id) {
    return f.session->SidebandMessage(
               WebSocketOpcode::kText,
               OutputJson(Object({{"type", String("transport.dtmf.send")},
                                  {"event", String(std::string(1, digit))},
                                  {"event_id", String(id)}}))) &&
           f.Read();
  };
  Q4T_CHECK(key('1', "a") && key('#', "b") && key('#', "c"));
  Q4T_CHECK(wire == "1" && f.Count("transport.dtmf.sent") == 0);
  Q4T_CHECK(f.session->MediaDtmf('K', 'D') && f.Read());
  Q4T_CHECK(f.Last("transport.dtmf.received").GetString("event") == "D");
  Q4T_CHECK(f.session->MediaDtmf('U', '1') && f.Tick());
  Q4T_CHECK(f.Last("error").Find("error")->GetString("client_event_id") == "a");
  Q4T_CHECK(wire == "1#" && f.session->MediaDtmf('T', '#') && f.Tick());
  Q4T_CHECK(f.Last("transport.dtmf.sent").GetString("client_event_id") == "b");
  Q4T_CHECK(wire == "1##" && f.session->CloseMedia() && f.Read());
  Q4T_CHECK(f.Last("error").Find("error")->GetString("client_event_id") == "c");
  Q4T_CHECK(f.session->MediaDtmf('T', '#') && f.Read());
  Q4T_CHECK(f.Count("transport.dtmf.sent") == 1 && f.backend.commands.empty());
  return true;
}

Q4T_TEST(live_sip_dtmf_timeout_and_queue_pressure_preserve_conversation) {
  Fixture f;
  int sends = 0;
  f.callbacks.configuration.transport = LiveTransport::kSip;
  f.callbacks.send_dtmf = [&](char) {
    ++sends;
    return true;
  };
  Q4T_CHECK(
      f.Start() &&
      f.session->StartMedia(Parse(R"({"type":"live","model":"native"})")).ok());
  const auto send = [&] {
    return f.session->SidebandMessage(
        WebSocketOpcode::kText,
        R"({"type":"transport.dtmf.send","event":"*","event_id":"pending"})");
  };
  for (int i = 0; i < 17; ++i) Q4T_CHECK(send());
  Q4T_CHECK(f.Read() && sends == 1 && f.Count("error") == 1);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "sip_dtmf_capacity");
  f.now += 5s;
  Q4T_CHECK(f.Tick() && f.Count("error") == 17);
  Q4T_CHECK(f.session->MediaDtmf('T', '*') && send() && f.Read());
  Q4T_CHECK(f.Count("transport.dtmf.sent") == 0 && sends == 1);
  Q4T_CHECK(f.Send(R"({"type":"session.input_audio.mute"})") &&
            f.Count("session.input_audio.muted") == 1);
  return true;
}
