#include "q4t/server/realtime_response_output.h"

#include <algorithm>
#include <cstdio>
#include <map>

#include "q4t/server/http_response.h"
#include "q4t/server/native_chat_generation.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"
#include "../src/server/object_protocol.h"

namespace {
namespace io = q4t::io;
using namespace q4t::server;
using namespace q4t::server::object_detail;
constexpr auto kPrompt = "<|im_start|>assistant\n<think>\n\n</think>\n\n";

Json Request() {
  Json value;
  io::ParseJson(R"({"messages":[{"role":"user","content":"query"}],
    "tools":[{"type":"function","function":{"name":"lookup",
    "parameters":{"type":"object","properties":{
    "text":{"type":"string"}}}}}]})",
                &value);
  return value;
}
struct Fixture {
  bool in_conversation;
  size_t ids = 0, sink_calls = 0, fail_sink_at = 0;
  size_t item_calls = 0, fail_item_at = 0;
  bool invalid_sink_result = false;
  RealtimeConversation history{"sess_a"};
  std::map<std::string, RealtimeItemTicket> tickets;
  std::vector<Json> events;
  RealtimeResponseOutput output;
  explicit Fixture(bool conversation = true, size_t bytes = 1ULL << 20,
                   size_t items = 128)
      : in_conversation(conversation),
        output(
            {"resp_a", "conv_a", conversation, {}, bytes, items},
            [this](const auto& event) { return Event(event); },
            [this](const auto& item, bool begin, bool terminal) {
              return Item(item, begin, terminal);
            },
            [this](std::string_view prefix) {
              return std::string(prefix) + std::to_string(++ids);
            }) {}
  ObjectReply Event(const Json& event) {
    if (++sink_calls == fail_sink_at) {
      if (invalid_sink_result) {
        ObjectReply invalid;
        invalid.http_status = 500;
        return invalid;
      }
      return object_detail::Error(503, "test_backpressure",
                                  "Event not accepted");
    }
    ChatPlanError shape;
    const auto status =
        ValidateRealtimeShape(event, RealtimeShape::kServerEvent, &shape);
    if (!status) {
      std::fprintf(stderr, "SDK event mismatch %s at %s: %s\n",
                   event.GetString("type").c_str(), shape.param.c_str(),
                   status.message().c_str());
      return object_detail::Error(500, "sdk_shape_invalid", status.message());
    }
    events.push_back(event);
    return {};
  }
  ObjectReply Item(const Json& item, bool begin, bool terminal) {
    if (++item_calls == fail_item_at)
      return object_detail::Error(503, "history_failed", "Item not accepted");
    const auto id = item.GetString("id");
    const auto previous = history.LastId();
    q4t::Status status;
    if (begin) {
      RealtimeItemTicket ticket;
      status = history.BeginOutput(item, id, in_conversation, 16384, &ticket);
      if (status) tickets.emplace(id, std::move(ticket));
    } else {
      const auto found = tickets.find(id);
      if (found == tickets.end())
        return object_detail::Error(409, "item_missing", "No output owner");
      status = history.Update(found->second, item, terminal);
    }
    if (!status)
      return object_detail::Error(503, "history_capacity", status.message());
    ObjectReply reply;
    reply.value = Object({{"previous_item_id", String(previous)}});
    return reply;
  }
  size_t Count(std::string_view type) const {
    return std::count_if(events.begin(), events.end(), [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
  const Json& Final() const { return *events.back().Find("response"); }
  bool Feed(std::string_view text, bool incomplete, Json* message) {
    auto request = Request();
    ChatResponseParser parser;
    if (!parser.Init(request, kPrompt, "request", true)) return false;
    detail::Utf8Stream utf8;
    auto append = [&](std::vector<ResponseDelta>& deltas) {
      for (const auto& delta : deltas)
        if (!output.Append(delta).ok()) return false;
      return true;
    };
    for (const char byte : text) {
      const auto decoded = utf8.Append(std::string(1, byte));
      std::vector<ResponseDelta> deltas;
      if (!parser.Append(decoded, &deltas) || !append(deltas)) return false;
    }
    std::vector<ResponseDelta> deltas;
    if (!parser.Append(utf8.Finish(), &deltas) || !append(deltas)) return false;
    deltas.clear();
    auto status = incomplete ? parser.FinishIncomplete(&deltas)
                             : parser.Finish(false, &deltas);
    return status && append(deltas) &&
           io::ParseJson(parser.MessageJson(), message);
  }
};
RealtimeResponseEnd Success(const Json& message) {
  RealtimeResponseEnd end;
  end.message = &message;
  end.usage = RealtimeResponseUsage{{11, 7, 3, 0, 0}, 11, 0, 0, 7, 0};
  return end;
}
RealtimeResponseEnd Failed() {
  RealtimeResponseEnd end;
  end.state = RealtimeResponseEnd::State::kFailed;
  end.error_code = "generation_failed";
  end.error_message = "Actual worker reported a failure";
  return end;
}
ResponseDelta Text(std::string value) {
  ResponseDelta result;
  result.text = std::move(value);
  return result;
}
Json Message(std::string value) {
  return Object({{"role", String("assistant")}, {"content", String(value)}});
}
}  // namespace

Q4T_TEST(realtime_output_actual_parser_text_tools_and_sdk_events_agree) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Json message;
  Q4T_CHECK(
      f.Feed("查询：<tool_call><function=lookup><parameter=text>"
             "真实数据</parameter></function></tool_call>",
             false, &message));
  Q4T_CHECK(f.Count("response.output_text.delta") >= 3);
  Q4T_CHECK(f.Count("response.function_call_arguments.delta") > 2);
  Q4T_CHECK(f.Count("response.done") == 0);
  Q4T_CHECK(f.output.Finish(Success(message)).ok());
  Q4T_CHECK(f.output.Terminal() && f.Count("response.done") == 1);
  Q4T_CHECK(f.Final().GetString("status") == "completed");
  Q4T_CHECK(f.Count("response.output_item.done") == 2);
  Q4T_CHECK(f.Count("conversation.item.done") == 2);
  const auto* usage = f.Final().Find("usage");
  Q4T_CHECK(usage && usage->GetInt("total_tokens") == 18);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 2);
  const auto* calls = message.GetArray("tool_calls");
  Q4T_CHECK(calls &&
            snapshot.items.array[1].GetString("arguments") ==
                calls->array[0].Find("function")->GetString("arguments"));
  return true;
}

