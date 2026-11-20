#include "q4t/server/live_session.h"

#include <algorithm>
#include <stdexcept>

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
      Q4T_CHECK(ValidateLiveShape(event, LiveShape::kServerEvent, &error));
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
