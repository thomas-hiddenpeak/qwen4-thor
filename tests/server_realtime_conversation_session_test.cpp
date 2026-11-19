#include "q4t/server/realtime_conversation_session.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"
#include "q4t/io/base64.h"
#include "q4t/server/realtime_transcription_protocol.h"

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
                     const RealtimeDeltaSink& emit, const RealtimeSpeechSink&) {
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
struct PromptFixture : Fixture {
  ObjectStoreOptions options;
  ChatPlanConfig config;
  std::unique_ptr<ObjectStore> objects;
  std::unique_ptr<PromptRegistry> prompts;
  size_t lookups = 0;
  std::string scope = "client:session";
  bool authorized = true;
  StateAccess Access() const { return {scope, Clock::now() + 5s, {}}; }
  PromptFixture() {
    config.model = "q4t-test";
    config.structured = true;
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(output ? output : ".q4t-work/prompt");
    std::filesystem::create_directories(root);
    auto path = (root / "prompt-session-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Prompt test path");
    options.directory = path + "/objects";
    objects = std::make_unique<ObjectStore>(options);
    prompts = std::make_unique<PromptRegistry>(*objects);
    cb.resolve_prompt = [&](const auto& reference, size_t budget,
                            const auto& cancelled, auto* out) {
      ++lookups;
      auto access = Access();
      access.cancelled = cancelled;
      return ResolveRealtimePrompt(objects.get(), reference, access, budget,
                                   out);
    };
    const auto validate = [this](const Json& selected, const Json& response,
                                 const Json& items,
                                 const PromptSnapshot* prompt) {
      if (!authorized || (prompt && prompt->scope != scope))
        return object_detail::Error(401, "client_authentication_failed",
                                    "Fixture identity revoked");
      RealtimeChatPlan plan;
      ChatPlanError error;
      const auto status = PlanRealtimeChat(selected, response, items, config,
                                           &plan, &error, prompt);
      return status ? ObjectReply{}
                    : object_detail::Error(error.http_status, error.code,
                                           status.message());
    };
    cb.validate_prompt = [validate](const auto& selection) {
      return validate(selection.settings, Object(), Array(),
                      selection.prompt ? &*selection.prompt : nullptr);
    };
    cb.prepare = [validate](const auto& job) {
      return validate(job.settings, job.response, job.snapshot.items,
                      job.prompt ? &*job.prompt : nullptr);
    };
  }
  Json Definition(std::string text, std::string prefix) {
    return Object({{"api", String("realtime")},
                   {"variables", Object()},
                   {"request", Object({{"instructions", String(text)},
                                       {"input", String(prefix)},
                                       {"max_output_tokens", Number(32)}})}});
  }
  Json Register(std::string text, std::string prefix = "private prefix") {
    const auto result =
        prompts->Create(Definition(text, prefix), config, Access());
    if (!result.ok()) throw std::runtime_error(result.message);
    return Object({{"id", String(result.value.GetString("id"))}});
  }
  bool Update(Json update) {
    Set(&update, "type", String("realtime"));
    return Send(Object({{"type", String("session.update")},
                        {"event_id", String("event_prompt")},
                        {"session", std::move(update)}}));
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
                     const RealtimeDeltaSink& emit, const RealtimeSpeechSink&) {
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

Q4T_TEST(realtime_session_bad_metadata_is_error_before_publication_or_owner) {
  Fixture f;
  Q4T_CHECK(f.Start());
  auto metadata = Object({{"key", String(std::string(513, 'x'))}});
  Q4T_CHECK(f.Send(Object({{"type", String("response.create")},
                           {"event_id", String("bad_metadata")},
                           {"response", Object({{"metadata", metadata}})}})));
  Q4T_CHECK(f.Count("error") == 1 && f.Count("response.created") == 0 &&
            f.calls == 0 && f.released == 0);
  const auto received = f.Last("error");
  const auto* error = received.Find("error");
  Q4T_CHECK(error && error->GetString("event_id") == "bad_metadata" &&
            error->GetString("param") == "response.metadata");
  Q4T_CHECK(f.Create() && f.Finish());
  return true;
}

Q4T_TEST(realtime_session_successful_context_cut_is_used_by_next_response) {
  Fixture f;
  const auto original = f.cb.execute;
  f.cb.execute = [&](const auto& job, const auto& control, const auto& emit,
                     const RealtimeSpeechSink& speech) {
    auto result = original(job, control, emit, speech);
    if (f.calls == 1) result.retained_input_indices = std::vector<size_t>{1};
    return result;
  };
  Q4T_CHECK(f.Start() && f.Add(User("old", "old")) &&
            f.Add(User("current", "current")));
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(f.Create() && f.Finish());
  const auto second = f.Snapshot(1);
  Q4T_CHECK(second.snapshot.items.array.size() == 2);
  Q4T_CHECK(second.snapshot.items.array[0].Find("content")->array[0].GetString(
                "text") == "current");
  return true;
}

Q4T_TEST(realtime_session_out_of_band_context_cut_does_not_change_default) {
  Fixture f;
  const auto original = f.cb.execute;
  f.cb.execute = [&](const auto& job, const auto& control, const auto& emit,
                     const RealtimeSpeechSink& speech) {
    auto result = original(job, control, emit, speech);
    if (f.calls == 1) result.retained_input_indices = std::vector<size_t>{1};
    return result;
  };
  Q4T_CHECK(f.Start() && f.Add(User("old", "old")) &&
            f.Add(User("current", "current")));
  Q4T_CHECK(f.Create(Object({{"conversation", String("none")}})) && f.Finish());
  Q4T_CHECK(f.Create() && f.Finish());
  const auto second = f.Snapshot(1);
  Q4T_CHECK(second.snapshot.items.array.size() == 2);
  Q4T_CHECK(second.snapshot.items.array[0].Find("content")->array[0].GetString(
                "text") == "old");
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

Q4T_TEST(
    realtime_session_prompt_pins_actual_registry_version_and_private_input) {
  PromptFixture f;
  auto reference = f.Register("template instruction");
  Q4T_CHECK(f.Start() && f.Update(Object({{"prompt", reference}})));
  const auto selected = *f.Last("session.updated").Find("session");
  Q4T_CHECK(selected.GetString("instructions") == "template instruction" &&
            selected.Find("prompt")->GetString("version") == "1" &&
            !selected.Has("input") && f.session->HistoryBytes() == 0);
  Q4T_CHECK(f.prompts
                ->Append(reference.GetString("id"), 1,
                         f.Definition("new", "new prefix"), f.config,
                         f.Access())
                .ok());
  Q4T_CHECK(f.prompts->Erase(reference.GetString("id"), f.Access()).ok());
  Q4T_CHECK(f.Update(Object({{"max_output_tokens", Number(28)}})) &&
            f.Create() && f.Finish());
  const auto job = f.Snapshot(0);
  Q4T_CHECK(f.lookups == 1 && job.prompt &&
            job.prompt->scope == "client:session" &&
            job.prompt->reference.GetString("version") == "1");
  Q4T_CHECK(job.settings.GetString("instructions") == "template instruction" &&
            job.settings.GetInt("max_output_tokens") == 28 &&
            job.snapshot.items.array.empty());
  Q4T_CHECK(OutputJson(job.prompt->request).find("private prefix") !=
            std::string::npos);
  Q4T_CHECK(f.Update(Object({{"prompt", Json{}}})) && f.Create() && f.Finish());
  Q4T_CHECK(!f.Snapshot(1).prompt &&
            f.Snapshot(1).settings.GetString("instructions") == "old");
  return true;
}

Q4T_TEST(realtime_session_prompt_per_response_selection_and_null_do_not_leak) {
  PromptFixture f;
  const auto first = f.Register("first"), second = f.Register("second");
  Q4T_CHECK(f.Start() && f.Update(Object({{"prompt", first}})));
  Q4T_CHECK(f.Create(Object({{"prompt", second}})) && f.Finish());
  Q4T_CHECK(f.Snapshot(0).settings.GetString("instructions") == "second" &&
            f.Snapshot(0).response.Find("prompt")->GetString("version") == "1");
  Q4T_CHECK(f.Create(Object({{"prompt", Json{}}})) && f.Finish());
  Q4T_CHECK(!f.Snapshot(1).prompt &&
            f.Snapshot(1).settings.GetString("instructions") == "old");
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(f.Snapshot(2).settings.GetString("instructions") == "first" &&
            f.Snapshot(2).prompt->reference.GetString("id") ==
                first.GetString("id"));
  Q4T_CHECK(f.Count("session.updated") == 1 && f.lookups == 2);
  return true;
}

Q4T_TEST(realtime_session_prompt_tenant_failure_and_revocation_keep_old_state) {
  PromptFixture f;
  const auto first = f.Register("owned");
  f.scope = "client:other";
  const auto other = f.Register("not yours");
  f.scope = "client:session";
  Q4T_CHECK(f.Start() && f.Update(Object({{"prompt", first}})));
  Q4T_CHECK(f.Update(Object(
      {{"prompt", other}, {"instructions", String("must not persist")}})));
  Q4T_CHECK(f.Count("session.updated") == 1 && f.Count("error") == 1);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("event_id") ==
            "event_prompt");
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(f.Snapshot(0).settings.GetString("instructions") == "owned");
  f.authorized = false;
  Q4T_CHECK(f.Create() && f.calls == 1 && f.Count("response.created") == 1);
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "client_authentication_failed");
  return true;
}

Q4T_TEST(realtime_session_prompt_inflight_snapshots_survive_update_and_delete) {
  PromptFixture f;
  const auto first = f.Register("first", "first prefix");
  const auto second = f.Register("second", "second prefix");
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Update(Object({{"prompt", first}})) && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.calls == 1; }));
  Q4T_CHECK(f.Update(Object({{"prompt", second}})));
  Q4T_CHECK(f.prompts->Erase(first.GetString("id"), f.Access()).ok());
  Q4T_CHECK(
      f.Create(Object({{"conversation", String("none")}, {"input", Array()}})));
  Q4T_CHECK(f.Wait([&] { return f.calls == 2; }));
  f.hold = false;
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.Snapshot(0).settings.GetString("instructions") == "first" &&
            f.Snapshot(1).settings.GetString("instructions") == "second");
  Q4T_CHECK(OutputJson(f.Snapshot(0).prompt->request).find("first prefix") !=
            std::string::npos);
  Q4T_CHECK(f.released == 2 && f.lookups == 2);
  return true;
}

