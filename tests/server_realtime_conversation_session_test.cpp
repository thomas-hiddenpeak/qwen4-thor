#include "q4t/server/realtime_conversation_session.h"

#include <algorithm>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
namespace io = q4t::io;
using Clock = std::chrono::steady_clock;
Json User(std::string id, std::string text) {
  return Object(
      {{"type", String("message")},
       {"id", String(std::move(id))},
       {"role", String("user")},
       {"content", Array({Object({{"type", String("input_text")},
                                  {"text", String(std::move(text))}})})}});
}
Json OutputMessage(std::string text) {
  return Object(
      {{"role", String("assistant")}, {"content", String(std::move(text))}});
}
Json OutputItem(std::string text, bool final = false) {
  return Object(
      {{"type", String("message")},
       {"id", String("item_output")},
       {"object", String("realtime.item")},
       {"role", String("assistant")},
       {"status", String(final ? "completed" : "in_progress")},
       {"content", Array({Object({{"type", String("output_text")},
                                  {"text", String(std::move(text))}})})}});
}
struct Fixture {
  WebSocketOutput output;
  RequestRegistry requests;
  RealtimeConversationCallbacks cb;
  std::atomic<size_t> calls{0}, released{0}, emitted{0};
  std::atomic<bool> hold{false}, entered{false};
  std::atomic<int> mode{0};  // 1=function, 2=throw, 3=mismatch, 4=backpressure.
  std::mutex mutex;
  std::vector<RealtimeConversationJob> snapshots;
  std::vector<Json> events;
  std::unique_ptr<RealtimeConversationSession> session;
  Json settings = Object({{"type", String("realtime")},
                          {"model", String("q4t-test")},
                          {"instructions", String("old")},
                          {"output_modalities", Array({String("text")})}});
  Fixture() {
    cb.validate = [](const Json& value) {
      return value.GetString("instructions") == "reject"
                 ? object_detail::Error(400, "fixture_setting_rejected",
                                        "Rejected setting")
                 : ObjectReply{};
    };
    cb.prepare = [](const auto&) { return ObjectReply{}; };
    cb.acquire = [&](const auto& id) {
      return requests.Register(id, "", Clock::now() + 5s);
    };
    cb.release = [&](const auto& control) {
      requests.Remove(control);
      ++released;
    };
    cb.execute = [&](const RealtimeConversationJob& job, const auto& control,
                     const RealtimeDeltaSink& emit) {
      ++calls;
      {
        std::lock_guard lock(mutex);
        snapshots.push_back(job);
      }
      entered = true;
      while (hold && !control->Cancelled()) std::this_thread::sleep_for(1ms);
      RealtimeGenerationResult result;
      if (control->Cancelled()) {
        result.reply =
            object_detail::Error(409, control->Reason(), "Fixture cancelled");
        return result;
      }
      if (mode == 2) throw std::runtime_error("Fixture worker failure");
      if (mode == 4) {
        for (size_t i = 0; i < 10000; ++i) {
          ResponseDelta delta;
          delta.text = std::string(1024, 'x');
          result.reply = emit(delta);
          if (!result.reply.ok()) return result;
          ++emitted;
        }
        result.message = OutputMessage("unreachable");
        return result;
      }
      Json request;
      io::ParseJson(R"({"messages":[{"role":"user","content":"fixture"}],
        "tools":[{"type":"function","function":{"name":"lookup",
        "parameters":{"type":"object","properties":{"text":{"type":"string"}}}}}]})",
                    &request);
      ChatResponseParser parser;
      auto status =
          parser.Init(request, "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                      control->id(), true);
      std::vector<ResponseDelta> deltas;
      const std::string text =
          mode == 1 ? "<tool_call><function=lookup><parameter=text>value</"
                      "parameter></function></tool_call>"
                    : "answer";
      if (status) status = parser.Append(text, &deltas);
      if (status) status = parser.Finish(false, &deltas);
      if (!status) {
        result.reply = object_detail::Error(500, "fixture_parser_failed",
                                            status.message());
        return result;
      }
      for (const auto& delta : deltas) {
        result.reply = emit(delta);
        if (!result.reply.ok()) return result;
      }
      io::ParseJson(parser.MessageJson(), &result.message);
      if (mode == 3) result.message = OutputMessage("wrong");
      return result;
    };
  }
  bool Read() {
    while (!output.Front().empty()) {
      const auto frame = output.Front();
      WebSocketDecoder decoder(false, kWebSocketMessageLimit);
      size_t used = 0;
      if (decoder.Consume(
              {reinterpret_cast<const uint8_t*>(frame.data()), frame.size()},
              &used) != WebSocketRead::kEvent ||
          used != frame.size())
        return false;
      Json event;
      if (!io::ParseJson(std::string(decoder.Event().payload), &event))
        return false;
      ChatPlanError error;
      auto status =
          ValidateRealtimeWireShape(event, RealtimeShape::kServerEvent, &error);
      if (!status) {
        std::fprintf(stderr, "Session SDK mismatch %s at %s: %s\n",
                     event.GetString("type").c_str(), error.param.c_str(),
                     status.message().c_str());
        return false;
      }
      if (event.GetString("type") == "error")
        std::fprintf(stderr, "Fixture received %s\n",
                     OutputJson(event).c_str());
      events.push_back(std::move(event));
      output.Acknowledge();
    }
    return true;
  }
  bool Start() {
    session = std::make_unique<RealtimeConversationSession>(
        "sess_fixture", "conv_fixture", settings, output, cb);
    return session->Start() && Read();
  }
  bool Send(const Json& event) {
    return session->Message(WebSocketOpcode::kText, OutputJson(event)) &&
           Read();
  }
  bool Create(const Json& response = Object()) {
    return Send(
        Object({{"type", String("response.create")}, {"response", response}}));
  }
  bool Add(const Json& item, std::string previous = {}) {
    auto event =
        Object({{"type", String("conversation.item.create")}, {"item", item}});
    if (!previous.empty()) Set(&event, "previous_item_id", String(previous));
    return Send(event);
  }
  bool Wait(const std::function<bool()>& predicate, bool tick = true) {
    const auto deadline = Clock::now() + 3s;
    while (Clock::now() < deadline) {
      if (tick && (!session->Tick() || !Read())) return false;
      if (predicate()) return true;
      std::this_thread::sleep_for(1ms);
    }
    return false;
  }
  bool Finish() {
    return Wait([&] { return session->ActiveResponses() == 0; });
  }
  size_t Count(std::string_view type) const {
    return std::count_if(events.begin(), events.end(), [&](const auto& e) {
      return e.GetString("type") == type;
    });
  }
  Json Last(std::string_view type) const {
    for (auto it = events.rbegin(); it != events.rend(); ++it)
      if (it->GetString("type") == type) return *it;
    return {};
  }
  RealtimeConversationJob Snapshot(size_t index) {
    std::lock_guard lock(mutex);
    return snapshots.at(index);
  }
};
}  // namespace

