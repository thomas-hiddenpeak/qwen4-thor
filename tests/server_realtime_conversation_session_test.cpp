#include "audio_transcript_test_util.h"
#include "q4t/server/realtime_conversation_session.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "../src/server/realtime_media_call.h"
#include "../src/server/realtime_webrtc_request.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/realtime_mcp_history.h"
#include "q4t/test.h"
#include "q4t/io/base64.h"
#include "q4t/server/realtime_transcription_protocol.h"
#include "q4t/server/realtime_trace_api.h"

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
struct InputFixture : Fixture {
  std::atomic<bool> asr_hold{false}, asr_fail{false};
  std::atomic<size_t> asr_calls{0}, asr_released{0};
  std::atomic<int> busy{0}, overlap{0};
  int vad_calls = 0, input_validations = 0;
  bool vad_fail = false, reject_language = false;
  std::vector<std::string> execution_order;
  InputFixture() {
    Set(&settings, "audio",
        Object({{"input", Object({{"turn_detection", Json()},
                                  {"transcription", Json()}})}}));
    RealtimeTranscriptionCallbacks native;
    native.validate = [&](const auto& value) {
      ++input_validations;
      return reject_language && value.request.language == "fr"
                 ? object_detail::Error(400, "fixture_input_rejected",
                                        "Native input rejected")
                 : ObjectReply{};
    };
    native.acquire = [&](const auto& id) {
      return requests.Register(id, "", Clock::now() + 5s);
    };
    native.release = [&](const auto& control) {
      requests.Remove(control);
      ++asr_released;
    };
    native.vad_step = [&](auto, auto*, float* probability) {
      ++vad_calls;
      *probability = vad_calls <= 3 ? .9f : 0.f;
      return vad_fail ? q4t::Status::Fail("Injected native VAD failure")
                      : q4t::Status{};
    };
    native.execute = [&](const auto& job, const auto&, const auto& control,
                         const auto&) {
      ++asr_calls;
      if (busy.fetch_add(1)) ++overlap;
      ScopeExit leave([&] { --busy; });
      {
        std::lock_guard lock(mutex);
        execution_order.push_back("asr");
      }
      while (asr_hold && !control->Cancelled())
        std::this_thread::sleep_for(1ms);
      if (control->Cancelled())
        return object_detail::Error(409, control->Reason(), "Input cancelled");
      if (asr_fail)
        return object_detail::Error(503, "fixture_asr_failed", "ASR failed");
      if (!job.processed_audio || job.processed_audio->Samples().empty())
        return object_detail::Error(500, "fixture_audio_missing",
                                    "Actual input samples missing");
      ObjectReply reply;
      reply.value = Object({{"text", String("native fixture transcript")},
                            {"usage", Object({{"type", String("tokens")},
                                              {"input_tokens", Number(23)},
                                              {"output_tokens", Number(7)},
                                              {"total_tokens", Number(30)}})}});
      return reply;
    };
    cb.input = std::move(native);
    const auto generate = cb.execute;
    cb.execute = [&, generate](const auto& job, const auto& control,
                               const auto& emit, const auto& speech) {
      if (busy.fetch_add(1)) ++overlap;
      ScopeExit leave([&] { --busy; });
      {
        std::lock_guard lock(mutex);
        execution_order.push_back("response");
      }
      return generate(job, control, emit, speech);
    };
  }
  ~InputFixture() { session.reset(); }
  bool Append(size_t samples = 2) {
    std::string encoded;
    return io::EncodeBase64(std::string(samples * 2, '\0'),
                            kWebSocketMessageLimit, &encoded) &&
           Send(Object({{"type", String("input_audio_buffer.append")},
                        {"audio", String(std::move(encoded))}}));
  }
  bool Commit() {
    return Send(Object({{"type", String("input_audio_buffer.commit")}}));
  }
  bool Vad(bool respond = true, bool interrupt = true) {
    Json threshold;
    if (!io::ParseJson("0.5", &threshold)) return false;
    return Send(Object(
        {{"type", String("session.update")},
         {"session",
          Object(
              {{"type", String("realtime")},
               {"audio",
                Object({{"input",
                         Object({{"turn_detection",
                                  Object({{"type", String("server_vad")},
                                          {"threshold", threshold},
                                          {"prefix_padding_ms", Number(16)},
                                          {"silence_duration_ms", Number(32)},
                                          {"create_response", Boolean(respond)},
                                          {"interrupt_response",
                                           Boolean(interrupt)}})}})}})}})}}));
  }
  bool Instructions(std::string value) {
    return Send(
        Object({{"type", String("session.update")},
                {"session", Object({{"type", String("realtime")},
                                    {"instructions", String(value)}})}}));
  }
  bool AllFinished() {
    return Wait([&] {
      return session->ActiveResponses() == 0 && session->PendingInputs() == 0;
    });
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

Q4T_TEST(realtime_session_public_identity_is_stable_and_not_client_settings) {
  Fixture f;
  Q4T_CHECK(f.Start());
  const auto created = *f.Last("session.created").Find("session");
  Q4T_CHECK(created.GetString("id") == "sess_fixture" &&
            created.GetString("object") == "realtime.session");
  auto settings = Object({{"type", String("realtime")},
                          {"instructions", String("updated")}});
  Q4T_CHECK(f.Send(Object({{"type", String("session.update")},
                           {"session", settings}})));
  const auto updated = *f.Last("session.updated").Find("session");
  Q4T_CHECK(updated.GetString("id") == created.GetString("id") &&
            updated.GetString("object") == created.GetString("object") &&
            updated.GetString("instructions") == "updated");
  for (const auto* field : {"id", "object"}) {
    auto forged = settings;
    Set(&forged, field, *created.Find(field));
    Q4T_CHECK(f.Send(Object({{"type", String("session.update")},
                             {"session", forged}})));
  }
  Q4T_CHECK(f.Count("session.updated") == 1 && f.Count("error") == 2);
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(!f.Snapshot(0).settings.Has("id") &&
            !f.Snapshot(0).settings.Has("object"));
  return true;
}

Q4T_TEST(realtime_wire_session_identity_rejects_malformed_or_duplicated_fields) {
  for (const auto* session_type : {"realtime", "transcription"}) {
    auto session = Object(
        {{"type", String(session_type)}, {"id", String("sess_fixture")},
         {"object", String(std::string("realtime") == session_type
                                ? "realtime.session"
                                : "realtime.transcription_session")}});
    for (const auto* event_type : {"session.created", "session.updated"}) {
      auto event = Object({{"type", String(event_type)},
                           {"event_id", String("event_fixture")},
                           {"session", session}});
      const auto original = OutputJson(event);
      ChatPlanError error;
      Q4T_CHECK(ValidateRealtimeWireShape(
          event, RealtimeShape::kServerEvent, &error));
      Q4T_CHECK(OutputJson(event) == original);
      for (int mutation = 0; mutation < 8; ++mutation) {
        auto bad = event;
        auto& settings = Field(bad, "session");
        if (mutation < 2) {
          const auto key = mutation ? "object" : "id";
          std::erase_if(settings.object,
                        [&](const auto& e) { return e.first == key; });
        } else if (mutation == 2) {
          Set(&settings, "id", Number(1));
        } else if (mutation == 3) {
          Set(&settings, "id", String("sess_../foreign"));
        } else if (mutation == 4) {
          Set(&settings, "object", String("other.object"));
        } else if (mutation == 5) {
          settings.object.push_back({"id", String("sess_other")});
        } else if (mutation == 6) {
          settings.object.push_back({"object", *session.Find("object")});
        } else {
          Set(&settings, "unknown", Boolean(true));
        }
        const auto before = OutputJson(bad);
        Q4T_CHECK(!ValidateRealtimeWireShape(
            bad, RealtimeShape::kServerEvent, &error));
        Q4T_CHECK(OutputJson(bad) == before);
      }
    }
  }
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
  InputFixture f;
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
    if (shape == RealtimeShape::kServerEvent) {
      Set(&Field(event, "session"), "id", String("sess_fixture"));
      Set(&Field(event, "session"), "object", String("realtime.session"));
    }
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

Q4T_TEST(realtime_session_native_input_wait_freezes_manual_request) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Add(User("prior", "original")) && f.Append() &&
            f.Commit());
  Q4T_CHECK(f.Count("response.created") == 0 && f.Create());
  Q4T_CHECK(f.session->ActiveResponses() == 1 && f.calls == 0);
  const auto id = f.Last("response.created").Find("response")->GetString("id");
  Q4T_CHECK(f.Wait([&] { return f.asr_calls == 1; }));
  Q4T_CHECK(f.Instructions("future setting") && f.Add(User("later", "later")));
  f.asr_hold = false;
  Q4T_CHECK(f.AllFinished() && f.calls == 1 && f.asr_calls == 1 &&
            f.released == 1 && f.asr_released == 1 && f.overlap == 0);
  const auto job = f.Snapshot(0);
  Q4T_CHECK(job.response_id == id &&
            job.settings.GetString("instructions") == "old");
  Q4T_CHECK(job.snapshot.items.array.size() == 2 &&
            job.snapshot.pending_inputs.empty());
  Q4T_CHECK(
      NativeTranscript(
          job.snapshot.items.array[1].GetArray("content")->array[0].GetString(
              "text")) == "native fixture transcript");
  Q4T_CHECK(f.Count("session.created") == 1 &&
            f.Count("conversation.created") == 1 &&
            f.Count("response.created") == 1 && f.Count("response.done") == 1);
  Q4T_CHECK(f.Count("conversation.item.input_audio_transcription.completed") ==
            0);
  return true;
}

Q4T_TEST(realtime_session_native_input_wait_cancel_does_not_execute) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Append() && f.Commit() && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.asr_calls == 1; }));
  Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")}})) &&
            f.Finish());
  Q4T_CHECK(f.calls == 0 && f.released == 1 && f.asr_released == 0 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "cancelled");
  f.asr_hold = false;
  Q4T_CHECK(f.AllFinished() && f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1 && f.released == 2 && f.asr_released == 1);
  return true;
}

