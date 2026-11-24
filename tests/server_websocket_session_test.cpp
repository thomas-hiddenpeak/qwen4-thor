#include "q4t/server/websocket_session.h"

#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <chrono>
#include <future>
#include <stdexcept>
#include <thread>
#include <vector>

#include "q4t/server/client_request.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Sockets {
  int f[2];
  Sockets() {
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, f))
      throw std::runtime_error("socketpair");
    const timeval timeout{2, 0};
    for (const auto fd : f) {
      setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
      setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    }
  }
  ~Sockets() {
    for (const auto fd : f) close(fd);
  }
  void Small() {
    const int size = 1024;
    setsockopt(f[0], SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
    setsockopt(f[1], SOL_SOCKET, SO_RCVBUF, &size, sizeof(size));
  }
};
std::string Frame(std::string_view payload,
                  WebSocketOpcode opcode = WebSocketOpcode::kText,
                  bool masked = true) {
  std::string result;
  const auto mask = masked
                        ? std::optional(std::array<uint8_t, 4>{13, 27, 59, 83})
                        : std::nullopt;
  if (!EncodeWebSocketFrame(opcode, payload, mask, &result))
    throw std::runtime_error("test frame");
  return result;
}
std::string Close() { return Frame("\x03\xe8", WebSocketOpcode::kClose); }
bool Send(int fd, std::string_view bytes) {
  while (!bytes.empty()) {
    const auto n = send(fd, bytes.data(), bytes.size(), MSG_NOSIGNAL);
    if (n <= 0) return false;
    bytes.remove_prefix(n);
  }
  return true;
}
std::string Read(int fd, size_t bytes) {
  std::string result(bytes, '\0');
  size_t at = 0;
  while (at < bytes) {
    const auto n = recv(fd, result.data() + at, bytes - at, 0);
    if (n <= 0) {
      result.resize(at);
      break;
    }
    at += n;
  }
  return result;
}
bool Eventually(const std::function<bool()>& predicate) {
  const auto end = Clock::now() + 1s;
  while (Clock::now() < end) {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
WebSocketSessionLimits Limits() { return {2ULL << 20, 1000ms, 500ms, 2000ms}; }
ClientIdentity Configure(ClientAdmission& admission) {
  GatewayClientPolicy policy;
  const std::string raw =
      R"({"version":3,"revision":0,"fair_queue":true,"gateway_token":")" +
      std::string(64, 'a') +
      R"(","clients":[{"id":"alpha","max_inflight":1,)"
      R"("max_control_inflight":1,"max_prompt_tokens":64,)"
      R"("max_output_tokens":16,"max_body_bytes":1024,)"
      R"("requests_per_minute":2,"tokens_per_minute":100}]})";
  if (!ParseGatewayClientPolicy(raw, &policy) ||
      !admission.Configure(std::move(policy)))
    throw std::runtime_error("policy");
  return admission.Authenticate("alpha", std::string(64, 'a'));
}
}  // namespace

Q4T_TEST(websocket_output_boundaries_are_atomic_and_charge_capacity) {
  WebSocketOutput bad(127), output(256, 1);
  Q4T_CHECK(!bad.Valid() && !bad.TryPush(WebSocketOpcode::kText, "x"));
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kClose, ""));
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kText, "\xff"));
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kBinary, std::string(257, 'a')));
  Q4T_CHECK(output.Bytes() == 0);
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "first"));
  const auto front = output.Front();
  const auto bytes = output.Bytes();
  Q4T_CHECK(front == Frame("first", WebSocketOpcode::kText, false));
  Q4T_CHECK(bytes >= front.size() && bytes <= 256);
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kText, "second"));
  Q4T_CHECK(output.Front().data() == front.data() && output.Bytes() == bytes);
  output.Acknowledge();
  Q4T_CHECK(output.Bytes() == 0 && output.Front().empty());
  Q4T_CHECK(output.PeakBytes() == bytes);
  return true;
}

Q4T_TEST(websocket_output_explicit_large_message_keeps_byte_and_frame_bounds) {
  WebSocketOutput original;
  const std::string payload((1ULL << 20) + 17, 'x');
  Q4T_CHECK(!original.TryPush(WebSocketOpcode::kText, payload));
  WebSocketOutput output(4ULL << 20, 1, 2ULL << 20);
  Q4T_CHECK(output.Valid() && output.MessageLimit() == 2ULL << 20);
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, payload));
  const auto front = output.Front();
  Q4T_CHECK(front == Frame(payload, WebSocketOpcode::kText, false));
  const auto charge = output.Bytes();
  Q4T_CHECK(charge >= front.size() && charge <= 4ULL << 20);
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kText, "later"));
  Q4T_CHECK(output.Bytes() == charge && output.Front().data() == front.data());
  output.Acknowledge();
  Q4T_CHECK(output.Bytes() == 0);
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kBinary,
                            std::string((2ULL << 20) + 1, 'x')));
  Q4T_CHECK(output.Bytes() == 0 && output.Front().empty());
  return true;
}