Q4T_TEST(realtime_output_truncated_tool_remains_explicitly_incomplete) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Json message;
  Q4T_CHECK(f.Feed("<tool_call><function=lookup><parameter=text>partial", true,
                   &message));
  Q4T_CHECK(!message.Has("tool_calls"));
  auto end = Success(message);
  end.state = RealtimeResponseEnd::State::kIncomplete;
  end.reason = "max_output_tokens";
  Q4T_CHECK(f.output.Finish(end).ok());
  Q4T_CHECK(f.Final().GetString("status") == "incomplete");
  Q4T_CHECK(f.Count("response.function_call_arguments.done") == 1);
  const auto& item = f.Final().GetArray("output")->array[0];
  Q4T_CHECK(item.GetString("status") == "incomplete");
  Json arguments;
  Q4T_CHECK(!io::ParseJson(item.GetString("arguments"), &arguments));
  return true;
}

Q4T_TEST(realtime_output_cancel_and_failure_have_one_honest_terminal) {
  for (const bool cancelled : {false, true}) {
    Fixture f;
    Q4T_CHECK(f.output.Begin().ok() && f.output.Append(Text("partial")).ok());
    auto end = Failed();
    if (cancelled) {
      end.state = RealtimeResponseEnd::State::kCancelled;
      end.reason = "client_cancelled";
    }
    Q4T_CHECK(f.output.Finish(end).ok());
    const auto count = f.events.size();
    Q4T_CHECK(!f.output.Finish(end).ok());
    Q4T_CHECK(!f.output.Append(Text("late")).ok());
    Q4T_CHECK(f.events.size() == count && f.Count("response.done") == 1);
    Q4T_CHECK(f.Final().GetString("status") ==
              (cancelled ? "cancelled" : "failed"));
    Q4T_CHECK(!f.Final().Has("usage"));
    Q4T_CHECK(f.Final().GetArray("output")->array[0].GetString("status") ==
              "incomplete");
  }
  return true;
}