Q4T_TEST(realtime_session_native_input_wait_uses_original_deadline) {
  InputFixture f;
  f.asr_hold = true;
  int acquired = 0;
  f.cb.acquire = [&](const auto& id) {
    ++acquired;
    return f.requests.Register(id, "", Clock::now() + 15ms);
  };
  Q4T_CHECK(f.Start() && f.Append() && f.Commit() && f.Create() && f.Finish());
  const auto done = *f.Last("response.done").Find("response");
  Q4T_CHECK(done.GetString("status") == "failed" && f.calls == 0 &&
            acquired == 1 && f.released == 1);
  Q4T_CHECK(done.Find("status_details")->Find("error")->GetString("code") ==
            "deadline_exceeded");
  f.asr_hold = false;
  Q4T_CHECK(f.AllFinished() && f.calls == 0);
  return true;
}

Q4T_TEST(realtime_session_native_input_delete_retains_worker_borrow) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Append(2400) && f.Commit() && f.Create());
  const auto id = f.Last("input_audio_buffer.committed").GetString("item_id");
  Q4T_CHECK(f.Wait([&] { return f.asr_calls == 1; }));
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.retrieve")},
                           {"item_id", String(id)}})));
  Q4T_CHECK(f.Last("conversation.item.retrieved")
                .Find("item")
                ->GetArray("content")
                ->array[0]
                .Has("audio"));
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String(id)}})));
  Q4T_CHECK(f.asr_released == 0 && f.session->InputAudioBytes() > 0 &&
            f.Count("conversation.item.deleted") == 1);
  Q4T_CHECK(f.AllFinished() && f.asr_released == 1 &&
            f.session->InputAudioBytes() == 0 && f.calls == 0);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "failed");
  Q4T_CHECK(f.Add(User(id, "replacement")) && f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1 && f.Snapshot(0).snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_session_native_input_failure_never_uses_stale_turn) {
  InputFixture f;
  f.asr_fail = true;
  Q4T_CHECK(f.Start() && f.Add(User("old", "old text")) && f.Append() &&
            f.Commit() && f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 0 && f.released == 1 && f.Count("error") == 1);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "failed");
  Q4T_CHECK(f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1 && f.Snapshot(0).snapshot.items.array.size() == 1 &&
            f.Snapshot(0).snapshot.items.array[0].GetString("id") == "old");
  return true;
}

Q4T_TEST(realtime_session_native_input_admission_handoff_precedes_next_asr) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Append() && f.Commit() && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.asr_calls == 1; }));
  Q4T_CHECK(f.Append() && f.Commit());
  f.asr_hold = false;
  Q4T_CHECK(f.AllFinished() && f.asr_calls == 2 && f.calls == 1 &&
            f.overlap == 0 && f.asr_released == 2);
  Q4T_CHECK((f.execution_order ==
             std::vector<std::string>{"asr", "response", "asr"}));
  Q4T_CHECK(f.Snapshot(0).snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_session_native_vad_auto_context_and_settings_are_frozen) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Add(User("before", "before")) && f.Vad() &&
            f.Append(6 * 768 + 48));
  Q4T_CHECK(f.Wait([&] { return f.Count("response.created") == 1; }));
  Q4T_CHECK(f.Count("input_audio_buffer.speech_started") == 1 &&
            f.Count("input_audio_buffer.speech_stopped") == 1 && f.calls == 0);
  Q4T_CHECK(f.Instructions("future") && f.Add(User("future", "future")));
  f.asr_hold = false;
  Q4T_CHECK(f.AllFinished() && f.calls == 1 && f.asr_calls == 1 &&
            f.overlap == 0);
  const auto job = f.Snapshot(0);
  Q4T_CHECK(job.settings.GetString("instructions") == "old" &&
            job.snapshot.items.array.size() == 2 &&
            job.snapshot.items.array[0].GetString("id") == "before");
  return true;
}

Q4T_TEST(realtime_session_native_vad_interrupt_preserves_out_of_band) {
  InputFixture f;
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Vad() && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.calls == 1; }));
  const auto old = f.Last("response.created").Find("response")->GetString("id");
  Q4T_CHECK(
      f.Create(Object({{"conversation", String("none")}, {"input", Array()}})));
  const auto oob = f.Last("response.created").Find("response")->GetString("id");
  Q4T_CHECK(f.Append(6 * 768 + 48));
  Q4T_CHECK(f.Wait([&] { return f.calls == 2; }));
  const auto cancelled = *f.Last("response.done").Find("response");
  Q4T_CHECK(cancelled.GetString("id") == old &&
            cancelled.GetString("status") == "cancelled" && f.asr_calls == 0);
  f.hold = false;
  Q4T_CHECK(f.AllFinished() && f.calls == 3 && f.asr_calls == 1 &&
            f.overlap == 0);
  bool preserved = false;
  for (const auto& event : f.events)
    if (event.GetString("type") == "response.done" &&
        event.Find("response")->GetString("id") == oob)
      preserved = event.Find("response")->GetString("status") == "completed";
  Q4T_CHECK(preserved && f.released == 3);
  return true;
}

Q4T_TEST(realtime_session_native_vad_controls_do_not_invent_response) {
  InputFixture f;
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Vad(false, false) && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.calls == 1; }));
  Q4T_CHECK(f.Append(6 * 768 + 48));
  Q4T_CHECK(
      f.Wait([&] { return f.Count("input_audio_buffer.committed") == 1; }));
  Q4T_CHECK(f.Count("response.created") == 1 && f.Count("response.done") == 0 &&
            f.asr_calls == 0);
  f.hold = false;
  Q4T_CHECK(f.AllFinished() && f.calls == 1 && f.asr_calls == 1 &&
            f.overlap == 0);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "completed");
  return true;
}

Q4T_TEST(realtime_session_native_input_settings_rejection_is_atomic) {
  InputFixture f;
  f.reject_language = true;
  Q4T_CHECK(f.Start());
  const auto prior_validations = f.input_validations;
  const auto update = Object(
      {{"type", String("session.update")},
       {"session",
        Object(
            {{"type", String("realtime")},
             {"instructions", String("rejected")},
             {"audio",
              Object(
                  {{"input",
                    Object({{"transcription",
                             Object({{"model",
                                      String(q4t::audio::AsrConfig::kModelId)},
                                     {"language", String("fr")}})}})}})}})}});
  Q4T_CHECK(f.Send(update) && f.Count("session.updated") == 0 &&
            f.Count("error") == 1 &&
            f.input_validations == prior_validations + 1);
  Q4T_CHECK(f.Append() && f.Commit() && f.Create() && f.AllFinished());
  Q4T_CHECK(f.Snapshot(0).settings.GetString("instructions") == "old" &&
            f.Count("conversation.item.input_audio_transcription.completed") ==
                0);
  return true;
}

Q4T_TEST(realtime_session_native_input_fatal_vad_keeps_text_and_updates) {
  InputFixture f;
  f.vad_fail = true;
  Q4T_CHECK(f.Start() && f.Vad() && f.Append(768 + 48));
  Q4T_CHECK(f.Wait([&] { return f.Count("error") > 0; }));
  Q4T_CHECK(f.vad_calls == 1 && f.asr_calls == 0 && f.calls == 0);
  Q4T_CHECK(f.Instructions("healthy text") && f.Add(User("text", "hello")) &&
            f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1 &&
            f.Snapshot(0).settings.GetString("instructions") == "healthy text");
  Q4T_CHECK(f.Count("session.updated") == 2 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "completed");
  return true;
}

Q4T_TEST(realtime_session_native_input_fault_reaps_active_borrow_for_text) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Vad() && f.Append(6 * 768 + 48));
  Q4T_CHECK(f.Wait([&] { return f.asr_calls == 1; }));
  f.vad_fail = true;
  Q4T_CHECK(f.Append(768));
  Q4T_CHECK(f.Wait([&] { return f.Count("error") > 0; }));
  Q4T_CHECK(f.AllFinished() && f.calls == 0 && f.asr_released == 1);
  Q4T_CHECK(f.Add(User("healthy", "text")) && f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1 && f.overlap == 0 &&
            f.Snapshot(0).snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_session_missing_input_backend_keeps_text_contract) {
  Fixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.Send(Object(
      {{"type", String("session.update")},
       {"session", Object({{"type", String("realtime")},
                           {"audio", Object({{"input", Object()}})}})}})));
  Q4T_CHECK(f.Count("session.updated") == 0 && f.Count("error") == 1);
  Q4T_CHECK(f.Create() && f.Finish() && f.calls == 1);
  return true;
}

Q4T_TEST(
    realtime_session_native_input_resolved_unknown_prepare_never_executes) {
  InputFixture f;
  size_t prepared = 0;
  f.cb.prepare = [&](const auto& job) {
    ++prepared;
    ObjectReply reply;
    reply.commit_unknown = !job.snapshot.items.array.empty();
    return reply;
  };
  Q4T_CHECK(f.Start() && f.Append() && f.Commit() && f.Create() &&
            f.AllFinished());
  Q4T_CHECK(prepared == 2 && f.calls == 0 && f.asr_calls == 1 &&
            f.released == 1);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "failed");
  return true;
}

Q4T_TEST(realtime_session_native_waiting_responses_remain_capacity_bounded) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Append() && f.Commit() && f.Create());
  const auto input =
      f.Last("input_audio_buffer.committed").GetString("item_id");
  const auto isolated =
      Object({{"conversation", String("none")},
              {"input", Array({Object({{"type", String("item_reference")},
                                       {"id", String(input)}})})}});
  for (size_t i = 0; i < 4; ++i) Q4T_CHECK(f.Create(isolated));
  Q4T_CHECK(f.session->ActiveResponses() == 4 &&
            f.Count("response.created") == 4 && f.Count("error") == 1 &&
            f.calls == 0);
  std::vector<std::string> ids;
  for (const auto& event : f.events)
    if (event.GetString("type") == "response.created")
      ids.push_back(event.Find("response")->GetString("id"));
  for (const auto& id : ids)
    Q4T_CHECK(f.Send(Object(
        {{"type", String("response.cancel")}, {"response_id", String(id)}})));
  Q4T_CHECK(f.Finish() && f.released == 4 && f.calls == 0);
  f.asr_hold = false;
  Q4T_CHECK(f.AllFinished() && f.asr_released == 1);
  return true;
}