Q4T_TEST(websocket_output_explicit_limits_reject_without_allocating_queue) {
  WebSocketOutput full(kWebSocketOutputMaximumBytes, 64,
                       kWebSocketMaximumLimit);
  Q4T_CHECK(full.Valid() && !full.Bytes());
  Q4T_CHECK(!WebSocketOutput(kWebSocketOutputMaximumBytes + 1).Valid());
  Q4T_CHECK(!WebSocketOutput(1024, 0, 1024).Valid());
  Q4T_CHECK(!WebSocketOutput(1024, 65, 1024).Valid());
  Q4T_CHECK(!WebSocketOutput(1024, 1, 0).Valid());
  Q4T_CHECK(!WebSocketOutput(1024, 1, kWebSocketMaximumLimit + 1).Valid());
  // A small mailbox may use the unchanged larger default message limit;
  // the actual encoded frame still must fit its own smaller byte budget.
  WebSocketOutput small(256);
  Q4T_CHECK(small.Valid() &&
            !small.TryPush(WebSocketOpcode::kBinary, std::string(256, 'x')));
  Q4T_CHECK(!small.Bytes());
  return true;
}

Q4T_TEST(websocket_output_producer_waits_for_ack_and_observes_cancel) {
  WebSocketOutput output(256, 1);
  RequestControl control("turn", "", Clock::now() + 2s);
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "first"));
  auto work = std::async(std::launch::async, [&] {
    return output.Push(WebSocketOpcode::kText, "second", control);
  });
  Q4T_CHECK(work.wait_for(15ms) == std::future_status::timeout);
  output.Acknowledge();
  Q4T_CHECK(work.wait_for(1s) == std::future_status::ready && work.get());
  Q4T_CHECK(output.Front() == Frame("second", WebSocketOpcode::kText, false));
  auto cancelled = std::async(std::launch::async, [&] {
    return output.Push(WebSocketOpcode::kText, "never", control);
  });
  Q4T_CHECK(cancelled.wait_for(15ms) == std::future_status::timeout);
  control.Cancel();
  Q4T_CHECK(cancelled.wait_for(1s) == std::future_status::ready &&
            !cancelled.get());
  return true;
}

Q4T_TEST(websocket_output_close_keeps_only_borrowed_front_and_wakes_producer) {
  WebSocketOutput output(256, 2);
  RequestControl control("turn", "", Clock::now() + 2s);
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "first"));
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "second"));
  const auto front = output.Front();
  auto work = std::async(std::launch::async, [&] {
    return output.Push(WebSocketOpcode::kText, "never", control);
  });
  output.Close(true);
  Q4T_CHECK(work.wait_for(1s) == std::future_status::ready && !work.get());
  Q4T_CHECK(output.Closed() && output.Front().data() == front.data());
  output.Acknowledge();
  Q4T_CHECK(output.Front().empty() && output.Bytes() == 0);
  Q4T_CHECK(!output.TryPush(WebSocketOpcode::kText, "late"));
  return true;
}

Q4T_TEST(websocket_session_initial_fragment_ping_and_close_are_ordered) {
  Sockets s;
  WebSocketOutput output;
  int cancelled = 0;
  std::vector<std::string> messages;
  auto first = Frame("hel");
  first[0] = 1;  // Non-final text, followed by a masked continuation.
  auto last = Frame("lo");
  last[0] = char(0x80);
  const auto initial = first + Frame("probe", WebSocketOpcode::kPing) + last;
  auto work = std::async(std::launch::async, [&] {
    return RunWebSocketSession(s.f[0], initial, output,
                               {[&](auto op, auto text) {
                                  if (op != WebSocketOpcode::kText)
                                    return false;
                                  messages.emplace_back(text);
                                  return output.TryPush(WebSocketOpcode::kText,
                                                        text);
                                },
                                {},
                                {},
                                [&] { ++cancelled; }},
                               Limits());
  });
  const auto expected = Frame("probe", WebSocketOpcode::kPong, false) +
                        Frame("hello", WebSocketOpcode::kText, false);
  Q4T_CHECK(Read(s.f[1], expected.size()) == expected);
  Q4T_CHECK(Send(s.f[1], Close()));
  Q4T_CHECK(Read(s.f[1], 4) ==
            Frame("\x03\xe8", WebSocketOpcode::kClose, false));
  const auto result = work.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kPeerClose &&
            result.close_code == 1000 && result.messages == 1);
  Q4T_CHECK(messages == std::vector<std::string>{"hello"} && cancelled == 1);
  Q4T_CHECK(output.Closed() && output.Bytes() == 0 && result.peak_output_bytes);
  return true;
}

Q4T_TEST(websocket_session_eof_is_abnormal_and_never_delivers_truncated_data) {
  Sockets s;
  WebSocketOutput output;
  int delivered = 0, cancelled = 0;
  const auto partial = Frame("incomplete").substr(0, 8);
  shutdown(s.f[1], SHUT_WR);
  const auto result = RunWebSocketSession(s.f[0], partial, output,
                                          {[&](auto, auto) {
                                             ++delivered;
                                             return true;
                                           },
                                           {},
                                           {},
                                           [&] { ++cancelled; }},
                                          Limits());
  Q4T_CHECK(result.end == WebSocketSessionEnd::kPeerEof &&
            result.close_code == 1006 && delivered == 0 && cancelled == 1);
  return true;
}