Q4T_TEST(realtime_output_capacity_failure_still_closes_healthy_transport) {
  Fixture f(true, 4);
  Q4T_CHECK(f.output.Begin().ok() && f.output.Append(Text("abc")).ok());
  Q4T_CHECK(!f.output.Append(Text("de")).ok());
  Q4T_CHECK(f.output.Finish(Failed()).ok());
  Q4T_CHECK(f.Count("response.done") == 1 && f.output.Terminal());
  Q4T_CHECK(!f.output.Error().ok());
  Q4T_CHECK(f.Final()
                .GetArray("output")
                ->array[0]
                .GetArray("content")
                ->array[0]
                .GetString("text") == "abc");
  return true;
}

Q4T_TEST(realtime_output_failed_sink_is_not_retried_or_replayed) {
  Fixture f;
  f.fail_sink_at = 2;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(!f.output.Append(Text("content")).ok());
  const auto attempts = f.sink_calls;
  Q4T_CHECK(!f.output.Finish(Failed()).ok());
  Q4T_CHECK(!f.output.Append(Text("retry")).ok());
  Q4T_CHECK(f.sink_calls == attempts && f.Count("response.done") == 0);
  return true;
}

Q4T_TEST(realtime_output_out_of_band_keeps_default_history_unchanged) {
  Fixture f(false);
  Q4T_CHECK(f.output.Begin().ok());
  Json message;
  Q4T_CHECK(f.Feed("Separate result", false, &message));
  Q4T_CHECK(f.output.Finish(Success(message)).ok());
  Q4T_CHECK(f.Count("conversation.item.added") == 0 &&
            f.Count("conversation.item.done") == 0);
  Q4T_CHECK(!f.Final().Has("conversation_id"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(f.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  const auto id = f.Final().GetArray("output")->array[0].GetString("id");
  auto input =
      Array({Object({{"type", String("item_reference")}, {"id", String(id)}})});
  Q4T_CHECK(f.history.Snapshot(&input, &snapshot) &&
            snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_output_history_failure_never_replays_partial_finalization) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Json message;
  Q4T_CHECK(
      f.Feed("Result <tool_call><function=lookup><parameter=text>value"
             "</parameter></function></tool_call>",
             false, &message));
  // The first item commits and publishes; the second fails. Retrying the
  // terminal must neither replay the first nor invent a completed response.
  f.fail_item_at = f.item_calls + 2;
  Q4T_CHECK(!f.output.Finish(Success(message)).ok());
  Q4T_CHECK(f.Count("response.output_item.done") == 1);
  const auto events = f.sink_calls, writes = f.item_calls;
  Q4T_CHECK(!f.output.Finish(Failed()).ok());
  Q4T_CHECK(!f.output.Append(Text("retry")).ok());
  Q4T_CHECK(f.sink_calls == events && f.item_calls == writes);
  Q4T_CHECK(f.Count("response.done") == 0);
  return true;
}

Q4T_TEST(realtime_output_http_failure_without_error_code_is_not_accepted) {
  Fixture f;
  f.invalid_sink_result = true;
  f.fail_sink_at = 2;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(!f.output.Append(Text("answer")).ok());
  Q4T_CHECK(f.output.Error().code == "realtime_sink_result_invalid");
  Q4T_CHECK(!f.output.Finish(Failed()).ok() && f.sink_calls == 2);
  return true;
}

Q4T_TEST(realtime_output_final_message_mismatch_cannot_become_success) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok() && f.output.Append(Text("actual")).ok());
  const auto wrong = Message("invented");
  Q4T_CHECK(!f.output.Finish(Success(wrong)).ok());
  Q4T_CHECK(f.Count("response.done") == 0);
  Q4T_CHECK(f.output.Finish(Failed()).ok());
  Q4T_CHECK(f.Final().GetString("status") == "failed");
  return true;
}

Q4T_TEST(realtime_output_unknown_usage_does_not_fail_healthy_answer) {
  for (const bool invalid : {false, true}) {
    Fixture f;
    Q4T_CHECK(f.output.Begin().ok() && f.output.Append(Text("answer")).ok());
    const auto message = Message("answer");
    auto end = Success(message);
    if (invalid)
      end.usage->input_audio = 12;  // Cannot fit the observed total of 11.
    else
      end.usage.reset();
    Q4T_CHECK(f.output.Finish(end).ok());
    Q4T_CHECK(f.Final().GetString("status") == "completed");
    Q4T_CHECK(!f.Final().Has("usage") && !f.output.UsageAvailable());
  }
  return true;
}

Q4T_TEST(realtime_output_usage_does_not_invent_unknown_token_categories) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok() && f.output.Append(Text("answer")).ok());
  const auto message = Message("answer");
  auto end = Success(message);
  end.usage = RealtimeResponseUsage{};
  end.usage->total = {11, 7, 3, 0, 0};
  Q4T_CHECK(f.output.Finish(end).ok() && f.output.UsageAvailable());
  const auto& usage = *f.Final().Find("usage");
  Q4T_CHECK(usage.GetInt("input_tokens") == 11);
  const auto* detail = usage.Find("input_token_details");
  Q4T_CHECK(detail && !detail->Has("text_tokens") &&
            !detail->Has("audio_tokens"));
  Q4T_CHECK(detail->GetInt("cached_tokens") == 3);
  return true;
}