namespace {
Json SpeechNumber(double value) {
  Json result;
  result.type = Json::Type::kNumber;
  result.number = value;
  return result;
}
// Deterministic CPU speech source. The runtime TTS model is exercised
// separately.
struct SpeechFixture : Fixture {
  std::atomic<bool> hold_audio{false};
  std::atomic<bool> native_finished{false};
  bool finish_native = false;
  size_t samples = 2400;
  std::atomic<int> fault{
      0};  // 1=missing EOF, 2=bad EOF, 3=final text mismatch.
  SpeechFixture() {
    Set(&settings, "output_modalities", Array({String("audio")}));
    Set(&settings, "audio",
        Object({{"output", Object({{"voice", String("vivian")},
                                   {"speed", Number(1)}})}}));
    cb.execute = [&](const auto& job, const auto& control,
                     const RealtimeDeltaSink& emit,
                     const RealtimeSpeechSink& audio) {
      ++calls;
      {
        std::lock_guard lock(mutex);
        snapshots.push_back(job);
      }
      RealtimeGenerationResult result;
      ResponseDelta text;
      text.text = "spoken";
      result.reply = emit(text);
      if (!result.reply.ok()) return result;
      RealtimeSpeechOptions options;
      ChatPlanError error;
      if (!PlanRealtimeSpeech(job.settings, job.response, &options, &error)) {
        result.reply = object_detail::Error(400, error.code, "CPU options");
        return result;
      }
      result.message = OutputMessage("spoken");
      if (!options.enabled) return result;
      RealtimeSpeechExecution delivery(options, audio,
                                       [&] { return control->Cancelled(); });
      const std::string pcm(samples * 2, '\0');
      result.reply = delivery.Append(pcm);
      ++emitted;
      if (!result.reply.ok()) return result;
      while (hold_audio && !control->Cancelled())
        std::this_thread::sleep_for(1ms);
      if (control->Cancelled()) {
        result.reply =
            object_detail::Error(409, control->Reason(), "CPU cancel");
        return result;
      }
      if (fault != 1) {
        SpeechEncodingReceipt receipt;
        receipt.input_samples = receipt.output_samples = samples;
        receipt.bytes = pcm.size() + (fault == 2 ? 1 : 0);
        receipt.sha256 = q4t::trace::HexDigest(q4t::trace::Sha256(
            {reinterpret_cast<const uint8_t*>(pcm.data()), pcm.size()}));
        result.reply = delivery.Finish("spoken", receipt, {5, 33});
      }
      if (fault == 3) result.message = OutputMessage("invented");
      RealtimeResponseUsage usage;
      usage.total = {9, 35};
      usage.output_audio = 33;
      usage.output_text = 2;
      result.usage = usage;
      if (finish_native) {
        control->Finish();
        native_finished = true;
      }
      return result;
    };
  }
  ~SpeechFixture() {
    if (session) {
      session->Cancel();
      session->Join();
    }
  }
  std::string Item() {
    return Last("conversation.item.added").Find("item")->GetString("id");
  }
  bool Retrieve(const std::string& id) {
    return Send(Object({{"type", String("conversation.item.retrieve")},
                        {"item_id", String(id)}}));
  }
  bool Truncate(const std::string& id, double time, double index = 0) {
    return Send(Object({{"type", String("conversation.item.truncate")},
                        {"item_id", String(id)},
                        {"audio_end_ms", SpeechNumber(time)},
                        {"content_index", SpeechNumber(index)}}));
  }
  bool UpdateOutput(Json output) {
    return Send(Object(
        {{"type", String("session.update")},
         {"session",
          Object({{"type", String("realtime")},
                  {"audio", Object({{"output", std::move(output)}})}})}}));
  }
};
std::string RetrievedAudio(const Fixture& fixture) {
  const auto item = fixture.Last("conversation.item.retrieved");
  const auto& part = item.Find("item")->GetArray("content")->array[0];
  const auto encoded = part.GetString("audio");
  if (encoded.empty()) return {};
  RealtimeTranscriptionCommand command;
  const auto input =
      OutputJson(Object({{"type", String("input_audio_buffer.append")},
                         {"audio", String(encoded)}}));
  if (!ParseRealtimeTranscriptionCommand(input, kWebSocketMessageLimit,
                                         &command)
           .ok())
    throw std::runtime_error("CPU audio retrieval");
  return command.audio;
}
}  // namespace