Q4T_TEST(websocket_session_protocol_failures_emit_one_close_without_dispatch) {
  for (const bool utf8 : {false, true}) {
    Sockets s;
    WebSocketOutput output;
    auto initial = Frame("x", WebSocketOpcode::kText, utf8);
    if (utf8) initial.back() = char(0xff ^ 13);
    int calls = 0;
    const auto result = RunWebSocketSession(s.f[0], initial, output,
                                            {[&](auto, auto) {
                                               ++calls;
                                               return true;
                                             },
                                             {},
                                             {},
                                             {}},
                                            Limits());
    const uint16_t code = utf8 ? 1007 : 1002;
    Q4T_CHECK(result.end == WebSocketSessionEnd::kProtocol &&
              result.close_code == code && calls == 0);
    const auto wire = Read(s.f[1], 4);
    Q4T_CHECK(wire == Frame(std::string({char(code >> 8), char(code & 255)}),
                            WebSocketOpcode::kClose, false));
  }
  return true;
}

Q4T_TEST(websocket_session_reads_cancel_while_output_and_producer_are_blocked) {
  Sockets s;
  s.Small();
  WebSocketOutput output(2ULL << 20, 1);
  RequestControl control("turn", "", Clock::now() + 2s);
  Q4T_CHECK(
      output.TryPush(WebSocketOpcode::kBinary, std::string(1ULL << 20, 'a')));
  auto producer = std::async(std::launch::async, [&] {
    return output.Push(WebSocketOpcode::kText, "late", control);
  });
  std::atomic<bool> received{false};
  auto work = std::async(std::launch::async, [&] {
    return RunWebSocketSession(s.f[0], "", output,
                               {[&](auto, auto text) {
                                  if (text == "cancel") {
                                    control.Cancel();
                                    received = true;
                                  }
                                  return true;
                                },
                                {},
                                {},
                                [&] { control.Cancel(); }},
                               Limits());
  });
  Q4T_CHECK(Send(s.f[1], Frame("cancel")));
  Q4T_CHECK(Eventually([&] { return received.load(); }));
  Q4T_CHECK(producer.wait_for(1s) == std::future_status::ready &&
            !producer.get());
  shutdown(s.f[1], SHUT_RDWR);
  const auto result = work.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kPeerEof ||
            result.end == WebSocketSessionEnd::kIo);
  Q4T_CHECK(output.Closed() && output.Bytes() == 0);
  return true;
}

Q4T_TEST(websocket_session_reverse_traffic_cannot_renew_write_deadline) {
  Sockets s;
  s.Small();
  WebSocketOutput output;
  Q4T_CHECK(
      output.TryPush(WebSocketOpcode::kBinary, std::string(1ULL << 20, 'b')));
  std::atomic<int> incoming{0};
  auto limits = Limits();
  limits.write = 80ms;
  auto work = std::async(std::launch::async, [&] {
    return RunWebSocketSession(s.f[0], "", output,
                               {[&](auto, auto) {
                                  ++incoming;
                                  return true;
                                },
                                {},
                                {},
                                {}},
                               limits);
  });
  for (int i = 0; i < 20 && work.wait_for(0ms) != std::future_status::ready;
       ++i) {
    Q4T_CHECK(Send(s.f[1], Frame("keep active")));
    std::this_thread::sleep_for(5ms);
  }
  const auto result = work.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kWriteDeadline && incoming > 1);
  Q4T_CHECK(result.sent_bytes > 0 && result.sent_bytes < (1ULL << 20));
  return true;
}

Q4T_TEST(websocket_session_idle_absolute_and_invalid_limits_are_distinct) {
  for (int kind = 0; kind < 3; ++kind) {
    Sockets s;
    WebSocketOutput output;
    int cancelled = 0;
    auto limits = Limits();
    if (kind == 0) limits.idle = 10ms;
    if (kind == 1) limits.session = 10ms;
    if (kind == 2) limits.write = 0ms;
    const auto result = RunWebSocketSession(
        s.f[0], "", output,
        {[](auto, auto) { return true; }, {}, {}, [&] { ++cancelled; }},
        limits);
    Q4T_CHECK(result.end == (kind == 0   ? WebSocketSessionEnd::kIdleDeadline
                             : kind == 1 ? WebSocketSessionEnd::kSessionDeadline
                                         : WebSocketSessionEnd::kInvalid));
    Q4T_CHECK(cancelled == 1 && output.Closed());
  }
  return true;
}