Q4T_TEST(realtime_wire_response_references_use_exact_separate_sdk_shape) {
  const auto response =
      Object({{"input", Array({Object({{"type", String("item_reference")},
                                       {"id", String("owned")}})})}});
  const auto event =
      Object({{"type", String("response.create")}, {"response", response}});
  const auto original = OutputJson(event);
  ChatPlanError error;
  Q4T_CHECK(
      ValidateRealtimeShape(response, RealtimeShape::kResponseCreate, &error));
  Q4T_CHECK(!ValidateRealtimeShape(event, RealtimeShape::kClientEvent, &error));
  Q4T_CHECK(
      ValidateRealtimeWireShape(event, RealtimeShape::kClientEvent, &error));
  Q4T_CHECK(OutputJson(event) == original);
  Fixture f;
  Q4T_CHECK(f.Start() && f.Add(User("owned", "reference content")) &&
            f.Create(response) && f.Finish());
  Q4T_CHECK(f.calls == 1 && f.Snapshot(0).snapshot.items.array.size() == 1);
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String("owned")}})));
  Q4T_CHECK(f.Create(response) && f.Count("response.created") == 1 &&
            f.Count("error") == 1 && f.calls == 1);
  // Referencing cannot create an input item or turn client transcript text
  // into an owned native ASR result.
  Q4T_CHECK(f.Add(response.Find("input")->array[0]) && f.Count("error") == 2);
  return true;
}

Q4T_TEST(realtime_wire_reference_projection_preserves_full_strict_envelope) {
  Json event;
  Q4T_CHECK(io::ParseJson(
      R"({"type":"response.create","response":{"input":[
        {"type":"item_reference","id":"owned"}]}})",
      &event));
  ChatPlanError error;
  for (size_t variation = 0; variation < 8; ++variation) {
    auto invalid = event;
    auto& response = Field(invalid, "response");
    auto& reference = Field(response, "input").array[0];
    switch (variation) {
      case 0:
        invalid.object.push_back(invalid.object.back());
        break;
      case 1:
        Set(&invalid, "unknown", Boolean(true));
        break;
      case 2:
        response.object.push_back(response.object.back());
        break;
      case 3:
        Set(&response, "unknown", Boolean(true));
        break;
      case 4:
        reference.object.push_back(reference.object.back());
        break;
      case 5:
        Set(&reference, "transcript", String("forged"));
        break;
      case 6:
        Field(reference, "id") = Number(7);
        break;
      case 7:
        Set(&invalid, "event_id", Number(7));
        break;
    }
    Q4T_CHECK(!ValidateRealtimeWireShape(invalid, RealtimeShape::kClientEvent,
                                         &error));
  }
  return true;
}

Q4T_TEST(realtime_session_native_input_reaches_actual_chat_planner) {
  for (bool automatic : {false, true}) {
    InputFixture f;
    ChatPlanConfig config;
    config.model = "q4t-test";
    config.max_len = 8192;
    config.structured = true;
    size_t prepared = 0, resolved = 0;
    bool private_owner = true;
    const auto prepare = [&](const Json& settings, const Json& response,
                             const Json& input, bool connected) {
      RealtimeChatPlan plan;
      ChatPlanError error;
      const auto status = PlanRealtimeChat(settings, response, input, config,
                                           &plan, &error, nullptr, connected);
      return status ? ObjectReply{}
                    : object_detail::Error(error.http_status, error.code,
                                           status.message());
    };
    f.cb.validate = [&](const Json& settings) {
      return prepare(settings, Object(), Array(), true);
    };
    f.cb.prepare = [&](const RealtimeConversationJob& job) {
      ++prepared;
      private_owner &= job.native_input;
      resolved += !job.snapshot.items.array.empty();
      return prepare(job.settings, job.response, job.snapshot.items,
                     job.native_input);
    };
    Q4T_CHECK(f.Start());
    if (automatic) {
      Q4T_CHECK(f.Vad() && f.Append(6 * 768 + 48));
      Q4T_CHECK(f.Wait([&] { return f.Count("response.created") == 1; }));
    } else {
      Q4T_CHECK(f.Append() && f.Commit() && f.Create());
    }
    Q4T_CHECK(f.AllFinished() && f.Count("error") == 0 && f.calls == 1 &&
              f.asr_calls == 1 && prepared == 2 && resolved == 1 &&
              private_owner && f.overlap == 0);
  }
  return true;
}

Q4T_TEST(
    realtime_session_recovers_new_audio_after_borrowed_fault_without_replay) {
  InputFixture f;
  auto now = Clock::now();
  f.cb.now = [&] { return now; };
  Q4T_CHECK(f.Start() && f.Append(24) && f.Commit() && f.AllFinished());
  Q4T_CHECK(f.asr_calls == 1 && f.calls == 0);
  f.asr_hold = true;
  Q4T_CHECK(f.Vad() && f.Append(6 * 768 + 48));
  Q4T_CHECK(f.Wait([&] { return f.asr_calls == 2; }));
  f.vad_fail = true;
  Q4T_CHECK(f.Append(768) && f.Wait([&] { return f.Count("error") > 0; }) &&
            f.AllFinished());
  Q4T_CHECK(f.calls == 0 && f.asr_released == 2 &&
            f.Count("response.done") == 1);
  Q4T_CHECK(f.Add(User("healthy", "text")) && f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1);
  f.vad_fail = false;
  f.asr_hold = false;
  now += 5s;
  Q4T_CHECK(f.session->Tick() && f.Read() && f.asr_calls == 2 && f.calls == 1);
  Q4T_CHECK(f.Send(Object(
      {{"type", String("session.update")},
       {"session",
        Object({{"type", String("realtime")},
                {"audio", Object({{"input", Object({{"turn_detection",
                                                     Json()}})}})}})}})));
  const auto errors = f.Count("error");
  Q4T_CHECK(f.Append(24) && f.Commit() && f.Create() && f.AllFinished());
  Q4T_CHECK(f.Count("error") == errors && f.asr_calls == 3 && f.calls == 2 &&
            f.asr_released == 3 && f.released == 3 && f.overlap == 0);
  Q4T_CHECK(f.Count("session.created") == 1 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "completed");
  return true;
}

namespace {
void EnableIdle(Fixture& f) {
  if (!f.settings.Has("audio")) Set(&f.settings, "audio", Object());
  auto& audio = Field(f.settings, "audio");
  if (!audio.Has("input")) Set(&audio, "input", Object());
  Set(&Field(audio, "input"), "turn_detection",
      Object({{"type", String("server_vad")},
              {"threshold", SpeechNumber(.5)},
              {"idle_timeout_ms", Number(5000)}}));
}
}  // namespace

Q4T_TEST(realtime_session_idle_empty_turn_reaches_actual_chat_planner) {
  InputFixture f;
  auto now = Clock::now();
  f.cb.now = [&] { return now; };
  EnableIdle(f);
  const std::string instructions =
      "If no speech was detected during the idle interval, reply exactly "
      "READY.";
  Set(&f.settings, "instructions", String(instructions));
  ChatPlanConfig config;
  config.model = "q4t-test";
  f.cb.prepare = [&](const RealtimeConversationJob& job) {
    RealtimeChatPlan plan;
    ChatPlanError error;
    const auto status = PlanRealtimeChat(
        job.settings, job.response, job.snapshot.items, config, &plan, &error,
        job.prompt ? &*job.prompt : nullptr, job.native_input);
    if (status) {
      const auto* messages =
          plan.generation.generation_request.GetArray("messages");
      if (!messages ||
          messages->array.front().GetString("content") != instructions ||
          job.settings.GetString("instructions") != instructions)
        return object_detail::Error(500, "instruction_changed",
                                    "Native input changed application policy");
    }
    return status ? ObjectReply{}
                  : object_detail::Error(400, error.code, status.message());
  };
  Q4T_CHECK(f.Start() && f.Create() && f.AllFinished());
  now += 5s;
  Q4T_CHECK(f.session->Tick() && f.Read() && f.AllFinished());
  Q4T_CHECK(f.calls == 2 && f.asr_calls == 0 && f.asr_released == 0 &&
            f.released == 2 && f.Count("error") == 0 &&
            f.Count("input_audio_buffer.timeout_triggered") == 1);
  const auto snapshot = f.Snapshot(1);
  Q4T_CHECK(
      snapshot.native_input &&
      OutputJson(snapshot.snapshot.items).find("No speech was detected") !=
          std::string::npos);
  Q4T_CHECK(f.Count("session.created") == 1 && f.overlap == 0);
  return true;
}

Q4T_TEST(realtime_session_idle_uses_actual_output_playback_duration) {
  SpeechFixture f;
  auto now = Clock::now();
  f.cb.now = [&] { return now; };
  EnableIdle(f);
  size_t asr_calls = 0;
  RealtimeTranscriptionCallbacks native;
  native.validate = [](const auto&) { return ObjectReply{}; };
  native.acquire = f.cb.acquire;
  native.release = f.cb.release;
  native.vad_step = [](auto, auto*, float* probability) {
    *probability = 0;
    return q4t::Status{};
  };
  native.execute = [&](const auto&, const auto&, const auto&, const auto&) {
    ++asr_calls;
    return object_detail::Error(500, "unexpected_asr", "No samples were sent");
  };
  f.cb.input = std::move(native);
  Q4T_CHECK(f.Start() && f.Create() && f.Finish());
  Q4T_CHECK(f.Count("response.output_audio.done") == 1 && f.calls == 1);
  now += 5099ms;  // Real 2400 / 24000 = 100 ms playback + 5000 ms idle.
  Q4T_CHECK(f.session->Tick() && f.Read() &&
            f.Count("input_audio_buffer.timeout_triggered") == 0);
  now += 1ms;
  Q4T_CHECK(f.session->Tick() && f.Read() && f.Finish());
  Q4T_CHECK(f.calls == 2 && asr_calls == 0 &&
            f.Count("input_audio_buffer.timeout_triggered") == 1 &&
            f.Count("response.output_audio.done") == 2 &&
            f.Count("error") == 0);
  return true;
}