Q4T_TEST(realtime_session_item_order_retrieve_delete_and_sdk_events) {
  Fixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.Count("session.created") == 1 &&
            f.Count("conversation.created") == 1);
  Q4T_CHECK(f.Add(User("item_b", "B")) && f.Add(User("item_a", "A"), "root"));
  Q4T_CHECK(f.Add(User("item_c", "C"), "item_a"));
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.retrieve")},
                           {"item_id", String("item_c")}})));
  Q4T_CHECK(
      f.Last("conversation.item.retrieved").Find("item")->GetString("id") ==
      "item_c");
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String("item_b")}})));
  Q4T_CHECK(f.Create() && f.Finish());
  const auto job = f.Snapshot(0);
  Q4T_CHECK(job.snapshot.items.array.size() == 2);
  Q4T_CHECK(job.snapshot.items.array[0].GetString("id") == "item_a");
  Q4T_CHECK(job.snapshot.items.array[1].GetString("id") == "item_c");
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "completed");
  Q4T_CHECK(f.calls == 1 && f.released == 1);
  return true;
}

Q4T_TEST(realtime_session_function_result_returns_to_actual_history_snapshot) {
  Fixture f;
  f.mode = 1;
  Q4T_CHECK(f.Start() && f.Add(User("item_query", "lookup")));
  Q4T_CHECK(f.Create() && f.Finish());
  const auto response = *f.Last("response.done").Find("response");
  const auto& call = response.GetArray("output")->array[0];
  Q4T_CHECK(call.GetString("type") == "function_call" &&
            call.GetString("status") == "completed");
  Q4T_CHECK(f.Add(Object({{"type", String("function_call_output")},
                          {"call_id", String(call.GetString("call_id"))},
                          {"output", String("actual fixture tool result")}})));
  f.mode = 0;
  Q4T_CHECK(f.Create() && f.Finish());
  const auto job = f.Snapshot(1);
  Q4T_CHECK(job.snapshot.items.array.size() == 3);
  Q4T_CHECK(job.snapshot.items.array[2].GetString("output") ==
            "actual fixture tool result");
  Q4T_CHECK(f.Count("response.done") == 2 && f.calls == 2 && f.released == 2);
  return true;
}