Q4T_TEST(websocket_session_revocation_and_shutdown_cancel_before_next_message) {
  for (const bool revoked : {false, true}) {
    Sockets s;
    WebSocketOutput output;
    int calls = 0, cancelled = 0;
    const auto result =
        RunWebSocketSession(s.f[0], Frame("must not run"), output,
                            {[&](auto, auto) {
                               ++calls;
                               return true;
                             },
                             [=] { return !revoked; }, [=] { return !revoked; },
                             [&] { ++cancelled; }},
                            Limits());
    Q4T_CHECK(result.end == (revoked ? WebSocketSessionEnd::kRevoked
                                     : WebSocketSessionEnd::kShutdown));
    Q4T_CHECK(cancelled == 1 && calls == 0);
  }
  return true;
}

Q4T_TEST(websocket_session_partial_writes_keep_exact_bytes_and_close_boundary) {
  Sockets s;
  s.Small();
  WebSocketOutput output;
  std::string payload(256 * 1024, '\0');
  for (size_t i = 0; i < payload.size(); ++i) payload[i] = char(i % 251);
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kBinary, payload));
  auto work = std::async(std::launch::async, [&] {
    return RunWebSocketSession(s.f[0], "", output,
                               {[](auto, auto) { return true; }, {}, {}, {}},
                               Limits());
  });
  const auto expected = Frame(payload, WebSocketOpcode::kBinary, false);
  // Receive a real prefix before closing; the frame must finish before Close.
  auto received = Read(s.f[1], 100);
  Q4T_CHECK(received.size() == 100 && Send(s.f[1], Close()));
  received += Read(s.f[1], expected.size() - received.size() + 4);
  Q4T_CHECK(received ==
            expected + Frame("\x03\xe8", WebSocketOpcode::kClose, false));
  const auto result = work.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kPeerClose &&
            result.sent_bytes == received.size());
  return true;
}

Q4T_TEST(
    websocket_session_callback_exception_cancels_and_drops_pending_output) {
  Sockets s;
  WebSocketOutput output;
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "never"));
  int cancelled = 0;
  const auto result = RunWebSocketSession(
      s.f[0], Frame("throw"), output,
      {[](auto, auto) -> bool { throw std::runtime_error("fixture"); },
       {},
       {},
       [&] {
         ++cancelled;
         throw std::runtime_error("cancel fixture");
       }},
      Limits());
  Q4T_CHECK(result.end == WebSocketSessionEnd::kApplication && cancelled == 1);
  Q4T_CHECK(result.sent_bytes == 0 && output.Closed() && output.Bytes() == 0);
  return true;
}

Q4T_TEST(
    websocket_session_receiver_backpressure_keeps_message_and_output_live) {
  Sockets s;
  WebSocketOutput output;
  std::atomic<bool> accept{false};
  std::atomic<int> ticks{0}, waits{0};
  std::vector<std::string> messages;
  WebSocketSessionCallbacks cb;
  cb.message = [&](auto, auto text) {
    messages.emplace_back(text);
    return true;
  };
  cb.message_ready = [&](size_t) {
    ++waits;
    return accept.load();
  };
  cb.tick = [&] {
    ++ticks;
    return true;
  };
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "alive"));
  auto task = std::async(std::launch::async, [&] {
    return RunWebSocketSession(s.f[0], Frame("one") + Frame("two") + Close(),
                               output, cb, Limits());
  });
  const auto alive = Frame("alive", WebSocketOpcode::kText, false);
  Q4T_CHECK(Read(s.f[1], alive.size()) == alive);
  Q4T_CHECK(Eventually([&] { return waits >= 2 && ticks >= 2; }));
  accept = true;
  Q4T_CHECK(task.wait_for(1s) == std::future_status::ready);
  const auto result = task.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kPeerClose &&
            result.messages == 2);
  Q4T_CHECK(messages == std::vector<std::string>({"one", "two"}));
  return true;
}

Q4T_TEST(websocket_session_receiver_wait_has_deadline_and_rechecks_authority) {
  for (bool revoke : {false, true}) {
    Sockets s;
    WebSocketOutput output;
    std::atomic<bool> authorized{true}, waited{false};
    int delivered = 0;
    WebSocketSessionCallbacks cb;
    cb.message = [&](auto, auto) {
      ++delivered;
      return true;
    };
    cb.message_ready = [&](size_t) {
      waited = true;
      return false;
    };
    cb.authorized = [&] { return authorized.load(); };
    auto limits = Limits();
    limits.write = 50ms;
    auto task = std::async(std::launch::async, [&] {
      return RunWebSocketSession(s.f[0], Frame("retained"), output, cb, limits);
    });
    Q4T_CHECK(Eventually([&] { return waited.load(); }));
    if (revoke) authorized = false;
    Q4T_CHECK(task.wait_for(1s) == std::future_status::ready);
    const auto result = task.get();
    Q4T_CHECK(delivered == 0 && result.messages == 0);
    Q4T_CHECK(result.end == (revoke ? WebSocketSessionEnd::kRevoked
                                    : WebSocketSessionEnd::kApplication));
  }
  return true;
}