Q4T_TEST(realtime_session_idle_ignores_oob_failed_and_cancelled_output) {
  for (int mode = 0; mode < 4; ++mode) {
    InputFixture f;
    auto now = Clock::now();
    f.cb.now = [&] { return now; };
    EnableIdle(f);
    Q4T_CHECK(f.Start());
    if (mode == 3) {
      Q4T_CHECK(f.Create() && f.AllFinished());
      now += 4s;
    }
    if (mode == 1) f.mode = 2;
    if (mode == 2) f.hold = true;
    Q4T_CHECK(f.Create(
        mode == 0 || mode == 3
            ? Object({{"conversation", String("none")}, {"input", Array()}})
            : Object()));
    if (mode == 2) {
      Q4T_CHECK(f.Wait([&] { return f.calls == 1; }));
      Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")}})));
      f.hold = false;
    }
    Q4T_CHECK(f.AllFinished());
    now += mode == 3 ? 1s : 60s;
    Q4T_CHECK(f.session->Tick() && f.Read() && f.AllFinished());
    Q4T_CHECK(f.Count("input_audio_buffer.timeout_triggered") ==
              (mode == 3 ? 1 : 0));
    Q4T_CHECK(f.calls == (mode == 3 ? 3 : 1) && f.asr_calls == 0);
  }
  return true;
}

namespace {
Json DirectMessage(std::string id = "mixed", bool two = true) {
  auto item = User(std::move(id), "before audio");
  auto& parts = Field(item, "content").array;
  parts.push_back(Object({{"type", String("input_audio")},
                          {"audio", String("AQACAAMA")},
                          {"transcript", String("client reference")}}));
  if (two) {
    parts.push_back(Object(
        {{"type", String("input_text")}, {"text", String("between audio")}}));
    parts.push_back(Object(
        {{"type", String("input_audio")}, {"audio", String("BAAFAAYA")}}));
  }
  return item;
}
Json InlineAudio(std::string conversation = "none") {
  return Object({{"conversation", String(std::move(conversation))},
                 {"input", Array({DirectMessage()})}});
}
bool HasId(const RealtimeConversationJob& job, std::string_view id) {
  return std::ranges::any_of(job.snapshot.items.array, [&](const auto& item) {
    return item.GetString("id") == id;
  });
}
}  // namespace

Q4T_TEST(realtime_direct_session_mixed_item_reaches_actual_chat_planner) {
  InputFixture f;
  ChatPlanConfig config;
  config.model = "q4t-test";
  config.max_len = 8192;
  config.structured = true;
  size_t prepared = 0;
  f.cb.prepare = [&](const auto& job) {
    ++prepared;
    RealtimeChatPlan plan;
    ChatPlanError error;
    auto status =
        PlanRealtimeChat(job.settings, job.response, job.snapshot.items, config,
                         &plan, &error, nullptr, job.native_input);
    return status ? ObjectReply{}
                  : object_detail::Error(error.http_status, error.code,
                                         status.message());
  };
  Q4T_CHECK(f.Start() && f.Add(DirectMessage()));
  Q4T_CHECK(f.Count("conversation.item.added") == 1 &&
            f.Count("conversation.item.done") == 1 &&
            f.Count("input_audio_buffer.committed") == 0 && f.asr_calls == 0);
  const auto added = *f.Last("conversation.item.added").Find("item");
  Q4T_CHECK(!added.GetArray("content")->array[1].Has("audio") &&
            added.GetArray("content")->array[1].GetString("transcript") ==
                "client reference");
  Q4T_CHECK(f.Create() && f.AllFinished() && f.calls == 1 && f.asr_calls == 2 &&
            f.overlap == 0 && prepared == 2 && f.Count("error") == 0);
  const auto job = f.Snapshot(0);
  const auto& parts = job.snapshot.items.array[0].GetArray("content")->array;
  Q4T_CHECK(parts.size() == 4 && parts[0].GetString("text") == "before audio" &&
            NativeTranscript(parts[1].GetString("text")) ==
                "native fixture transcript" &&
            parts[2].GetString("text") == "between audio" &&
            NativeTranscript(parts[3].GetString("text")) ==
                "native fixture transcript");
  return true;
}

Q4T_TEST(realtime_direct_session_empty_reference_keeps_actual_text_only) {
  InputFixture f;
  auto item = DirectMessage("empty", false);
  Field(item, "content").array[1] =
      Object({{"type", String("input_audio")},
              {"transcript", String("do not use reference")}});
  Q4T_CHECK(f.Start() && f.Add(item) && f.Create() && f.AllFinished());
  Q4T_CHECK(f.calls == 1 && f.asr_calls == 0 && f.asr_released == 0 &&
            f.Count("error") == 0 &&
            f.Count("conversation.item.input_audio_transcription.completed") ==
                0);
  const auto job = f.Snapshot(0);
  const auto data = AudioTranscriptData(
      job.snapshot.items.array[0].GetArray("content")->array[1].GetString(
          "text"));
  Q4T_CHECK(data.GetString("transcript", "missing").empty() &&
            data.GetString("source") == "conversation.item[empty].content[1]");
  Q4T_CHECK(OutputJson(job.snapshot.items).find("do not use reference") ==
            std::string::npos);
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.retrieve")},
                           {"item_id", String("empty")}})));
  const auto part = f.Last("conversation.item.retrieved")
                        .Find("item")
                        ->GetArray("content")
                        ->array[1];
  Q4T_CHECK(part.GetString("audio", "missing").empty() &&
            part.GetString("transcript") == "do not use reference");
  return true;
}

Q4T_TEST(realtime_direct_session_inline_cleanup_preserves_default_context) {
  for (const auto* conversation : {"none", "auto"}) {
    InputFixture f;
    Q4T_CHECK(f.Start() && f.Add(User("prior", "default text")));
    auto response = InlineAudio(conversation);
    Field(response, "input")
        .array.push_back(Object(
            {{"type", String("item_reference")}, {"id", String("prior")}}));
    Q4T_CHECK(f.Create(response) && f.AllFinished() && f.calls == 1 &&
              f.asr_calls == 2 && f.session->InputAudioBytes() == 0 &&
              f.Count("error") == 0);
    const auto job = f.Snapshot(0);
    Q4T_CHECK(job.snapshot.items.array.size() == 2 && HasId(job, "mixed") &&
              HasId(job, "prior") && !job.snapshot.default_context &&
              job.response.GetArray("input")->array[0].GetString("type") ==
                  "item_reference");
    Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.retrieve")},
                             {"item_id", String("mixed")}})));
    Q4T_CHECK(f.Count("conversation.item.retrieved") == 0 &&
              f.Count("error") == 1);
    Q4T_CHECK(f.Create() && f.AllFinished() && f.calls == 2 &&
              !HasId(f.Snapshot(1), "mixed") && HasId(f.Snapshot(1), "prior"));
  }
  return true;
}

Q4T_TEST(realtime_direct_session_prepare_failure_releases_unpublished_input) {
  for (bool unknown : {false, true}) {
    InputFixture f;
    f.cb.prepare = [&](const auto&) {
      auto reply = unknown ? ObjectReply{}
                           : object_detail::Error(400, "fixture_reject", "No");
      reply.commit_unknown = unknown;
      return reply;
    };
    Q4T_CHECK(f.Start() && f.Create(InlineAudio()) && f.AllFinished());
    Q4T_CHECK(f.Count("error") == 1 && f.calls == 0 && f.asr_calls == 0 &&
              f.released == 0 && f.session->HistoryBytes() == 0 &&
              f.session->InputAudioBytes() == 0 &&
              f.Count("response.created") == 0 &&
              f.Count("conversation.item.added") == 0);
  }
  return true;
}

Q4T_TEST(realtime_direct_session_inline_cancel_reaps_borrow_without_response) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Create(InlineAudio()) &&
            f.Wait([&] { return f.asr_calls == 1; }));
  const auto id = f.Last("response.created").Find("response")->GetString("id");
  Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")},
                           {"response_id", String(id)}})) &&
            f.AllFinished());
  Q4T_CHECK(f.calls == 0 && f.asr_calls == 1 && f.released == 1 &&
            f.asr_released == 1 && f.session->InputAudioBytes() == 0 &&
            f.session->HistoryBytes() == 0 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "cancelled");
  f.asr_hold = false;
  Q4T_CHECK(f.Create(InlineAudio()) && f.AllFinished() && f.calls == 1 &&
            f.asr_calls == 3 && f.session->InputAudioBytes() == 0);
  return true;
}

Q4T_TEST(
    realtime_direct_session_inline_failed_asr_cleans_without_stale_answer) {
  InputFixture f;
  f.asr_fail = true;
  Q4T_CHECK(f.Start() && f.Add(User("prior", "default text")) &&
            f.Create(InlineAudio()) && f.AllFinished());
  Q4T_CHECK(f.calls == 0 && f.asr_calls >= 1 && f.released == 1 &&
            f.session->InputAudioBytes() == 0 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "failed");
  Q4T_CHECK(f.Create() && f.AllFinished() && f.calls == 1 &&
            f.Snapshot(0).snapshot.items.array.size() == 1 &&
            HasId(f.Snapshot(0), "prior"));
  return true;
}

Q4T_TEST(realtime_direct_session_inline_bad_later_item_is_atomic) {
  InputFixture f;
  Q4T_CHECK(f.Start());
  auto response = InlineAudio();
  auto bad = DirectMessage("bad", false);
  Set(&Field(bad, "content").array[1], "audio", String("!"));
  Field(response, "input").array.push_back(std::move(bad));
  Q4T_CHECK(f.Create(response) && f.AllFinished());
  Q4T_CHECK(f.Count("error") == 1 && f.calls == 0 && f.asr_calls == 0 &&
            f.released == 0 && f.Count("response.created") == 0 &&
            f.Count("conversation.item.added") == 0 &&
            f.session->HistoryBytes() == 0 &&
            f.session->InputAudioBytes() == 0);
  Q4T_CHECK(f.Create(InlineAudio()) && f.AllFinished() && f.calls == 1 &&
            f.asr_calls == 2 && f.session->InputAudioBytes() == 0);
  return true;
}