Q4T_TEST(
    realtime_session_settings_atomic_merge_and_response_override_snapshot) {
  Fixture f;
  Q4T_CHECK(f.Start());
  auto update =
      Object({{"type", String("session.update")},
              {"session", Object({{"type", String("realtime")},
                                  {"instructions", String("reject")}})}});
  Q4T_CHECK(f.Send(update) && f.Count("session.updated") == 0 &&
            f.Count("error") == 1);
  Field(Field(update, "session"), "instructions") = String("");
  Set(&Field(update, "session"), "audio",
      Object({{"input",
               Object({{"format", Object({{"type", String("audio/pcm")},
                                          {"rate", Number(24000)}})}})}}));
  Q4T_CHECK(f.Send(update) && f.Count("session.updated") == 1);
  Set(&update, "session",
      Object({{"type", String("realtime")},
              {"audio",
               Object({{"input", Object({{"noise_reduction", Json{}}})}})}}));
  Q4T_CHECK(f.Send(update) && f.Count("session.updated") == 2);
  const auto& effective = *f.events.back().Find("session");
  Q4T_CHECK(effective.GetString("instructions") == "");
  Q4T_CHECK(effective.Find("audio")->Find("input")->Find("format")->GetString(
                "type") == "audio/pcm");
  Q4T_CHECK(effective.Find("audio")
                ->Find("input")
                ->Find("noise_reduction")
                ->IsNull());
  Q4T_CHECK(f.Create(Object({{"instructions", String("one turn")}})) &&
            f.Finish());
  Q4T_CHECK(f.Snapshot(0).settings.GetString("instructions") == "");
  Q4T_CHECK(f.Snapshot(0).response.GetString("instructions") == "one turn");
  return true;
}

Q4T_TEST(realtime_session_default_single_writer_and_bounded_out_of_band) {
  Fixture f;
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Add(User("item_a", "context")) && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.entered.load(); }));
  Q4T_CHECK(f.Create() && f.Count("response.created") == 1);
  const auto isolated =
      Object({{"conversation", String("none")}, {"input", Array()}});
  for (size_t i = 0; i < 3; ++i) Q4T_CHECK(f.Create(isolated));
  Q4T_CHECK(f.session->ActiveResponses() == 4);
  Q4T_CHECK(f.Create(isolated) && f.Count("response.created") == 4);
  f.hold = false;
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.calls == 4 && f.released == 4 && f.Count("response.done") == 4);
  Q4T_CHECK(f.Count("conversation.item.done") ==
            2);  // Input plus default output.
  return true;
}