Q4T_TEST(realtime_output_private_reasoning_stays_out_of_public_text) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  auto reasoning = Text("private thought");
  reasoning.kind = ResponseDelta::Kind::kReasoning;
  Q4T_CHECK(f.output.Append(reasoning).ok());
  Q4T_CHECK(f.Count("response.output_item.added") == 0);
  Q4T_CHECK(f.output.Append(Text("answer")).ok());
  const auto message = Message("answer");
  Q4T_CHECK(f.output.Finish(Success(message)).ok());
  Q4T_CHECK(OutputJson(f.Final()).find("private thought") == std::string::npos);
  return true;
}

Q4T_TEST(realtime_output_rejects_unbound_function_fragment) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = false;
  delta.call.arguments = "fragment";
  Q4T_CHECK(!f.output.Append(delta).ok());
  Q4T_CHECK(f.Count("response.output_item.added") == 0);
  Q4T_CHECK(f.output.Finish(Failed()).ok());
  return true;
}

Q4T_TEST(native_chat_sink_retains_first_failure_and_stops_callbacks) {
  const auto request = Request();
  size_t calls = 0;
  NativeChatGeneration native(request, [&](const auto&) {
    ++calls;
    return object_detail::Error(409, "cancelled", "Caller cancelled");
  });
  Q4T_CHECK(!native.Emit(Text("first")));
  Q4T_CHECK(!native.Emit(Text("second")));
  Q4T_CHECK(calls == 1 && native.Error().code == "cancelled");
  Q4T_CHECK(native.AcceptedBytes() == 0);
  return true;
}

Q4T_TEST(native_chat_sink_is_bounded_and_sealed_after_owner_returns) {
  const auto request = Request();
  size_t calls = 0;
  NativeChatGeneration native(
      request,
      [&](const auto&) {
        ++calls;
        return ObjectReply{};
      },
      4);
  Q4T_CHECK(native.Emit(Text("abc")) && native.AcceptedBytes() == 3);
  native.Seal();
  Q4T_CHECK(!native.Emit(Text("d")) && calls == 1);
  NativeChatGeneration bounded(
      request,
      [&](const auto&) {
        ++calls;
        return ObjectReply{};
      },
      2);
  Q4T_CHECK(!bounded.Emit(Text("abc")) && calls == 1);
  Q4T_CHECK(bounded.Error().code == "native_output_capacity_exhausted");
  return true;
}

Q4T_TEST(native_chat_sink_does_not_expose_private_chat_tool_or_review_steps) {
  for (const auto* field :
       {"moderation", "web_search_options", "modalities", "audio"}) {
    auto request = Request();
    Set(&request, field, Object());
    bool called = false;
    NativeChatGeneration native(request, [&](const auto&) {
      called = true;
      return ObjectReply{};
    });
    Q4T_CHECK(!native.Valid() && !native.Emit(Text("private")) && !called);
  }
  return true;
}

Q4T_TEST(native_chat_sink_requires_confirmed_success) {
  const auto request = Request();
  for (const bool unknown : {false, true}) {
    size_t calls = 0;
    NativeChatGeneration native(request, [&](const auto&) {
      ++calls;
      ObjectReply reply;
      reply.http_status = unknown ? 200 : 500;
      reply.commit_unknown = unknown;
      return reply;
    });
    Q4T_CHECK(!native.Emit(Text("answer")));
    Q4T_CHECK(native.Error().code == "native_sink_result_invalid");
    Q4T_CHECK(native.AcceptedBytes() == 0);
    Q4T_CHECK(!native.Emit(Text("retry")) && calls == 1);
  }
  return true;
}
