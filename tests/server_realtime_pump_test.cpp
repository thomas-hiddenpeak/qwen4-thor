#include "q4t/server/realtime_transcription_pump.h"

#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
bool Wait(const std::function<bool()>& predicate) {
  const auto end = Clock::now() + 2s;
  while (Clock::now() < end) {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
struct Fixture {
  WebSocketOutput output;
  RequestRegistry requests;
  std::atomic<int> calls{0}, released{0};
  RealtimeTranscriptionCallbacks cb;
  Fixture() {
    cb.validate = [](const auto&) { return ObjectReply{}; };
    cb.acquire = [&](const auto& id) {
      return requests.Register(id, "", Clock::now() + 5s);
    };
    cb.release = [&](const auto& value) {
      requests.Remove(value);
      ++released;
    };
    cb.execute = [&](const auto&, const auto&, const auto&, const auto&) {
      ++calls;
      ObjectReply reply;
      reply.value = Object({{"text", String("fixture")},
                            {"usage", Object({{"total_tokens", Number(1)}})}});
      return reply;
    };
  }
};
constexpr auto kAppend =
    R"({"type":"input_audio_buffer.append","audio":"AQACAA=="})";
constexpr auto kCommit = R"({"type":"input_audio_buffer.commit"})";
constexpr auto kClear = R"({"type":"input_audio_buffer.clear"})";
constexpr auto kHold =
    R"({"type":"transcription_session.update","session":{"input_audio_transcription":{"prompt":"hold"}}})";
}  // namespace

Q4T_TEST(
    realtime_pump_preserves_pipelined_message_order_and_finishes_without_input) {
  Fixture f;
  RealtimeTranscriptionPump p("sess_pump", f.output, f.cb, 30);
  Q4T_CHECK(p.Start() && !p.Start());
  for (int i = 0; i < 2; ++i) {
    Q4T_CHECK(p.Message(WebSocketOpcode::kText, kAppend));
    Q4T_CHECK(p.Message(WebSocketOpcode::kText, kCommit));
  }
  Q4T_CHECK(Wait([&] { return f.released == 2; }));
  Q4T_CHECK(f.calls == 2 && p.Alive());
  Q4T_CHECK(
      Wait([&] { return p.InputMessages() == 0 && p.InputBytes() == 0; }));
  p.Stop();
  p.Join();
  Q4T_CHECK(!p.Alive() && !p.Message(WebSocketOpcode::kText, kClear));
  return true;
}

Q4T_TEST(realtime_pump_active_message_stays_charged_and_inbox_is_bounded) {
  for (bool byte_limit : {false, true}) {
    Fixture f;
    std::atomic<bool> entered{false}, release{false};
    f.cb.validate = [&](const auto& settings) {
      if (settings.request.prompt == "hold") {
        entered = true;
        while (!release.load()) std::this_thread::sleep_for(1ms);
      }
      return ObjectReply{};
    };
    RealtimeTranscriptionPump p("sess_pump", f.output, f.cb, 30);
    ScopeExit unblock([&] { release = true; });
    Q4T_CHECK(p.Start() && p.Message(WebSocketOpcode::kText, kHold));
    Q4T_CHECK(Wait([&] { return entered.load(); }));
    Q4T_CHECK(p.InputMessages() == 1 &&
              p.InputBytes() >= std::string_view(kHold).size());
    if (byte_limit) {
      const std::string large(8ULL << 20, ' ');
      Q4T_CHECK(p.Message(WebSocketOpcode::kText, large));
      Q4T_CHECK(!p.CanAccept(large.size()));
      Q4T_CHECK(!p.Message(WebSocketOpcode::kText, large));
      Q4T_CHECK(p.InputBytes() < kRealtimeInboxBytes);
      p.Stop();
    } else {
      for (int i = 0; i < 7; ++i)
        Q4T_CHECK(p.Message(WebSocketOpcode::kText, kClear));
      Q4T_CHECK(p.InputMessages() == 8 &&
                !p.CanAccept(std::string_view(kClear).size()));
      Q4T_CHECK(!p.Message(WebSocketOpcode::kText, kClear));
    }
    release = true;
    Q4T_CHECK(Wait([&] { return p.InputMessages() == 0; }));
    p.Stop();
    p.Join();
    Q4T_CHECK(p.InputBytes() == 0 && f.calls == 0);
  }
  return true;
}

Q4T_TEST(realtime_pump_stop_cancels_model_borrower_before_releasing_input) {
  Fixture f;
  f.cb.execute = [&](const auto&, const auto&, const auto& control,
                     const auto&) {
    ++f.calls;
    while (!control->Cancelled()) std::this_thread::sleep_for(1ms);
    return Error(409, control->Reason(), "fixture cancellation");
  };
  RealtimeTranscriptionPump p("sess_pump", f.output, f.cb, 30);
  Q4T_CHECK(p.Start() && p.Message(WebSocketOpcode::kText, kAppend) &&
            p.Message(WebSocketOpcode::kText, kCommit));
  Q4T_CHECK(Wait([&] { return f.calls == 1; }));
  p.Stop();
  p.Join();
  Q4T_CHECK(!p.Alive() && f.released == 1 && p.InputMessages() == 0);
  return true;
}

Q4T_TEST(realtime_pump_local_control_exception_does_not_affect_other_session) {
  Fixture f;
  f.cb.validate = [](const auto& settings) {
    if (settings.request.prompt == "hold") throw std::runtime_error("fixture");
    return ObjectReply{};
  };
  RealtimeTranscriptionPump p("sess_bad", f.output, f.cb, 30);
  Q4T_CHECK(p.Start() && p.Message(WebSocketOpcode::kText, kHold));
  Q4T_CHECK(Wait([&] { return !p.Alive(); }));
  p.Join();
  Q4T_CHECK(f.calls == 0 && p.InputBytes() == 0);
  Fixture healthy;
  RealtimeTranscriptionPump next("sess_good", healthy.output, healthy.cb, 30);
  Q4T_CHECK(next.Start() && next.Message(WebSocketOpcode::kText, kAppend) &&
            next.Message(WebSocketOpcode::kText, kCommit));
  Q4T_CHECK(Wait([&] { return healthy.released == 1; }));
  next.Stop();
  next.Join();
  return true;
}