Q4T_TEST(realtime_session_cancel_is_scoped_and_does_not_restart_other_work) {
  Fixture f;
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Create());
  Q4T_CHECK(
      f.Create(Object({{"conversation", String("none")}, {"input", Array()}})));
  Q4T_CHECK(f.Wait([&] { return f.calls == 2; }));
  const auto independent =
      f.Last("response.created").Find("response")->GetString("id");
  Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")},
                           {"response_id", String(independent)}})));
  Q4T_CHECK(f.Wait([&] { return f.released == 1; }));
  Q4T_CHECK(f.session->ActiveResponses() == 1);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "cancelled");
  f.hold = false;
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.calls == 2 && f.released == 2 && f.Count("response.done") == 2);
  return true;
}

Q4T_TEST(realtime_session_deleted_output_is_not_resurrected_by_late_deltas) {
  Fixture f;
  std::atomic<bool> first{false}, resume{false};
  f.cb.execute = [&](const auto&, const auto& control,
                     const RealtimeDeltaSink& emit) {
    RealtimeGenerationResult result;
    ResponseDelta delta;
    delta.text = "first";
    result.reply = emit(delta);
    first = true;
    while (!resume && !control->Cancelled()) std::this_thread::sleep_for(1ms);
    delta.text = "last";
    if (!control->Cancelled()) result.reply = emit(delta);
    result.message = OutputMessage("firstlast");
    return result;
  };
  Q4T_CHECK(f.Start() && f.Create());
  Q4T_CHECK(
      f.Wait([&] { return first && f.Count("conversation.item.added") == 1; }));
  const auto id =
      f.Last("conversation.item.added").Find("item")->GetString("id");
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String(id)}})));
  resume = true;
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.session->HistoryBytes() == 0);
  Q4T_CHECK(f.Count("conversation.item.done") == 0);
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.retrieve")},
                           {"item_id", String(id)}})));
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "realtime_item_not_found");
  return true;
}

Q4T_TEST(realtime_session_worker_failure_and_mismatch_keep_honest_terminal) {
  for (const int mode : {2, 3}) {
    Fixture f;
    f.mode = mode;
    Q4T_CHECK(f.Start() && f.Create() && f.Finish());
    Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
              "failed");
    f.mode = 0;
    Q4T_CHECK(f.Create() && f.Finish());
    Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
              "completed");
    Q4T_CHECK(f.calls == 2 && f.released == 2 && f.Count("response.done") == 2);
  }
  return true;
}

Q4T_TEST(realtime_session_invalid_commands_do_not_mutate_or_start_model) {
  Fixture f;
  Q4T_CHECK(f.Start());
  for (
      const auto* text :
      {R"({"type":"response.create","type":"response.create"})",
       R"({"type":"response.create","response":{"max_output_tokens":null}})",
       R"({"type":"session.update","session":{"type":"realtime","unknown":true}})"})
    Q4T_CHECK(f.session->Message(WebSocketOpcode::kText, text) && f.Read());
  Q4T_CHECK(f.Count("error") == 3 && f.calls == 0 &&
            f.session->HistoryBytes() == 0);
  return true;
}

Q4T_TEST(
    realtime_session_cancel_wakes_a_backpressured_worker_and_releases_owner) {
  Fixture f;
  f.mode = 4;
  Q4T_CHECK(f.Start() && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.emitted >= 256; }, false));
  Q4T_CHECK(f.emitted == 256);
  Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")}})));
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.calls == 1 && f.released == 1);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "cancelled");
  return true;
}

Q4T_TEST(realtime_session_snapshot_is_independent_of_subsequent_deletion) {
  Fixture f;
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Add(User("item_a", "original")) && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.entered.load(); }));
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String("item_a")}})));
  f.hold = false;
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.Snapshot(0).snapshot.items.array[0].GetString("id") == "item_a");
  return true;
}