Q4T_TEST(
    realtime_direct_session_delete_pending_then_reuse_cannot_replace_turn) {
  InputFixture f;
  f.asr_hold = true;
  Q4T_CHECK(f.Start() && f.Add(DirectMessage()) && f.Create() &&
            f.Wait([&] { return f.asr_calls == 1; }));
  Q4T_CHECK(f.Send(Object({{"type", String("conversation.item.delete")},
                           {"item_id", String("mixed")}})) &&
            f.Add(User("mixed", "new generation")) && f.AllFinished());
  Q4T_CHECK(f.calls == 0 && f.asr_calls == 1 &&
            f.session->InputAudioBytes() == 0 &&
            f.Last("response.done").Find("response")->GetString("status") ==
                "failed");
  Q4T_CHECK(f.Create() && f.AllFinished() && f.calls == 1);
  const auto job = f.Snapshot(0);
  Q4T_CHECK(job.snapshot.items.array.size() == 1 &&
            job.snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "new generation");
  return true;
}

Q4T_TEST(realtime_direct_session_absent_backend_rejects_before_history) {
  Fixture f;
  Q4T_CHECK(f.Start() && f.Add(DirectMessage()) && f.Create(InlineAudio()));
  Q4T_CHECK(f.Count("error") == 2 && f.calls == 0 &&
            f.session->HistoryBytes() == 0 &&
            f.Count("conversation.item.added") == 0 &&
            f.Count("response.created") == 0);
  Q4T_CHECK(f.Add(User("text", "healthy text")) && f.Create() && f.Finish() &&
            f.calls == 1 && HasId(f.Snapshot(0), "text"));
  return true;
}

namespace {
struct McpSessionFixture : Fixture {
  ChatPlanConfig config;
  std::atomic<size_t> imports{0}, proposals{0}, dispatches{0}, cleanups{0};
  std::atomic<bool> import_hold{false}, dispatch_hold{false};
  bool approval = false, import_fails = false;
  std::map<std::string, Json> proposals_by_id;
  ~McpSessionFixture() {
    if (session) {
      session->Cancel();
      session->Join();
      session.reset();
    }
  }
  Json Tools(std::string label = "project",
             std::string url = "https://example.com") {
    return Array({Object(
        {{"type", String("mcp")},
         {"server_label", String(label)},
         {"server_url", String(url)},
         {"authorization", String("session-fixture-token")},
         {"headers", Object({{"x-fixture", String("session-private")}})},
         {"require_approval", String(approval ? "always" : "never")}})});
  }
  ObjectReply Plan(const RealtimeConversationJob& job, RealtimeChatPlan* plan,
                   bool resolve = false) {
    Json native;
    auto reply = resolve
                     ? ResolveRealtimeMcpHistory(
                           nullptr, config, job.snapshot,
                           {"client:fixture", Clock::now() + 2s, {}}, &native)
                     : PreviewRealtimeMcpHistory(job.snapshot.items, &native);
    if (!reply.ok()) return reply;
    ChatPlanError error;
    auto status = PlanRealtimeChat(
        job.settings, job.response, job.snapshot.items, config, plan, &error,
        job.prompt ? &*job.prompt : nullptr, false, job.mcp.get(),
        job.mcp_definitions_only, &native);
    return status ? ObjectReply{}
                  : object_detail::Error(error.http_status, error.code,
                                         status.message());
  }
  McpSessionFixture() {
    config.model = "q4t-test";
    config.max_len = 8192;
    config.structured = true;
    settings = DefaultRealtimeChatSettings(config, "");
    cb.validate_prompt = [this](const RealtimePromptSelection& selection) {
      if (selection.settings.GetString("instructions") == "reject")
        return object_detail::Error(400, "fixture_setting_rejected",
                                    "Rejected setting");
      RealtimeConversationJob job;
      job.settings = selection.settings;
      job.response = Object();
      job.snapshot.items = Array();
      job.prompt = selection.prompt;
      job.mcp = selection.mcp;
      job.mcp_definitions_only = true;
      RealtimeChatPlan plan;
      return Plan(job, &plan);
    };
    cb.prepare = [this](const auto& job) {
      RealtimeChatPlan plan;
      return Plan(job, &plan);
    };
    cb.mcp_import.emplace();
    cb.mcp_import->import = [this](const auto& definition, std::string id,
                                   auto deadline, const auto& cancelled,
                                   RealtimeMcpCatalog* catalog) {
      ++imports;
      while (import_hold && !cancelled() && Clock::now() < deadline)
        std::this_thread::sleep_for(1ms);
      if (cancelled())
        return object_detail::Error(499, "cancelled", "Cancelled");
      if (import_fails)
        return object_detail::Error(503, "fixture_import_failed",
                                    "Import failed");
      catalog->definition = definition;
      catalog->item = Object(
          {{"id", String(id)},
           {"type", String("mcp_list_tools")},
           {"server_label", *definition.declaration.Find("server_label")},
           {"tools",
            Array({Object(
                {{"name", String("read")},
                 {"input_schema",
                  Object({{"type", String("object")},
                          {"properties",
                           Object({{"path",
                                    Object({{"type",
                                             String("string")}})}})}})}})})}});
      catalog->authority = catalog->item;
      catalog->loaded = true;
      ObjectReply reply;
      reply.value = catalog->item;
      return reply;
    };
    cb.mcp_call.emplace();
    cb.mcp_call->run = [this](const RealtimeMcpCallJob& job, auto deadline,
                              const auto& cancelled,
                              RealtimeMcpExecution* execution) {
      auto native = job.generated;
      const auto id = native.GetString("id");
      if (job.action == RealtimeMcpAction::kPropose) {
        ++proposals;
        if (approval) {
          Set(&native, "approval_request_id", String("mcpr_" + id));
          proposals_by_id[id] = native;
          Set(&native, "type", String("mcp_approval_request"));
          Set(&native, "id", *native.Find("approval_request_id"));
          std::erase_if(native.object, [](const auto& field) {
            return field.first == "approval_request_id";
          });
          ObjectReply reply;
          reply.value = native;
          return reply;
        }
      }
      if (job.action == RealtimeMcpAction::kAbandon) {
        ++cleanups;
        if (proposals_by_id.contains(id)) native = proposals_by_id.at(id);
      }
      bool pending = job.action == RealtimeMcpAction::kPropose ||
                     (job.action == RealtimeMcpAction::kDecide &&
                      job.decision.GetBool("approve"));
      bool failed = job.action == RealtimeMcpAction::kAbandon ||
                    (job.action == RealtimeMcpAction::kDecide &&
                     !job.decision.GetBool("approve"));
      if (job.action == RealtimeMcpAction::kExecute) {
        ++dispatches;
        while (dispatch_hold && !cancelled() && Clock::now() < deadline)
          std::this_thread::sleep_for(1ms);
        if (cancelled())
          return object_detail::Error(499, "cancelled", "Cancelled");
      }
      if (!native.Has("approval_request_id"))
        Set(&native, "approval_request_id", Json());
      Set(&native, "status",
          String(pending  ? "in_progress"
                 : failed ? "failed"
                          : "completed"));
      Set(&native, "output",
          !pending && !failed ? String("actual fixture document") : Json());
      Set(&native, "error",
          failed ? Object({{"type", String("mcp_protocol_error")},
                           {"code", Number(-32603)},
                           {"message", String("not executed")}})
                 : Json());
      execution->item = native;
      execution->call_permitted = job.action == RealtimeMcpAction::kExecute;
      ObjectReply reply;
      reply.value = native;
      return reply;
    };
    cb.execute = [this](const RealtimeConversationJob& job, const auto& control,
                        const RealtimeDeltaSink& emit,
                        const RealtimeSpeechSink&) {
      ++calls;
      {
        std::lock_guard lock(mutex);
        snapshots.push_back(job);
      }
      entered = true;
      while (hold && !control->Cancelled()) std::this_thread::sleep_for(1ms);
      RealtimeGenerationResult result;
      if (control->Cancelled()) {
        result.reply = object_detail::Error(409, "cancelled", "Cancelled");
        return result;
      }
      RealtimeChatPlan plan;
      result.reply = Plan(job, &plan, true);
      if (!result.reply.ok()) return result;
      ChatResponseParser parser;
      auto status =
          parser.Init(plan.generation.generation_request,
                      "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                      control->id(), true);
      std::vector<ResponseDelta> deltas;
      const auto name = mode == 1 && job.mcp && !job.mcp->bindings.empty()
                            ? job.mcp->bindings[0].model_name
                            : std::string();
      // This CPU model fixture supplies the receipt produced by the actual
      // isolated decoder in production; keep the real parser contract.
      std::vector<SchemaReceipt> receipts;
      if (!name.empty() && plan.generation.chat.constraints.schema_receipts) {
        const auto& functions =
            plan.generation.generation_request.GetArray("tools")->array;
        for (const auto& tool : functions) {
          const auto& function = *tool.Find("function");
          if (function.GetString("name") != name) continue;
          const auto value = Object({{"path", String("docs/STATUS.md")}});
          OutputSchemaGraph schema;
          status = schema.Init(*function.Find("parameters"), false, nullptr,
                               "schema", OutputSchemaPurpose::kToolResult);
          if (status) status = schema.Check(value);
          SchemaReceipt receipt;
          if (status)
            status = MakeSchemaReceipt(name, *function.Find("parameters"),
                                       value, &receipt);
          if (status) receipts.push_back(receipt);
        }
        parser.SetSchemaReceipts(&receipts);
      }
      const std::string arguments =
          plan.generation.chat.constraints.active
              ? "<parameter=arguments>{\"path\":\"docs/STATUS.md\"}</parameter>"
              : "<parameter=path>docs/STATUS.md</parameter>";
      const auto text = name.empty()
                            ? std::string("answer")
                            : "<tool_call><function=" + name + ">" + arguments +
                                  "</function></tool_call>";
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
      return result;
    };
  }
  bool Configure() {
    return Send(Object({{"type", String("session.update")},
                        {"session", Object({{"type", String("realtime")},
                                            {"tools", Tools()}})}}));
  }
  bool Imported() {
    return Wait([&] { return Count("mcp_list_tools.completed") != 0; });
  }
  bool Settled() {
    return Wait([&] {
      return session->ActiveResponses() == 0 && session->PendingMcp() == 0;
    });
  }
  Json Approval() const {
    for (const auto& event : events) {
      const auto* item = event.Find("item");
      if (event.GetString("type") == "conversation.item.done" && item &&
          item->GetString("type") == "mcp_approval_request")
        return *item;
    }
    return {};
  }
};
}  // namespace

Q4T_TEST(realtime_session_mcp_public_events_and_tool_result_next_turn) {
  McpSessionFixture f;
  f.mode = 1;
  Q4T_CHECK(f.Start() && f.Configure() && f.Imported() && f.Create() &&
            f.Settled());
  Q4T_CHECK(f.calls == 1 && f.dispatches == 1 &&
            f.Count("response.mcp_call_arguments.delta") > 0 &&
            f.Count("response.mcp_call.in_progress") == 1);
  Q4T_CHECK(
      f.Last("response.output_item.done").Find("item")->GetString("output") ==
      "actual fixture document");
  f.mode = 0;
  Q4T_CHECK(f.Create() && f.Finish() && f.calls == 2);
  Q4T_CHECK(OutputJson(f.Snapshot(1).snapshot.items)
                .find("actual fixture document") != std::string::npos);
  for (const auto& event : f.events) {
    Q4T_CHECK(OutputJson(event).find("session-fixture-token") ==
              std::string::npos);
    Q4T_CHECK(OutputJson(event).find("session-private") == std::string::npos);
  }
  return true;
}
Q4T_TEST(realtime_session_mcp_approval_after_generation_single_dispatch) {
  McpSessionFixture f;
  f.mode = 1;
  f.approval = true;
  Q4T_CHECK(f.Start() && f.Configure() && f.Imported() && f.Create());
  Q4T_CHECK(f.Wait([&] { return !f.Approval().IsNull(); }));
  Q4T_CHECK(f.Count("response.done") == 1 && f.dispatches == 0);
  const auto approval = f.Approval();
  auto decision = Object({{"type", String("mcp_approval_response")},
                          {"id", String("decision_fixture")},
                          {"approval_request_id", *approval.Find("id")},
                          {"approve", Boolean(true)}});
  Q4T_CHECK(f.Add(decision) && f.Add(decision) && f.Settled());
  Q4T_CHECK(f.dispatches == 1 && f.Count("error") == 1);
  return true;
}
Q4T_TEST(realtime_session_mcp_response_only_import_waits_for_actual_catalog) {
  McpSessionFixture f;
  f.mode = 1;
  f.import_hold = true;
  Q4T_CHECK(f.Start() &&
            f.Create(Object(
                {{"tools", f.Tools()},
                 {"tool_choice", Object({{"type", String("mcp")},
                                         {"server_label", String("project")},
                                         {"name", String("read")}})}})));
  Q4T_CHECK(f.Wait([&] { return f.imports == 1; }) && f.calls == 0);
  f.import_hold = false;
  const bool settled = f.Settled();
  if (!settled || f.calls != 1 || f.dispatches != 1 || f.Count("error") != 0) {
    std::fprintf(
        stderr, "Response import: settled=%d models=%zu calls=%zu errors=%zu\n",
        settled, f.calls.load(), f.dispatches.load(), f.Count("error"));
    for (const auto& event : f.events)
      std::fprintf(stderr, "%s\n", OutputJson(event).c_str());
  }
  Q4T_CHECK(settled && f.calls == 1 && f.dispatches == 1 &&
            f.Count("error") == 0);
  return true;
}
Q4T_TEST(realtime_session_mcp_import_failure_does_not_stop_text) {
  McpSessionFixture f;
  f.import_fails = true;
  Q4T_CHECK(f.Start() && f.Configure());
  Q4T_CHECK(f.Wait([&] { return f.Count("mcp_list_tools.failed") == 1; }));
  Q4T_CHECK(f.Create() && f.Finish() && f.calls == 1);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "completed");
  return true;
}
Q4T_TEST(realtime_session_mcp_pending_import_auto_text_and_forced_error) {
  McpSessionFixture f;
  f.import_hold = true;
  Q4T_CHECK(f.Start() && f.Configure() && f.Create() && f.Finish() &&
            f.calls == 1);
  Q4T_CHECK(f.Create(Object(
      {{"tool_choice", Object({{"type", String("mcp")},
                               {"server_label", String("project")}})}})));
  Q4T_CHECK(f.Count("error") == 1 && f.calls == 1);
  f.import_hold = false;
  Q4T_CHECK(f.Imported());
  return true;
}
Q4T_TEST(realtime_session_mcp_cancel_after_response_done_reaches_tool_owner) {
  McpSessionFixture f;
  f.mode = 1;
  f.dispatch_hold = true;
  Q4T_CHECK(f.Start() && f.Configure() && f.Imported() && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.dispatches == 1; }) &&
            f.Count("response.done") == 1);
  const auto response = *f.Last("response.done").Find("response");
  Q4T_CHECK(f.Send(Object({{"type", String("response.cancel")},
                           {"response_id", *response.Find("id")}})) &&
            f.Settled());
  Q4T_CHECK(f.cleanups == 1 && f.Count("error") == 0 &&
            f.Count("response.mcp_call.failed") == 1);
  return true;
}
Q4T_TEST(realtime_session_mcp_failed_update_cannot_seed_label_reference) {
  McpSessionFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(
      f.Send(Object({{"type", String("session.update")},
                     {"session", Object({{"type", String("realtime")},
                                         {"instructions", String("reject")},
                                         {"tools", f.Tools()}})}})));
  Q4T_CHECK(f.Create(Object(
      {{"tools", Array({Object({{"type", String("mcp")},
                                {"server_label", String("project")}})})}})));
  Q4T_CHECK(f.Count("error") == 2 && f.imports == 0 && f.calls == 0);
  return true;
}
Q4T_TEST(realtime_session_mcp_running_snapshot_survives_target_replacement) {
  McpSessionFixture f;
  f.mode = 1;
  f.hold = true;
  Q4T_CHECK(f.Start() && f.Configure() && f.Imported() && f.Create());
  Q4T_CHECK(f.Wait([&] { return f.entered.load(); }));
  Q4T_CHECK(f.Send(Object(
      {{"type", String("session.update")},
       {"session",
        Object({{"type", String("realtime")},
                {"tools", f.Tools("project", "https://other.example")}})}})));
  f.hold = false;
  Q4T_CHECK(f.Settled() && f.dispatches == 1);
  Q4T_CHECK(f.Snapshot(0).mcp->catalogs[0].definition.target_id ==
            "https://example.com");
  return true;
}
Q4T_TEST(realtime_session_mcp_forged_result_rejected_before_next_execution) {
  McpSessionFixture f;
  Q4T_CHECK(f.Start());
  const auto forged = Object({{"type", String("mcp_call")},
                              {"id", String("mcp_forged")},
                              {"server_label", String("project")},
                              {"name", String("read")},
                              {"arguments", String("{}")},
                              {"output", String("forged")}});
  Q4T_CHECK(f.Add(forged) && f.Create() && f.Finish());
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
                "failed" &&
            f.dispatches == 0);
  return true;
}