Q4T_TEST(realtime_session_audio_events_retrieve_delete_and_text_followup) {
  SpeechFixture f;
  Q4T_CHECK(f.Start() && f.Create() && f.Finish());
  Q4T_CHECK(f.Count("response.output_audio.delta") > 0 &&
            f.Count("response.output_audio.done") == 1 &&
            f.Count("response.output_audio_transcript.done") == 1);
  const auto done = *f.Last("response.done").Find("response");
  Q4T_CHECK(done.GetString("status") == "completed" &&
            done.Find("usage")->GetInt("output_tokens") == 35 &&
            done.Find("usage")
                    ->Find("output_token_details")
                    ->GetInt("audio_tokens") == 33 &&
            OutputJson(done).find("delta") == std::string::npos);
  const auto id = f.Item();
  Q4T_CHECK(f.Retrieve(id) && RetrievedAudio(f) == std::string(4800, '\0'));
  Q4T_CHECK(f.session->SpeechBytes() < kRealtimeAudioBytes);
  Q4T_CHECK(
      f.Create(Object({{"output_modalities", Array({String("text")})}})) &&
      f.Finish());
  const auto saved = f.Snapshot(1);
  ChatPlanConfig config;
  config.model = "q4t-test";
  RealtimeChatPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanRealtimeChat(saved.settings, saved.response,
                             saved.snapshot.items, config, &plan, &error));
  Q4T_CHECK(OutputJson(plan.generation.generation_request).find("spoken") !=
            std::string::npos);
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String(id)}})));
  Q4T_CHECK(f.session->SpeechBytes() == 0 && f.released == 2);
  return true;
}

