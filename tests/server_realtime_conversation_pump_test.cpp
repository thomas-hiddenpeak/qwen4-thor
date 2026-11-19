#include "q4t/server/realtime_conversation_pump.h"

#include <stdexcept>
#include <cstdlib>
#include <filesystem>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
bool Wait(const std::function<bool()>& predicate) {
  const auto end = Clock::now() + 3s;
  while (Clock::now() < end) {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
struct Fixture {
  WebSocketOutput output;
  RequestRegistry requests;
  RealtimeConversationCallbacks cb;
  std::atomic<int> calls{0}, released{0};
  std::string context;
  Json settings = Object({{"type", String("realtime")},
                          {"model", String("q4t-test")},
                          {"output_modalities", Array({String("text")})}});
  Fixture() {
    cb.validate = [](const auto&) { return ObjectReply{}; };
    cb.prepare = [](const auto&) { return ObjectReply{}; };
    cb.acquire = [&](const auto& id) {
      return requests.Register(id, "", Clock::now() + 5s);
    };
    cb.release = [&](const auto& value) {
      requests.Remove(value);
      ++released;
    };
    cb.execute = [&](const auto& job, const auto&, const auto& emit) {
      ++calls;
      context = OutputJson(job.snapshot.items);
      ResponseDelta delta;
      delta.text = "fixture answer";
      RealtimeGenerationResult result;
      result.reply = emit(delta);
      result.message = Object({{"role", String("assistant")},
                               {"content", String("fixture answer")}});
      return result;
    };
  }
};
constexpr auto kItem =
    R"({"type":"conversation.item.create","item":{"type":"message","role":"user","content":[{"type":"input_text","text":"ordered fixture"}]}})";
constexpr auto kResponse = R"({"type":"response.create"})";
constexpr auto kHold =
    R"({"type":"session.update","session":{"type":"realtime","instructions":"hold"}})";
constexpr auto kClear =
    R"({"type":"session.update","session":{"type":"realtime","instructions":""}})";
}  // namespace

Q4T_TEST(realtime_conversation_pump_keeps_item_before_generation_snapshot) {
  Fixture f;
  RealtimeConversationPump pump("sess_pump", "conv_pump", f.settings, f.output,
                                f.cb);
  Q4T_CHECK(pump.Start() && !pump.Start());
  Q4T_CHECK(pump.Message(WebSocketOpcode::kText, kItem));
  Q4T_CHECK(pump.Message(WebSocketOpcode::kText, kResponse));
  Q4T_CHECK(Wait([&] { return f.released == 1; }));
  Q4T_CHECK(f.context.find("ordered fixture") != std::string::npos);
  Q4T_CHECK(f.calls == 1 && pump.Alive());
  Q4T_CHECK(Wait(
      [&] { return pump.InputBytes() == 0 && pump.InputMessages() == 0; }));
  pump.Stop();
  pump.Join();
  Q4T_CHECK(!pump.Alive() && !pump.Message(WebSocketOpcode::kText, kResponse));
  return true;
}

Q4T_TEST(realtime_conversation_pump_active_parse_stays_charged_until_done) {
  for (const bool bytes : {false, true}) {
    Fixture f;
    std::atomic<bool> entered{false}, release{false};
    f.cb.validate = [&](const auto& settings) {
      if (settings.GetString("instructions") == "hold") {
        entered = true;
        while (!release.load()) std::this_thread::sleep_for(1ms);
      }
      return ObjectReply{};
    };
    RealtimeConversationPump pump("sess_pump", "conv_pump", f.settings,
                                  f.output, f.cb);
    ScopeExit unblock([&] { release = true; });
    Q4T_CHECK(pump.Start() && pump.Message(WebSocketOpcode::kText, kHold));
    Q4T_CHECK(Wait([&] { return entered.load(); }));
    Q4T_CHECK(pump.InputMessages() == 1 &&
              pump.InputBytes() >= std::string_view(kHold).size());
    if (bytes) {
      const std::string large(8ULL << 20, ' ');
      Q4T_CHECK(pump.Message(WebSocketOpcode::kText, large));
      Q4T_CHECK(!pump.CanAccept(large.size()) &&
                !pump.Message(WebSocketOpcode::kText, large));
    } else {
      for (int i = 0; i < 7; ++i)
        Q4T_CHECK(pump.Message(WebSocketOpcode::kText, kClear));
      Q4T_CHECK(pump.InputMessages() == 8 &&
                !pump.CanAccept(std::string_view(kClear).size()));
      Q4T_CHECK(!pump.Message(WebSocketOpcode::kText, kClear));
    }
    Q4T_CHECK(pump.InputBytes() <= kRealtimeInboxBytes);
    pump.Stop();
    release = true;
    pump.Join();
    Q4T_CHECK(pump.InputBytes() == 0 && pump.InputMessages() == 0 &&
              f.calls == 0);
  }
  return true;
}

Q4T_TEST(realtime_conversation_pump_stop_cancels_and_joins_actual_owner_once) {
  Fixture f;
  f.cb.execute = [&](const auto&, const auto& control, const auto&) {
    ++f.calls;
    while (!control->Cancelled()) std::this_thread::sleep_for(1ms);
    RealtimeGenerationResult result;
    result.reply = Error(409, control->Reason(), "Fixture cancellation");
    return result;
  };
  RealtimeConversationPump pump("sess_pump", "conv_pump", f.settings, f.output,
                                f.cb);
  Q4T_CHECK(pump.Start() && pump.Message(WebSocketOpcode::kText, kResponse));
  Q4T_CHECK(Wait([&] { return f.calls == 1; }));
  pump.Stop();
  pump.Join();
  pump.Join();
  Q4T_CHECK(!pump.Alive() && f.released == 1 && pump.InputBytes() == 0);
  return true;
}

Q4T_TEST(
    realtime_conversation_pump_callback_failure_is_local_and_not_replayed) {
  Fixture f;
  f.cb.validate = [](const auto& settings) {
    if (settings.GetString("instructions") == "hold")
      throw std::runtime_error("Fixture validation failure");
    return ObjectReply{};
  };
  RealtimeConversationPump failed("sess_failed", "conv_failed", f.settings,
                                  f.output, f.cb);
  Q4T_CHECK(failed.Start() && failed.Message(WebSocketOpcode::kText, kHold));
  Q4T_CHECK(Wait([&] { return !failed.Alive(); }));
  failed.Join();
  Q4T_CHECK(f.calls == 0 && failed.InputBytes() == 0);
  Fixture good;
  RealtimeConversationPump healthy("sess_good", "conv_good", good.settings,
                                   good.output, good.cb);
  Q4T_CHECK(healthy.Start() &&
            healthy.Message(WebSocketOpcode::kText, kResponse));
  Q4T_CHECK(Wait([&] { return good.released == 1; }));
  Q4T_CHECK(good.calls == 1 && healthy.Alive());
  healthy.Stop();
  healthy.Join();
  return true;
}

Q4T_TEST(realtime_conversation_prompt_lookup_disconnect_cancels_object_worker) {
  ObjectWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  for (const bool disconnect : {true, false}) {
    Fixture f;
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(output ? output : ".q4t-work/prompt");
    std::filesystem::create_directories(root);
    auto path = (root / "prompt-pump-XXXXXX").string();
    Q4T_CHECK(mkdtemp(path.data()));
    ObjectStoreOptions options;
    options.directory = path + "/objects";
    ObjectStore store(options);
    std::atomic<bool> entered{false}, returned{false};
    std::atomic<int> http_status{0};
    f.cb.resolve_prompt = [&](const auto& reference, size_t budget,
                              const auto& cancelled, auto* out) {
      entered = true;
      auto reply = ResolveRealtimePrompt(
          &store, reference, {"client:pump", Clock::now() + 5s, cancelled},
          budget, out);
      http_status = reply.http_status;
      returned = true;
      return reply;
    };
    RealtimeConversationPump pump("sess_pump", "conv_pump", f.settings,
                                  f.output, f.cb);
    Q4T_CHECK(pump.Start());
    Q4T_CHECK(pump.Message(
        WebSocketOpcode::kText,
        R"({"type":"session.update","session":{"type":"realtime","prompt":{"id":"prompt_missing"}}})"));
    Q4T_CHECK(Wait([&] { return entered && store.Health().workers_started; }));
    Q4T_CHECK(pump.InputMessages() == 1 && !returned);
    Q4T_CHECK(pump.Message(WebSocketOpcode::kText, kResponse));
    const auto begin = Clock::now();
    if (disconnect) {
      pump.Stop();
      pump.Join();
      Q4T_CHECK(returned && http_status == 499 && f.calls == 0);
    } else {
      Q4T_CHECK(Wait([&] { return f.released == 1; }));
      Q4T_CHECK(returned && http_status >= 400 && pump.Alive());
      Q4T_CHECK(f.calls == 1);
      pump.Stop();
      pump.Join();
    }
    Q4T_CHECK(Clock::now() - begin < 1s);
    Q4T_CHECK(pump.InputBytes() == 0 && pump.InputMessages() == 0);
  }
  return true;
}