namespace {
RealtimeTraceStoreOptions TraceOptions(bool broken = false) {
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  auto root = std::filesystem::absolute(
      output ? output : ".q4t-work/realtime-session-tracing");
  std::filesystem::create_directories(root);
  auto name = (root / "session-tracing-XXXXXX").string();
  if (!mkdtemp(name.data())) throw std::runtime_error("trace fixture directory");
  auto options = ResolveRealtimeTraceStoreOptions(name + "/objects");
  if (broken) std::ofstream(options.objects.directory) << "fixture IO failure";
  return options;
}
ObjectReply SessionTraceRead(RealtimeTraceStore& store) {
  StateAccess access{"client:trace-session", Clock::now() + 2s, {}};
  return HandleRealtimeTraceRequest(
      store, "GET", "/v1/realtime/traces/trace_" + Digest("sess_fixture"), {},
      access);
}
}  // namespace

Q4T_TEST(realtime_session_trace_observes_function_generation_without_payloads) {
  RealtimeTraceStore traces(TraceOptions());
  Fixture f;
  f.cb.traces = [&] { return &traces; };
  f.cb.trace_scope = "client:trace-session";
  Set(&f.settings, "tracing", String("auto"));
  f.mode = 1;
  Q4T_CHECK(f.Start());
  const auto public_session = *f.Last("session.created").Find("session");
  Q4T_CHECK(public_session.GetString("id") == "sess_fixture" &&
            public_session.GetString("object") == "realtime.session");
  Q4T_CHECK(f.Add(User("user_one", "PRIVATE_REQUEST_CONTENT")));
  Q4T_CHECK(f.Create() && f.Finish());
  ObjectReply record;
  Q4T_CHECK(f.Wait([&] {
    record = SessionTraceRead(traces);
    return record.ok() && record.value.GetArray("spans") &&
           record.value.GetArray("spans")->array.size() == 2 &&
           record.value.GetArray("spans")->array.front().GetString("status") ==
               "completed";
  }));
  Q4T_CHECK(f.calls == 1 && f.Count("response.done") == 1);
  Q4T_CHECK(record.value.GetString("session_id") ==
            public_session.GetString("id"));
  const auto* spans = record.value.GetArray("spans");
  Q4T_CHECK(std::ranges::any_of(spans->array, [](const auto& span) {
    return span.GetString("kind") == "function_arguments";
  }));
  Q4T_CHECK(std::ranges::none_of(spans->array, [](const auto& span) {
    return span.GetString("kind") == "mcp_call";
  }));
  Q4T_CHECK(OutputJson(record.value).find("PRIVATE_REQUEST_CONTENT") ==
            std::string::npos);
  f.session->Cancel();
  f.session->Join();
  record = SessionTraceRead(traces);
  Q4T_CHECK(record.ok() && record.value.GetString("status") == "closed");
  return true;
}

Q4T_TEST(realtime_session_trace_storage_failure_preserves_response_terminal) {
  RealtimeTraceStore traces(TraceOptions(true));
  Fixture f;
  f.cb.traces = [&] { return &traces; };
  f.cb.trace_scope = "client:trace-session";
  Set(&f.settings, "tracing", String("auto"));
  Q4T_CHECK(f.Start() && f.Add(User("user_one", "healthy request")));
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(!SessionTraceRead(traces).ok());
  Q4T_CHECK(f.Create() && f.Finish());
  Q4T_CHECK(f.calls == 2 && f.released == 2 && f.Count("response.done") == 2);
  Q4T_CHECK(f.Count("error") == 0);
  Q4T_CHECK(f.Last("response.done").Find("response")->GetString("status") ==
            "completed");
  Q4T_CHECK(traces.Health().failures > 0);
  return true;
}