Q4T_TEST(realtime_session_g711_retrieves_actual_bytes_and_playback_clock) {
  for (const auto& [format, code] : std::vector<std::pair<std::string, char>>{
           {"audio/pcmu", char(255)}, {"audio/pcma", char(213)}}) {
    SpeechFixture f;
    Q4T_CHECK(f.Start());
    Q4T_CHECK(f.UpdateOutput(
        Object({{"format", Object({{"type", String(format)}})}})));
    Q4T_CHECK(f.Create() && f.Finish());
    const auto id = f.Item();
    Q4T_CHECK(f.Retrieve(id) && RetrievedAudio(f) == std::string(800, code));
    Q4T_CHECK(f.Truncate(id, 37) && f.Retrieve(id));
    Q4T_CHECK(RetrievedAudio(f) == std::string(296, code));
    const auto part = f.Last("conversation.item.retrieved")
                          .Find("item")
                          ->GetArray("content")
                          ->array[0];
    Q4T_CHECK(part.GetString("transcript").empty());
    Q4T_CHECK(
        f.Create(Object({{"output_modalities", Array({String("text")})}})) &&
        f.Finish());
    ChatPlanConfig config;
    config.model = "q4t-test";
    RealtimeChatPlan plan;
    ChatPlanError error;
    const auto next = f.Snapshot(1);
    Q4T_CHECK(PlanRealtimeChat(next.settings, next.response,
                               next.snapshot.items, config, &plan, &error));
    Q4T_CHECK(OutputJson(plan.generation.generation_request).find("spoken") ==
              std::string::npos);
  }
  return true;
}