Q4T_TEST(realtime_session_wrong_owner_is_neither_cancelled_nor_released) {
  Fixture f;
  auto other =
      std::make_shared<RequestControl>("resp_other", "", Clock::now() + 5s);
  f.cb.acquire = [&](const auto&) { return other; };
  Q4T_CHECK(f.Start() && f.Create());
  Q4T_CHECK(f.calls == 0 && f.released == 0 && !other->Cancelled());
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "realtime_request_owner_invalid");
  return true;
}

Q4T_TEST(realtime_conversation_atomic_growth_respects_actual_global_capacity) {
  RealtimeConversation history("sess_growth", 8, 32768);
  RealtimeItemTicket ticket;
  Q4T_CHECK(
      history.BeginOutput(OutputItem(""), "item_output", true, 8192, &ticket));
  const auto grown = OutputItem(std::string(12000, 'a'));
  Q4T_CHECK(!history.Update(ticket, grown, false));
  Q4T_CHECK(history.Update(ticket, grown, false, true));
  const auto charged = history.ChargedBytes(), revision = history.Revision();
  Q4T_CHECK(charged > 8192 && charged <= 32768);
  Q4T_CHECK(!history.Update(ticket, OutputItem(std::string(40000, 'a')), false,
                            true));
  Q4T_CHECK(history.ChargedBytes() == charged &&
            history.Revision() == revision);
  Json retained;
  Q4T_CHECK(history.Get(ticket, &retained));
  Q4T_CHECK(retained.GetArray("content")->array[0].GetString("text").size() ==
            12000);
  Q4T_CHECK(history.Update(ticket, OutputItem("done", true), true, true));
  Q4T_CHECK(history.ChargedBytes() < charged);
  return true;
}

Q4T_TEST(realtime_wire_documented_null_clears_preserve_exact_sdk_discrepancy) {
  auto input = Object({{"noise_reduction", Json{}}, {"transcription", Json{}}});
  const auto session = Object(
      {{"type", String("realtime")}, {"audio", Object({{"input", input}})}});
  ChatPlanError error;
  for (const auto* type :
       {"session.update", "session.created", "session.updated"}) {
    auto event = Object({{"type", String(type)},
                         {"event_id", String("event_a")},
                         {"session", session}});
    const auto shape = std::string_view(type) == "session.update"
                           ? RealtimeShape::kClientEvent
                           : RealtimeShape::kServerEvent;
    const auto original = OutputJson(event);
    Q4T_CHECK(!ValidateRealtimeShape(event, shape, &error));
    Q4T_CHECK(ValidateRealtimeWireShape(event, shape, &error));
    Q4T_CHECK(OutputJson(event) == original);
  }
  Q4T_CHECK(!ValidateRealtimeShape(session, RealtimeShape::kSession, &error));
  Q4T_CHECK(
      ValidateRealtimeWireShape(session, RealtimeShape::kSession, &error));
  return true;
}

Q4T_TEST(realtime_wire_null_projection_never_hides_duplicates_or_other_errors) {
  for (
      const auto* text :
      {R"({"type":"session.update","session":{"type":"realtime","audio":{"input":{"noise_reduction":null,"noise_reduction":null}}}})",
       R"({"type":"session.update","session":{"type":"realtime","audio":{"input":{"noise_reduction":null,"format":null}}}})",
       R"({"type":"session.update","session":{"type":"realtime","audio":{"input":{"transcription":null}},"instructions":null}})",
       R"({"type":"session.update","session":{"type":"realtime","audio":{"input":{"transcription":null,"unknown":true}}}})",
       R"({"type":"session.update","session":{"type":"realtime","audio":{"input":{"noise_reduction":null},"input":{"noise_reduction":null}}}})",
       R"({"type":"response.create","response":{"audio":{"input":{"noise_reduction":null}}}})"}) {
    Json value;
    Q4T_CHECK(io::ParseJson(text, &value));
    const auto original = OutputJson(value);
    ChatPlanError error;
    Q4T_CHECK(
        !ValidateRealtimeWireShape(value, RealtimeShape::kClientEvent, &error));
    Q4T_CHECK(OutputJson(value) == original);
  }
  return true;
}