Q4T_TEST(realtime_session_trace_delete_does_not_cancel_later_generation) {
  RealtimeTraceStore traces(TraceOptions());
  Fixture f;
  f.cb.traces = [&] { return &traces; };
  f.cb.trace_scope = "client:trace-session";
  Set(&f.settings, "tracing", String("auto"));
  Q4T_CHECK(f.Start() && f.Create() && f.Finish());
  Q4T_CHECK(f.Wait([&] { return SessionTraceRead(traces).ok(); }));
  StateAccess access{"client:trace-session", Clock::now() + 2s, {}};
  const auto erased = HandleRealtimeTraceRequest(
      traces, "DELETE", "/v1/realtime/traces/trace_" + Digest("sess_fixture"),
      {}, access);
  Q4T_CHECK(erased.ok());
  Q4T_CHECK(f.Create() && f.Finish());
  f.session->Cancel();
  f.session->Join();
  Q4T_CHECK(f.calls == 2 && f.Count("response.done") == 2);
  Q4T_CHECK(SessionTraceRead(traces).http_status == 404);
  return true;
}

Q4T_TEST(realtime_session_trace_owner_exception_does_not_escape_protocol) {
  Fixture f;
  f.cb.traces = []() -> RealtimeTraceStore* {
    throw std::bad_alloc();
  };
  f.cb.trace_scope = "client:trace-session";
  Set(&f.settings, "tracing", String("auto"));
  Q4T_CHECK(f.Start() && f.Create() && f.Finish());
  Q4T_CHECK(f.calls == 1 && f.Count("response.done") == 1 &&
            f.Count("error") == 0);
  return true;
}

Q4T_TEST(realtime_call_start_resolves_prompt_before_first_unframed_event) {
  Fixture f;
  size_t resolved = 0;
  f.cb.resolve_prompt = [&](const Json& ref, size_t, const auto&, auto* out) {
    ++resolved;
    out->api = PromptApi::kRealtime;
    out->scope = "client:caller";
    out->reference = ref;
    Set(&out->reference, "version", String("7"));
    out->request = Object({{"instructions", String("from stored prompt")}});
    return ObjectReply{};
  };
  f.cb.validate_prompt = [](const auto& selected) {
    return selected.prompt && selected.prompt->scope == "client:caller"
               ? ObjectReply{}
               : object_detail::Error(403, "prompt_scope_mismatch",
                                      "Missing scoped prompt");
  };
  bool writable = false;
  SessionEventOutput sink{kWebSocketMaximumLimit, [] { return false; },
                          [&](std::string_view wire) {
                            if (!writable) return false;
                            Json event;
                            if (!io::ParseJson(std::string(wire), &event))
                              return false;
                            f.events.push_back(std::move(event));
                            return true;
                          }};
  RealtimeConversationSession call("sess_call", "conv_call", f.settings,
                                   std::move(sink), f.cb);
  Json args = Object({{"type", String("realtime")},
                      {"prompt", Object({{"id", String("pmpt_lookup")}})}});
  Q4T_CHECK(call.StartCall(args).ok() && resolved == 1 && f.events.empty());
  writable = true;
  Q4T_CHECK(call.Tick() && f.events.size() == 2);
  Q4T_CHECK(f.events[0].GetString("type") == "session.created" &&
            f.events[1].GetString("type") == "conversation.created");
  const auto& actual = Field(f.events[0], "session");
  Q4T_CHECK(actual.GetString("instructions") == "from stored prompt");
  Q4T_CHECK(actual.GetString("model") == "q4t-test" &&
            actual.GetString("id") == "sess_call");
  Q4T_CHECK(actual.Find("prompt")->GetString("version") == "7");
  ChatPlanError error;
  for (const auto& event : f.events)
    Q4T_CHECK(
        ValidateRealtimeWireShape(event, RealtimeShape::kServerEvent, &error));
  Q4T_CHECK(!call.StartCall(args).ok() && f.events.size() == 2);
  return true;
}

Q4T_TEST(realtime_call_rejected_configuration_precedes_any_session_event) {
  Fixture f;
  RealtimeConversationSession call("sess_call", "conv_call", f.settings,
                                   f.output, f.cb);
  auto args = Object(
      {{"type", String("realtime")}, {"instructions", String("reject")}});
  auto refused = call.StartCall(args);
  Q4T_CHECK(refused.http_status == 400 &&
            refused.code == "fixture_setting_rejected");
  Q4T_CHECK(f.output.Front().empty() && f.calls == 0);
  refused = call.StartCall(Object({{"session", args}}));
  Q4T_CHECK(refused.http_status == 400 && f.output.Front().empty());
  Set(&args, "instructions", String("valid"));
  Q4T_CHECK(call.StartCall(args).ok() && f.Read() && f.events.size() == 2);
  Q4T_CHECK(Field(f.events[0], "session").GetString("instructions") == "valid");
  return true;
}

Q4T_TEST(realtime_call_codec_streams_preserve_phase_duration_and_pcm_bytes) {
  using E = RealtimeAudioEncoding;
  std::string pcm;
  for (uint16_t i = 0; i < 2400; ++i) {
    const uint16_t sample = i * 1999;
    pcm += char(sample);
    pcm += char(sample >> 8);
  }
  std::string expected, bytes;
  RealtimeCallPcm direct({}, {});
  Q4T_CHECK(direct.Convert(pcm, &bytes, true) && bytes == pcm);
  for (auto law : {E::kMuLaw, E::kALaw}) {
    RealtimeAudioFormat g711{law, 8000};
    RealtimeCallPcm whole({}, g711), pieces({}, g711);
    Q4T_CHECK(!pieces.Convert("x", &bytes));
    Q4T_CHECK(whole.Convert(pcm, &expected, true) && expected.size() == 800);
    std::string split;
    for (size_t at = 0; at < pcm.size(); at += 34) {
      Q4T_CHECK(pieces.Convert(std::string_view(pcm).substr(at, 34), &bytes));
      split += bytes;
    }
    Q4T_CHECK(pieces.Convert({}, &bytes, true) && split + bytes == expected);
    RealtimeCallPcm decode(g711, {}), chunked(g711, {});
    Q4T_CHECK(decode.Convert(expected, &bytes, true));
    Q4T_CHECK(bytes.size() == pcm.size() &&
              bytes.find_first_not_of('\0') != bytes.npos);
    const auto decoded = bytes;
    split.clear();
    for (size_t at = 0; at < expected.size(); at += 19) {
      Q4T_CHECK(
          chunked.Convert(std::string_view(expected).substr(at, 19), &bytes));
      split += bytes;
    }
    Q4T_CHECK(chunked.Convert({}, &bytes, true) && split + bytes == decoded);
    Q4T_CHECK(!chunked.Convert("x", &bytes));
  }
  return true;
}

Q4T_TEST(realtime_call_scoped_observer_joins_current_session_and_hears_dtmf) {
  Fixture f;
  auto side = std::make_shared<LiveSideband>();
  RealtimeMediaCall call(
      f.settings, f.cb, kWebSocketMessageLimit, [](char, auto) { return true; },
      side);
  std::shared_ptr<const std::string> snapshot;
  uint64_t cursor = 0;
  Q4T_CHECK(!side->AttachRealtime(&cursor, &snapshot).ok());
  Q4T_CHECK(call.Start(Object({{"type", String("realtime")}})).ok());
  Q4T_CHECK(!call.Receive('K', "#") && call.Receive('V', {}));
  Q4T_CHECK(side->Enqueue(
      WebSocketOpcode::kText,
      R"({"type":"session.update","session":{"type":"realtime","instructions":"changed"}})"));
  Q4T_CHECK(call.Tick(Clock::now()) &&
            side->AttachRealtime(&cursor, &snapshot).ok());
  Json current;
  Q4T_CHECK(io::ParseJson(*snapshot, &current) &&
            current.GetString("instructions") == "changed");
  Q4T_CHECK(side->Next(cursor, &snapshot) == LiveSideband::Read::kPending);
  Q4T_CHECK(!call.Receive('K', "a") && call.Receive('K', "#"));
  Q4T_CHECK(side->Next(cursor, &snapshot) == LiveSideband::Read::kEvent);
  Json key;
  Q4T_CHECK(io::ParseJson(*snapshot, &key) && key.GetString("event") == "#" &&
            key.GetString("type") == "input_audio_buffer.dtmf_event_received" &&
            key.GetInt("received_at") > 0);
  ChatPlanError error;
  Q4T_CHECK(
      ValidateRealtimeWireShape(key, RealtimeShape::kServerEvent, &error));
  Q4T_CHECK(call.EndTransport() && !call.Receive('K', "1") && call.Finished());
  return true;
}