Q4T_TEST(websocket_session_receiver_wait_observes_peer_eof_without_delivery) {
  Sockets s;
  WebSocketOutput output;
  std::atomic<bool> waited{false};
  WebSocketSessionCallbacks cb;
  cb.message = [](auto, auto) { return false; };
  cb.message_ready = [&](size_t) {
    waited = true;
    return false;
  };
  auto task = std::async(std::launch::async, [&] {
    return RunWebSocketSession(s.f[0], Frame("retained"), output, cb, Limits());
  });
  Q4T_CHECK(Eventually([&] { return waited.load(); }));
  Q4T_CHECK(shutdown(s.f[1], SHUT_WR) == 0);
  Q4T_CHECK(task.wait_for(1s) == std::future_status::ready);
  const auto result = task.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kPeerEof &&
            result.close_code == 1006);
  Q4T_CHECK(result.messages == 0);
  return true;
}

Q4T_TEST(websocket_session_client_lane_is_separate_and_exactly_classified) {
  ClientAdmission admission;
  const auto identity = Configure(admission);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/realtime?intent=transcription") ==
            HttpConnectionClass::kBusiness);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/live/sessions") ==
            HttpConnectionClass::kRealtime);
  for (const auto path :
       {"/v1/realtime?intent=transcription", "/v1/live/sessions"}) {
    Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
              HttpConnectionClass::kRealtime);
    HttpRequestHeaders headers;
    headers.client_id = "alpha";
    headers.gateway_token = std::string(64, 'a');
    ClientLease connection, duplicate, request;
    ClientIdentity owner;
    int status = 0;
    Q4T_CHECK(AdmitClientRequest("GET", path, &headers, admission, &connection,
                                 &owner, &status));
    Q4T_CHECK(!duplicate.Acquire(admission, identity, ClientLane::kRealtime));
    Q4T_CHECK(request.Acquire(admission, identity, ClientLane::kData));
    const auto usage = admission.Usage(0);
    Q4T_CHECK(usage.realtime_active == 1 && usage.data_active == 1 &&
              usage.realtime_rejected > 0);
    Q4T_CHECK(ClientPolicyMetrics(admission).find(
                  "q4t_client_requests_active{client=\"alpha\",lane="
                  "\"realtime\"} 1") != std::string::npos);
  }
  Q4T_CHECK(admission.Usage(0).realtime_active == 0);
  for (const auto path :
       {"/v1/realtime/x", "/v1/live/sessions/x", "/v1/realtime#x"})
    Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
              HttpConnectionClass::kBusiness);
  return true;
}

Q4T_TEST(live_sip_reject_preserves_control_admission) {
  constexpr auto path = "/v1/live/sessions/live_abc/reject";
  ClientAdmission admission;
  const auto identity = Configure(admission);
  ClientLease data, request;
  Q4T_CHECK(data.Acquire(admission, identity, ClientLane::kData));
  Q4T_CHECK(ClassifyHttpConnection("POST", path) ==
            HttpConnectionClass::kControl);
  Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
            HttpConnectionClass::kBusiness);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/realtime/calls/rtc_abc/reject") ==
            HttpConnectionClass::kControl);
  for (const auto bad : {"/v1/realtime/calls/live_a/reject",
                         "/v1/live/sessions/rtc_a/reject",
                         "/v1/realtime/calls/rtc_/reject",
                         "/v1/live/sessions/live_/reject",
                         "/v1/live/sessions/live_a/b/reject",
                         "/v1/live/sessions/live_a/reject/"})
    Q4T_CHECK(ClassifyHttpConnection("POST", bad) ==
              HttpConnectionClass::kBusiness);
  HttpRequestHeaders headers;
  headers.client_id = "alpha";
  headers.gateway_token = std::string(64, 'a');
  ClientIdentity owner;
  int status = 0;
  Q4T_CHECK(AdmitClientRequest("POST", path, &headers, admission, &request,
                               &owner, &status));
  const auto usage = admission.Usage(0);
  Q4T_CHECK(usage.control_active == 1 && usage.data_active == 1);
  return true;
}

Q4T_TEST(live_sip_accept_holds_realtime_lane_while_hangup_uses_control) {
  constexpr auto accept = "/v1/live/sessions/live_abc/accept";
  constexpr auto hangup = "/v1/live/sessions/live_abc/hangup";
  ClientAdmission admission;
  Configure(admission);
  HttpRequestHeaders headers;
  headers.client_id = "alpha";
  headers.gateway_token = std::string(64, 'a');
  ClientIdentity owner;
  int status = 0;
  ClientLease media, control;
  Q4T_CHECK(ClassifyHttpConnection("POST", accept) ==
            HttpConnectionClass::kRealtime);
  Q4T_CHECK(AdmitClientRequest("POST", accept, &headers, admission, &media,
                               &owner, &status));
  Q4T_CHECK(ClassifyHttpConnection("POST", hangup) ==
            HttpConnectionClass::kControl);
  Q4T_CHECK(AdmitClientRequest("POST", hangup, &headers, admission, &control,
                               &owner, &status));
  Q4T_CHECK(admission.Usage(0).realtime_active == 1 &&
            admission.Usage(0).control_active == 1);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/realtime/calls/rtc_abc/hangup") ==
            HttpConnectionClass::kControl);
  for (auto path : {"/v1/live/sessions/live_abc/refer",
                    "/v1/realtime/calls/rtc_abc/refer"}) {
    Q4T_CHECK(ClassifyHttpConnection("POST", path) ==
              HttpConnectionClass::kControl);
  }
  for (auto bad : {"/v1/live/sessions/live_/accept",
                   "/v1/live/sessions/rtc_other/accept",
                   "/v1/realtime/calls/live_other/hangup",
                   "/v1/realtime/calls/live_other/refer",
                   "/v1/live/sessions/live_id/accept?x=1"}) {
    Q4T_CHECK(ClassifyHttpConnection("POST", bad) ==
              HttpConnectionClass::kBusiness);
  }
  return true;
}

