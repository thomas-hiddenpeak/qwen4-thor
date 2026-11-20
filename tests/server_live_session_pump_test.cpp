#include "q4t/server/live_session_pump.h"

#include <sys/socket.h>
#include <unistd.h>

#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
struct BackendState {
  size_t closes = 0, commands = 0, cancelled = 0, joined = 0;
};
class Backend final : public ILiveSessionBackend {
 public:
  explicit Backend(BackendState& state) : state_(state) {}
  ObjectReply Update(const Json&) override { return {}; }
  ObjectReply Command(const Json&) override {
    ++state_.commands;
    return {};
  }
  ObjectReply Tick() override { return {}; }
  ObjectReply Close() override {
    ++state_.closes;
    return {};
  }
  bool Drained() const override { return state_.closes > 0; }
  void Cancel() noexcept override { ++state_.cancelled; }
  void Join() noexcept override { ++state_.joined; }

 private:
  BackendState& state_;
};
struct Sockets {
  int f[2];
  Sockets() {
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, f))
      throw std::runtime_error("socketpair");
    const timeval timeout{2, 0};
    for (const auto fd : f)
      setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  }
  ~Sockets() {
    for (const auto fd : f) close(fd);
  }
};
}  // namespace

Q4T_TEST(live_session_pump_finalization_drains_wire_and_discards_queued_tail) {
  BackendState backend;
  LiveSessionCallbacks callbacks;
  callbacks.configuration.default_voice = String("vivian");
  callbacks.configuration.default_instructions = "Native fixture";
  callbacks.configuration.count_text = [](auto, size_t* count, auto*) {
    *count = 3;
    return q4t::Status{};
  };
  callbacks.configuration.count_history = [](const auto&, size_t* count,
                                             auto*) {
    *count = 0;
    return q4t::Status{};
  };
  callbacks.configuration.validate = [](const auto&, auto*) {
    return q4t::Status{};
  };
  callbacks.create = [&](const auto&, const auto&, auto* out) {
    *out = std::make_unique<Backend>(backend);
    return ObjectReply{};
  };
  WebSocketOutput output;
  LiveSessionPump pump("live_pump", 1795200000, output, callbacks);
  Q4T_CHECK(pump.Start());
  // Queue this complete tail before the reader observes finalization. Its last
  // command must not poison a valid terminal event or execute new model work.
  for (const auto event :
       {R"({"type":"session.start","session":{"model":"native"}})",
        R"({"type":"session.close"})",
        R"({"type":"session.input_audio.mute"})"})
    Q4T_CHECK(pump.Message(WebSocketOpcode::kText, event));
  Sockets sockets;
  WebSocketSessionCallbacks cb;
  cb.message = [&](auto op, auto event) { return pump.Message(op, event); };
  cb.message_ready = [&](size_t bytes) { return pump.CanAccept(bytes); };
  cb.tick = [&] { return pump.Alive(); };
  cb.cancel = [&] { pump.Stop(); };
  cb.completed = [&] { return pump.Finalized(); };
  const auto result =
      RunWebSocketSession(sockets.f[0], {}, output, cb, {65536, 1s, 500ms, 2s});
  pump.Join();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kCompleted &&
            result.close_code == 1000 && pump.Finalized());
  Q4T_CHECK(backend.closes == 1 && backend.joined == 1 &&
            backend.commands == 0 && backend.cancelled == 0);
  Q4T_CHECK(!pump.InputBytes() && !pump.InputMessages());
  std::string wire(result.sent_bytes, '\0');
  size_t at = 0;
  while (at < wire.size()) {
    const auto count =
        recv(sockets.f[1], wire.data() + at, wire.size() - at, 0);
    Q4T_CHECK(count > 0);
    at += size_t(count);
  }
  WebSocketDecoder decoder(false);
  auto remaining =
      std::span(reinterpret_cast<const uint8_t*>(wire.data()), wire.size());
  std::vector<std::string> events;
  size_t closes = 0;
  while (!remaining.empty()) {
    size_t consumed = 0;
    Q4T_CHECK(decoder.Consume(remaining, &consumed) == WebSocketRead::kEvent);
    remaining = remaining.subspan(consumed);
    const auto event = decoder.Event();
    if (event.opcode == WebSocketOpcode::kClose) {
      ++closes;
      Q4T_CHECK(event.payload == "\x03\xe8" && remaining.empty());
    } else {
      Json value;
      ChatPlanError error;
      Q4T_CHECK(q4t::io::ParseJson(std::string(event.payload), &value, true));
      Q4T_CHECK(ValidateLiveShape(value, LiveShape::kServerEvent, &error));
      events.push_back(value.GetString("type"));
    }
    decoder.Acknowledge();
  }
  Q4T_CHECK(events ==
            std::vector<std::string>({"session.started", "session.closed"}));
  Q4T_CHECK(closes == 1 && output.Closed() && !output.Bytes());
  return true;
}