Q4T_TEST(realtime_session_active_audio_truncate_cancels_without_late_revival) {
  SpeechFixture f;
  f.hold_audio = true;
  Q4T_CHECK(f.Start() && f.Create());
  Q4T_CHECK(
      f.Wait([&] { return f.Count("response.output_audio.delta") == 3; }));
  const auto id = f.Item();
  Q4T_CHECK(f.Truncate(id, 37, 0.5));
  Q4T_CHECK(f.Count("conversation.item.truncated") == 0);
  Q4T_CHECK(f.Truncate(id, 101));
  Q4T_CHECK(f.Count("conversation.item.truncated") == 0);
  Q4T_CHECK(f.Retrieve(id) && RetrievedAudio(f).size() == 4800);
  Q4T_CHECK(f.Truncate(id, 37) && f.Finish());
  Q4T_CHECK(f.Count("conversation.item.truncated") == 1 && f.released == 1 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "cancelled");
  Q4T_CHECK(f.Retrieve(id) && RetrievedAudio(f).size() == 1776);
  Q4T_CHECK(f.Last("conversation.item.retrieved")
                .Find("item")
                ->GetArray("content")
                ->array[0]
                .GetString("transcript")
                .empty());
  f.hold_audio = false;
  Q4T_CHECK(f.Create() && f.Finish() && f.released == 2);
  return true;
}

Q4T_TEST(realtime_session_audio_delete_cancels_owner_and_releases_waveform) {
  SpeechFixture f;
  f.hold_audio = true;
  Q4T_CHECK(f.Start() && f.Create() &&
            f.Wait([&] { return f.Count("response.output_audio.delta") > 0; }));
  const auto id = f.Item();
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String(id)}})) &&
            f.Finish());
  Q4T_CHECK(f.session->SpeechBytes() == 0 && f.released == 1);
  Q4T_CHECK(f.Retrieve(id) && f.Last("error").Find("error")->GetString(
                                  "code") == "realtime_item_not_found");
  Q4T_CHECK(f.Add(User(id, "recreated without old audio")) && f.Retrieve(id));
  Q4T_CHECK(f.Last("conversation.item.retrieved")
                .Find("item")
                ->GetArray("content")
                ->array[0]
                .GetString("text") == "recreated without old audio");
  return true;
}