Q4T_TEST(websocket_session_each_turn_keeps_online_rate_and_data_admission) {
  ClientAdmission admission;
  const auto identity = Configure(admission);
  ClientLease connection;
  Q4T_CHECK(connection.Acquire(admission, identity, ClientLane::kRealtime));
  for (int turn = 0; turn < 3; ++turn) {
    ClientLease request, competing;
    Q4T_CHECK(request.Acquire(admission, identity, ClientLane::kData));
    Q4T_CHECK(!competing.Acquire(admission, identity, ClientLane::kData));
    ClientRateLease rate;
    Q4T_CHECK(rate.Reserve(admission, identity, 4, 8) ==
              (turn < 2 ? ClientRateResult::kAllowed : ClientRateResult::kRpm));
    if (turn < 2) {
      rate.StartPrompt(4);
      rate.Completion(3);
    }
  }
  const auto usage = admission.RateUsage(0);
  Q4T_CHECK(usage.requests == 2 && usage.tokens == 14 && usage.reserved == 0);
  Q4T_CHECK(admission.Usage(0).realtime_active == 1 &&
            admission.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(websocket_http_handoff_permanently_seals_http_and_exception_writes) {
  Sockets s;
  detail::HttpResponseContext context(s.f[0]);
  context.SetKeepAlive(true);
  Q4T_CHECK(context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "realtime"));
  Q4T_CHECK(context.Upgraded() && !context.KeepAlive() &&
            *context.State() == detail::ResponseState::kUpgraded);
  std::array<char, 1024> wire{};
  const auto size = recv(s.f[1], wire.data(), wire.size(), 0);
  Q4T_CHECK(size > 0);
  const std::string head(wire.data(), size);
  Q4T_CHECK(head.starts_with("HTTP/1.1 101 ") && head.ends_with("\r\n\r\n"));
  Q4T_CHECK(head.find("Content-Length") == head.npos &&
            head.find("Transfer-Encoding") == head.npos);
  Q4T_CHECK(
      !context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "realtime"));
  Q4T_CHECK(!detail::SendSimple(s.f[0], 200, "OK", "bad", "text/plain"));
  detail::SendExceptionResponse(s.f[0], *context.State());
  *context.State() =
      detail::ResponseState::kUnsent;  // Even a misbehaving caller.
  Q4T_CHECK(!detail::WriteAll(s.f[0], "no HTTP after upgrade"));
  detail::SendExceptionResponse(s.f[0], *context.State());
  *context.State() = detail::ResponseState::kStreaming;
  bool heartbeat_rejected = false;
  try {
    context.StartHeartbeats(
        std::make_shared<RequestControl>("heartbeat", "", Clock::now() + 2s),
        10s);
  } catch (const std::logic_error&) {
    heartbeat_rejected = true;
  }
  context.StopHeartbeats();
  Q4T_CHECK(heartbeat_rejected);
  Q4T_CHECK(!context.FinishResponse());
  Q4T_CHECK(recv(s.f[1], wire.data(), wire.size(), MSG_DONTWAIT) < 0 &&
            (errno == EAGAIN || errno == EWOULDBLOCK));
  return true;
}