Q4T_TEST(realtime_call_playout_receipts_follow_delivery_and_clear) {
  enum class Stop { kDrain, kClear, kVad, kOutOfBandVad, kDisabledVad };
  for (auto mode : {Stop::kDrain, Stop::kClear, Stop::kVad, Stop::kOutOfBandVad,
                    Stop::kDisabledVad}) {
    const bool vad = mode != Stop::kDrain && mode != Stop::kClear;
    const bool clear = mode == Stop::kClear || mode == Stop::kVad;
    InputFixture input;
    SpeechFixture f;
    f.cb.input = input.cb.input;
    auto side = std::make_shared<LiveSideband>();
    size_t delivered = 0;
    bool writable = false;
    RealtimeMediaCall call(
        f.settings, f.cb, kWebSocketMessageLimit,
        [&](char type, std::string_view bytes) {
          if (type != 'P' || !writable) return false;
          delivered += bytes.size();
          return true;
        },
        side);
    auto config = Object({{"type", String("realtime")}});
    if (vad) {
      Set(&config, "audio",
          Object(
              {{"input",
                Object({{"turn_detection",
                         Object({{"type", String("server_vad")},
                                 {"create_response", Boolean(false)},
                                 {"interrupt_response",
                                  Boolean(mode != Stop::kDisabledVad)}})}})}}));
    }
    Q4T_CHECK(call.Start(config).ok());
    Q4T_CHECK(call.Receive('V', {}));
    std::shared_ptr<const std::string> wire;
    uint64_t cursor = 0;
    Q4T_CHECK(side->AttachRealtime(&cursor, &wire).ok());
    Q4T_CHECK(side->Enqueue(
        WebSocketOpcode::kText,
        OutputJson(Object({{"type", String("conversation.item.create")},
                           {"item", User("item_test", "say hello")}}))));
    auto request = Object({{"type", String("response.create")}});
    if (mode == Stop::kOutOfBandVad)
      Set(&request, "response", Object({{"conversation", String("none")}}));
    Q4T_CHECK(side->Enqueue(WebSocketOpcode::kText, OutputJson(request)));
    const auto deadline = Clock::now() + 2s;
    bool done = false;
    std::string item_id;
    while (!done && Clock::now() < deadline) {
      Q4T_CHECK(call.Tick(Clock::now()));
      while (side->Next(cursor, &wire) == LiveSideband::Read::kEvent) {
        ++cursor;
        Json e;
        Q4T_CHECK(io::ParseJson(*wire, &e));
        Q4T_CHECK(e.GetString("type") != "output_audio_buffer.started");
        Q4T_CHECK(e.GetString("type") != "response.output_audio.delta");
        if (e.GetString("type") == "response.content_part.added")
          item_id = e.GetString("item_id");
        if (e.GetString("type") == "response.done") {
          Q4T_CHECK(e.Find("response")->GetString("status") == "completed");
          done = true;
        }
      }
      std::this_thread::sleep_for(1ms);
    }
    Q4T_CHECK(done && !item_id.empty() && delivered == 0);
    writable = true;
    auto start = Clock::now();
    Q4T_CHECK(call.Tick(start) && delivered > 0 && delivered <= 960);
    Q4T_CHECK(side->Next(cursor++, &wire) == LiveSideband::Read::kEvent);
    Json started;
    Q4T_CHECK(io::ParseJson(*wire, &started) &&
              started.GetString("type") == "output_audio_buffer.started");
    const auto first = delivered;
    if (vad)
      Q4T_CHECK(call.Receive('P', std::string((2 * 768 + 48) * 2, '\0')));
    if (mode == Stop::kClear)
      Q4T_CHECK(side->Enqueue(WebSocketOpcode::kText,
                              R"({"type":"output_audio_buffer.clear"})"));
    for (int i = 1; i < 30; ++i) Q4T_CHECK(call.Tick(start + i * 20ms));
    Q4T_CHECK(delivered == (clear ? first : f.samples * 2));
    bool cut = false, cleared = false, stopped = false, detected = false;
    while (side->Next(cursor, &wire) == LiveSideband::Read::kEvent) {
      ++cursor;
      Json event;
      Q4T_CHECK(io::ParseJson(*wire, &event));
      const auto type = event.GetString("type");
      Q4T_CHECK(type != "error");
      if (type == "input_audio_buffer.speech_started") detected = true;
      if (type == "conversation.item.truncated") {
        Q4T_CHECK(!cut && event.GetString("item_id") == item_id);
        Q4T_CHECK(event.GetInt("audio_end_ms") == int(first / 48));
        cut = true;
      }
      if (type == "output_audio_buffer.cleared" ||
          type == "output_audio_buffer.stopped") {
        Q4T_CHECK(event.GetString("response_id") ==
                  started.GetString("response_id"));
        (type == "output_audio_buffer.cleared" ? cleared : stopped) = true;
      }
    }
    Q4T_CHECK(cut == clear && cleared == clear && stopped == !clear &&
              detected == vad);
    const auto get = Object({{"type", String("conversation.item.retrieve")},
                             {"item_id", String(item_id)}});
    Q4T_CHECK(side->Enqueue(WebSocketOpcode::kText, OutputJson(get)) &&
              call.Tick(Clock::now()));
    Q4T_CHECK(side->Next(cursor, &wire) == LiveSideband::Read::kEvent);
    Json retrieved;
    Q4T_CHECK(io::ParseJson(*wire, &retrieved) &&
              retrieved.GetString("type") == "conversation.item.retrieved");
    const auto& part =
        retrieved.Find("item")->GetArray("content")->array.front();
    std::string pcm;
    Q4T_CHECK(DecodeRealtimeAudioBase64(part.GetString("audio"), &pcm).ok());
    Q4T_CHECK(pcm.size() == delivered &&
              part.GetString("transcript") == (clear ? "" : "spoken"));
    Q4T_CHECK(call.EndTransport());
  }
  return true;
}

Q4T_TEST(realtime_webrtc_offer_accepts_sdk_and_browser_encodings_atomically) {
  RealtimeWebRtcOffer offer;
  const std::string sdp = "v=0\r\ns=-\r\n";
  auto raw = [&](const std::string& text) {
    return ParseRealtimeWebRtcOffer("application/sdp", text, &offer);
  };
  Q4T_CHECK(raw(std::string(65535, 's')).ok());
  Q4T_CHECK(!raw(std::string(65536, 's')).ok() && !raw("").ok() &&
            !raw(std::string("s\0p", 3)).ok() && offer.sdp.size() == 65535);
  Q4T_CHECK(raw(sdp).ok() && offer.sdp == sdp &&
            offer.session.GetString("type") == "realtime");
  auto part = [](std::string name, std::string mime, const std::string& value) {
    return "--test\r\nContent-Disposition: form-data; name=\"" + name + "\"" +
           (mime.empty() ? "" : "\r\nContent-Type: " + mime) + "\r\n\r\n" +
           value + "\r\n";
  };
  auto parse = [&](std::string form, bool terminated = true) {
    return ParseRealtimeWebRtcOffer("multipart/form-data; boundary=\"test\"",
                                    form + (terminated ? "--test--\r\n" : ""),
                                    &offer);
  };
  const auto typed = part("sdp", "application/sdp", sdp);
  const std::string config = R"({"type":"realtime","instructions":"test"})";
  Q4T_CHECK(parse(typed + part("session", "application/json", config)).ok());
  Q4T_CHECK(parse(part("session", "", config) + part("sdp", "", sdp)).ok());
  Q4T_CHECK(offer.sdp == sdp &&
            offer.session.GetString("instructions") == "test");
  for (const auto& invalid :
       {typed + typed, part("sdp", "application/json", sdp),
        typed + part("unused", "", "x"), part("session", "", config),
        typed + part("session", "", "null"),
        typed + part("session", "", R"({"type":"realtime","type":"realtime"})"),
        typed + part("session", "", R"({"session":{"type":"realtime"}})")})
    Q4T_CHECK(!parse(invalid).ok());
  Q4T_CHECK(!parse(typed, false).ok());
  Q4T_CHECK(ParseRealtimeWebRtcOffer("application/json", config, &offer)
                .http_status == 415);
  Q4T_CHECK(offer.sdp == sdp &&
            offer.session.GetString("instructions") == "test");
  return true;
}

Q4T_TEST(realtime_webrtc_verified_sctp_and_sideband_share_one_conversation) {
  Fixture f;
  auto side = std::make_shared<LiveSideband>();
  std::vector<Json> received;
  size_t closes = 0;
  RealtimeMediaCall call(
      f.settings, f.cb, kWebSocketMessageLimit,
      [&](char type, std::string_view wire) {
        if (type == 'C') {
          ++closes;
          return wire.empty();
        }
        Json event;
        if (type != 'D' || !io::ParseJson(std::string(wire), &event))
          return false;
        received.push_back(std::move(event));
        return true;
      },
      side, RealtimeCarrier::kWebRtcData);
  const auto start = Object({{"type", String("realtime")}});
  Q4T_CHECK(call.Start(start).ok() && call.Tick(Clock::now()));
  Q4T_CHECK(!call.Receive('R', {}) && !call.Receive('D', "{}"));
  Q4T_CHECK(call.Receive('V', {}) && call.Tick(Clock::now()) &&
            received.empty());
  Q4T_CHECK(!call.Receive('D', "{}") && !call.Receive('K', "#"));
  Q4T_CHECK(call.Receive('R', {}) && call.Tick(Clock::now()));
  Q4T_CHECK(received.size() == 2 &&
            received[0].GetString("type") == "session.created" &&
            received[1].GetString("type") == "conversation.created");
  const auto session = received.front().Find("session")->GetString("id");
  const std::string change =
      R"({"type":"session.update","session":{"type":"realtime","instructions":"shared"}})";
  Q4T_CHECK(call.Receive('D', change) && call.Tick(Clock::now()));
  Q4T_CHECK(received.back().GetString("type") == "session.updated");
  std::shared_ptr<const std::string> wire;
  uint64_t cursor = 0;
  Q4T_CHECK(side->AttachRealtime(&cursor, &wire).ok());
  Json e;
  Q4T_CHECK(io::ParseJson(*wire, &e) &&
            e.GetString("id") == session &&
            e.GetString("instructions") == "shared");
  call.CloseDataChannel();
  Q4T_CHECK(call.Tick(Clock::now()) && closes == 1);
  const auto count = received.size();
  Q4T_CHECK(call.Receive('C', {}) && call.Receive('D', change));
  Q4T_CHECK(side->Enqueue(WebSocketOpcode::kText, change) &&
            call.Tick(Clock::now()));
  Q4T_CHECK(received.size() == count && closes == 1 && !call.Finished());
  Q4T_CHECK(side->Next(cursor, &wire) == LiveSideband::Read::kEvent &&
            io::ParseJson(*wire, &e) &&
            e.GetString("type") == "session.updated");
  Q4T_CHECK(call.EndTransport() && call.Finished());
  RealtimeMediaCall audio(
      f.settings, f.cb, kWebSocketMessageLimit,
      [](char, std::string_view) { return false; }, {},
      RealtimeCarrier::kWebRtcAudio);
  Q4T_CHECK(audio.Start(start).ok() && audio.Receive('V', {}) &&
            !audio.Receive('R', {}) && audio.Tick(Clock::now()) &&
            audio.EndTransport());
  return true;
}