Q4T_TEST(realtime_session_audio_voice_and_speed_changes_respect_active_owner) {
  SpeechFixture f;
  f.hold_audio = true;
  Q4T_CHECK(f.Start() && f.Create() &&
            f.Wait([&] { return f.Count("response.output_audio.delta") > 0; }));
  Q4T_CHECK(f.UpdateOutput(Object({{"speed", SpeechNumber(0.5)}})));
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "realtime_speed_locked");
  Q4T_CHECK(f.UpdateOutput(Object({{"voice", String("serena")}})));
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "realtime_voice_locked");
  Q4T_CHECK(f.Create(
      Object({{"conversation", String("none")},
              {"audio",
               Object({{"output", Object({{"voice", String("serena")}})}})}})));
  Q4T_CHECK(f.session->ActiveResponses() == 1 && f.calls == 1);
  f.hold_audio = false;
  Q4T_CHECK(f.Finish());
  Q4T_CHECK(f.UpdateOutput(Object({{"voice", String("serena")}})));
  Q4T_CHECK(f.Last("error").Find("error")->GetString("code") ==
            "realtime_voice_locked");
  Q4T_CHECK(f.UpdateOutput(Object({{"speed", SpeechNumber(0.5)}})));
  Q4T_CHECK(f.Last("session.updated")
                .Find("session")
                ->Find("audio")
                ->Find("output")
                ->Find("speed")
                ->number == 0.5);
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(f.Snapshot(1)
                .settings.Find("audio")
                ->Find("output")
                ->Find("speed")
                ->number == 0.5);
  return true;
}

Q4T_TEST(realtime_session_audio_missing_or_invalid_eof_never_claims_success) {
  for (int fault : {1, 2, 3}) {
    SpeechFixture f;
    f.fault = fault;
    Q4T_CHECK(f.Start() && f.Create() && f.Finish());
    Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
              "failed");
    Q4T_CHECK(f.Count("response.done") == 1 && f.released == 1);
    Q4T_CHECK(f.Retrieve(f.Item()) && RetrievedAudio(f).size() == 4800);
    f.fault = 0;
    Q4T_CHECK(
        f.Create(Object({{"output_modalities", Array({String("text")})}})) &&
        f.Finish());
    Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
                  "completed" &&
              f.released == 2);
  }
  return true;
}

Q4T_TEST(realtime_session_audio_cancel_after_native_finish_keeps_public_owner) {
  for (bool truncate : {true, false}) {
    SpeechFixture f;
    f.samples = 24000;
    f.finish_native = true;
    Q4T_CHECK(f.Start() && f.Create() &&
              f.Wait([&] { return f.native_finished.load(); }, false));
    // One drain cannot consume all audio; native ownership is already Finished.
    Q4T_CHECK(f.session->Tick() && f.Read());
    Q4T_CHECK(f.session->ActiveResponses() == 1 &&
              f.Count("response.output_audio.delta") > 0);
    const auto id = f.Item();
    if (truncate) {
      Q4T_CHECK(f.Truncate(id, 37));
    } else {
      const auto response =
          f.Last("response.created").Find("response")->GetString("id");
      Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")},
                               {"response_id", String(response)}})));
    }
    Q4T_CHECK(f.Finish());
    const auto done = *f.Last("response.done").Find("response");
    Q4T_CHECK(done.GetString("status") == "cancelled" && f.released == 1 &&
              done.Find("usage")->GetInt("total_tokens") == 44);
    Q4T_CHECK(f.Retrieve(id) &&
              RetrievedAudio(f).size() == (truncate ? 1776 : 48000));
  }
  return true;
}