Q4T_TEST(websocket_http_handoff_rejects_invalid_protocol_accept_and_http10) {
  Sockets s;
  detail::HttpResponseContext context(s.f[0]);
  Q4T_CHECK(!context.BeginWebSocket("bad", "realtime"));
  Q4T_CHECK(!context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "bad\r\n"));
  Q4T_CHECK(!context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+\r\n=", ""));
  context.SetKeepAlive(false, false);
  Q4T_CHECK(!context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", ""));
  Q4T_CHECK(*context.State() == detail::ResponseState::kUnsent &&
            !context.Upgraded());
  return true;
}

Q4T_TEST(websocket_http_active_heartbeat_cannot_be_promoted_to_upgrade) {
  Sockets s;
  detail::HttpResponseContext context(s.f[0]);
  *context.State() = detail::ResponseState::kStreaming;
  context.StartHeartbeats(
      std::make_shared<RequestControl>("heartbeat", "", Clock::now() + 2s),
      10s);
  *context.State() = detail::ResponseState::kUnsent;
  Q4T_CHECK(!context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", ""));
  context.StopHeartbeats();
  Q4T_CHECK(context.BeginWebSocket("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", ""));
  return true;
}

Q4T_TEST(websocket_session_final_peak_includes_producer_racing_cancellation) {
  Sockets s;
  WebSocketOutput output;
  bool pushed = false;
  const auto result = RunWebSocketSession(
      s.f[0], Frame("fail"), output,
      {[](auto, auto) -> bool { throw std::runtime_error("fixture"); },
       {},
       {},
       [&] {
         // Represents producer publication already runnable when cancellation
         // is signalled, before the mailbox is sealed. It never reaches wire.
         pushed =
             output.TryPush(WebSocketOpcode::kText, std::string(1000, 'x'));
       }},
      Limits());
  Q4T_CHECK(pushed && output.Closed() && output.Bytes() == 0);
  Q4T_CHECK(result.end == WebSocketSessionEnd::kApplication &&
            result.sent_bytes == 0 && result.peak_output_bytes >= 1004);
  Q4T_CHECK(result.peak_output_bytes == output.PeakBytes());
  return true;
}

Q4T_TEST(
    websocket_session_ticks_without_peer_input_and_closes_on_tick_failure) {
  for (const bool throws : {false, true}) {
    Sockets sockets;
    WebSocketOutput output;
    int ticks = 0, cancels = 0;
    WebSocketSessionCallbacks cb;
    cb.message = [](auto, auto) { return true; };
    cb.cancel = [&] { ++cancels; };
    cb.tick = [&]() -> bool {
      ++ticks;
      if (ticks == 1) return output.TryPush(WebSocketOpcode::kText, "ready");
      if (throws) throw std::runtime_error("tick fixture");
      return false;
    };
    const auto result =
        RunWebSocketSession(sockets.f[0], {}, output, cb, Limits());
    Q4T_CHECK(ticks == 2 && cancels == 1);
    Q4T_CHECK(result.end == WebSocketSessionEnd::kApplication);
    Q4T_CHECK(Read(sockets.f[1],
                   Frame("ready", WebSocketOpcode::kText, false).size()) ==
              Frame("ready", WebSocketOpcode::kText, false));
  }
  return true;
}

Q4T_TEST(websocket_session_completion_drains_all_frames_before_normal_close) {
  Sockets sockets;
  sockets.Small();
  WebSocketOutput output;
  const std::string audio(128 * 1024, 'a');
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kBinary, audio));
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "session.closed"));
  int calls = 0, cancels = 0;
  WebSocketSessionCallbacks cb;
  cb.message = [&](auto, auto) {
    ++calls;
    return true;
  };
  cb.cancel = [&] { ++cancels; };
  cb.completed = [] { return true; };
  auto task = std::async(std::launch::async, [&] {
    return RunWebSocketSession(sockets.f[0], Frame("too late"), output, cb,
                               Limits());
  });
  const auto expected = Frame(audio, WebSocketOpcode::kBinary, false) +
                        Frame("session.closed", WebSocketOpcode::kText, false) +
                        Frame("\x03\xe8", WebSocketOpcode::kClose, false);
  Q4T_CHECK(Read(sockets.f[1], expected.size()) == expected);
  const auto result = task.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kCompleted &&
            result.close_code == 1000 && result.sent_bytes == expected.size());
  Q4T_CHECK(calls == 0 && cancels == 1 && output.Closed() && !output.Bytes());
  return true;
}

Q4T_TEST(websocket_session_drains_error_before_1011) {
  Sockets sockets;
  sockets.Small();
  WebSocketOutput output;
  const std::string audio(128 * 1024, 'a');
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kBinary, audio));
  Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "error:tts_output_limit"));
  int calls = 0, cancels = 0;
  WebSocketSessionCallbacks cb;
  cb.message = [&](auto, auto) {
    ++calls;
    return true;
  };
  cb.cancel = [&] { ++cancels; };
  cb.failed_after_output = [] { return true; };
  auto task = std::async(std::launch::async, [&] {
    return RunWebSocketSession(sockets.f[0], Frame("too late"), output, cb,
                               Limits());
  });
  const auto expected = Frame(audio, WebSocketOpcode::kBinary, false) +
                        Frame("error:tts_output_limit", WebSocketOpcode::kText, false) +
                        Frame("\x03\xf3", WebSocketOpcode::kClose, false);
  Q4T_CHECK(Read(sockets.f[1], expected.size()) == expected);
  const auto result = task.get();
  Q4T_CHECK(result.end == WebSocketSessionEnd::kApplication &&
            result.close_code == 1011 && result.sent_bytes == expected.size());
  Q4T_CHECK(calls == 0 && cancels == 1 && output.Closed() && !output.Bytes());
  return true;
}

Q4T_TEST(websocket_session_completion_in_message_stops_same_batch_dispatch) {
  Sockets sockets;
  WebSocketOutput output;
  bool completed = false;
  int calls = 0;
  WebSocketSessionCallbacks cb;
  cb.message = [&](auto, auto) {
    ++calls;
    completed = true;
    return output.TryPush(WebSocketOpcode::kText, "session.closed");
  };
  cb.completed = [&] { return completed; };
  const auto result = RunWebSocketSession(
      sockets.f[0], Frame("close") + Frame("must not execute"), output, cb,
      Limits());
  const auto expected = Frame("session.closed", WebSocketOpcode::kText, false) +
                        Frame("\x03\xe8", WebSocketOpcode::kClose, false);
  Q4T_CHECK(result.end == WebSocketSessionEnd::kCompleted && calls == 1);
  Q4T_CHECK(Read(sockets.f[1], expected.size()) == expected);
  return true;
}

Q4T_TEST(websocket_session_completion_does_not_override_failure_or_revocation) {
  for (int kind = 0; kind < 4; ++kind) {
    Sockets sockets;
    WebSocketOutput output;
    Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "must not leak"));
    int cancels = 0;
    WebSocketSessionCallbacks cb;
    cb.message = [](auto, auto) { return true; };
    cb.stopped = [=] { return kind == 0; };
    cb.authorized = [=] { return kind != 1; };
    cb.tick = [=] { return kind != 2; };
    cb.completed = [=] {
      if (kind == 3) throw std::runtime_error("completion fixture");
      return true;
    };
    cb.cancel = [&] { ++cancels; };
    const auto result =
        RunWebSocketSession(sockets.f[0], {}, output, cb, Limits());
    const auto expected = kind == 0   ? WebSocketSessionEnd::kShutdown
                          : kind == 1 ? WebSocketSessionEnd::kRevoked
                                      : WebSocketSessionEnd::kApplication;
    Q4T_CHECK(result.end == expected && cancels == 1 && output.Closed());
    Q4T_CHECK(result.sent_bytes <= 4 && !output.Bytes());
  }
  return true;
}

Q4T_TEST(websocket_session_completion_slow_reader_and_eof_are_not_success) {
  for (bool eof : {false, true}) {
    Sockets sockets;
    sockets.Small();
    WebSocketOutput output;
    Q4T_CHECK(
        output.TryPush(WebSocketOpcode::kBinary, std::string(1ULL << 20, 'a')));
    Q4T_CHECK(output.TryPush(WebSocketOpcode::kText, "session.closed"));
    WebSocketSessionCallbacks cb;
    cb.message = [](auto, auto) { return true; };
    cb.completed = [] { return true; };
    auto limits = Limits();
    limits.write = 60ms;
    if (eof) Q4T_CHECK(shutdown(sockets.f[1], SHUT_WR) == 0);
    const auto start = Clock::now();
    const auto result =
        RunWebSocketSession(sockets.f[0], {}, output, cb, limits);
    Q4T_CHECK(result.end == (eof ? WebSocketSessionEnd::kPeerEof
                                 : WebSocketSessionEnd::kWriteDeadline));
    Q4T_CHECK(result.sent_bytes < (1ULL << 20) && Clock::now() - start < 1s);
    Q4T_CHECK(output.Closed() && !output.Bytes());
  }
  return true;
}


Q4T_TEST(realtime_call_accept_reserves_the_call_and_attach_uses_a_distinct_lane) {
  constexpr auto accept = "/v1/realtime/calls/rtc_valid/accept";
  constexpr auto attach = "/v1/realtime?call_id=rtc_valid";
  constexpr auto create = "/v1/realtime/calls";
  Q4T_CHECK(IsLiveMediaCreate("POST", create));
  Q4T_CHECK(!IsLiveMediaCreate("GET", create));
  Q4T_CHECK(ClassifyHttpConnection("POST", create) == HttpConnectionClass::kRealtime);
  Q4T_CHECK(IsLiveMediaCreate("POST", accept));
  Q4T_CHECK(ClassifyHttpConnection("POST", accept) == HttpConnectionClass::kRealtime);
  Q4T_CHECK(RealtimeSidebandId(attach) == "rtc_valid");
  Q4T_CHECK(ClassifyHttpConnection("GET", attach) == HttpConnectionClass::kSideband);
  for (auto bad : {"/v1/realtime?call_id=live_x", "/v1/realtime?call_id=rtc_",
                   "/v1/realtime?call_id=rtc_a&model=x",
                   "/v1/realtime?call_id=rtc_a&call_id=rtc_b"})
    Q4T_CHECK(RealtimeSidebandId(bad).empty());
  ClientAdmission admission;
  Configure(admission);
  ClientLease active, observer;
  ClientIdentity identity;
  HttpRequestHeaders headers;
  headers.client_id = "alpha";
  headers.gateway_token = std::string(64, 'a');
  int status = 0;
  Q4T_CHECK(AdmitClientRequest("POST", accept, &headers, admission, &active,
                               &identity, &status));
  Q4T_CHECK(AdmitClientRequest("GET", attach, &headers, admission, &observer,
                               &identity, &status));
  Q4T_CHECK(admission.Usage(0).realtime_active == 1 &&
            admission.Usage(0).sideband_active == 1);
  return true;
}
