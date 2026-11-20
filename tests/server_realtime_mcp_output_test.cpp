#include "q4t/server/realtime_response_output.h"

#include <algorithm>
#include <map>

#include "../src/server/object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/responses_mcp_binding.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
RealtimeMcpOutputBinding Binding(std::string label = "project",
                                 std::string name = "read") {
  return {ResponsesMcpModelName(label, name), label, name};
}
struct Fixture {
  std::vector<RealtimeMcpOutputBinding> bindings;
  std::vector<Json> events;
  RealtimeConversation history{"sess_output"};
  std::map<std::string, RealtimeItemTicket> tickets;
  size_t next_id = 0, attempts = 0, fail_at = SIZE_MAX;
  bool drop_items = false;
  RealtimeResponseOutput output;
  static RealtimeResponseOutputOptions Options(
      const std::vector<RealtimeMcpOutputBinding>& bindings,
      bool conversation) {
    RealtimeResponseOutputOptions options;
    options.response_id = "resp_owned";
    options.conversation_id = "conv_owned";
    options.in_conversation = conversation;
    options.mcp_bindings = bindings;
    return options;
  }
  explicit Fixture(std::vector<RealtimeMcpOutputBinding> selected = {Binding()},
                   bool conversation = true)
      : bindings(std::move(selected)),
        output(
            Options(bindings, conversation),
            [this](const Json& event) {
              if (++attempts == fail_at)
                return Error(503, "fixture_sink_failure", "Closed output");
              ChatPlanError error;
              const auto shape = ValidateRealtimeShape(
                  event, RealtimeShape::kServerEvent, &error);
              if (!shape)
                return Error(500, "fixture_shape_failure",
                             error.param + ": " + shape.message());
              events.push_back(event);
              return ObjectReply{};
            },
            [this, conversation](const Json& item, bool begin, bool terminal) {
              ObjectReply reply;
              if (drop_items && !begin) {
                reply.value = Object({{"deleted", Boolean(true)}});
                return reply;
              }
              const auto id = item.GetString("id");
              q4t::Status status;
              if (begin) {
                RealtimeItemTicket ticket;
                status =
                    history.BeginOutput(item, id, conversation, 8192, &ticket);
                if (status) tickets.emplace(id, ticket);
              } else {
                const auto found = tickets.find(id);
                if (found == tickets.end())
                  return Error(500, "fixture_ticket_missing", "Missing owner");
                status = history.Update(found->second, item, terminal, true);
              }
              return status ? reply
                            : Error(503, "fixture_history_failure",
                                    status.message());
            },
            [this](std::string_view prefix) {
              return std::string(prefix) + std::to_string(++next_id);
            }) {}
  size_t Count(std::string_view type) const {
    return std::ranges::count_if(events, [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
  bool Feed(std::string_view text, Json* message, bool incomplete = false) {
    const auto parameters =
        Object({{"type", String("object")},
                {"properties",
                 Object({{"text", Object({{"type", String("string")}})}})}});
    auto tools = Array();
    for (const auto& binding : bindings)
      tools.array.push_back(
          Object({{"type", String("function")},
                  {"function", Object({{"name", String(binding.model_name)},
                                       {"parameters", parameters}})}}));
    tools.array.push_back(Object(
        {{"type", String("function")},
         {"function",
          Object({{"name", String("local")}, {"parameters", parameters}})}}));
    const auto request =
        Object({{"messages", Array({Object({{"role", String("user")},
                                            {"content", String("query")}})})},
                {"tools", tools}});
    ChatResponseParser parser;
    if (!parser.Init(request, "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                     "request", true))
      return false;
    detail::Utf8Stream utf8;
    const auto append = [&](std::vector<ResponseDelta>* deltas) {
      for (const auto& delta : *deltas)
        if (!output.Append(delta).ok()) return false;
      deltas->clear();
      return true;
    };
    for (const char byte : text) {
      std::vector<ResponseDelta> deltas;
      if (!parser.Append(utf8.Append(std::string(1, byte)), &deltas) ||
          !append(&deltas))
        return false;
    }
    std::vector<ResponseDelta> deltas;
    if (!parser.Append(utf8.Finish(), &deltas) || !append(&deltas))
      return false;
    const auto status = incomplete ? parser.FinishIncomplete(&deltas)
                                   : parser.Finish(false, &deltas);
    return status && append(&deltas) &&
           q4t::io::ParseJson(parser.MessageJson(), message);
  }
};
std::string Tool(std::string name, std::string argument = "真实资料") {
  return "<tool_call><function=" + name + "><parameter=text>" + argument +
         "</parameter></function></tool_call>";
}
RealtimeResponseEnd Complete(const Json& message) {
  RealtimeResponseEnd end;
  end.message = &message;
  return end;
}
RealtimeResponseEnd Failed() {
  RealtimeResponseEnd end;
  end.state = RealtimeResponseEnd::State::kFailed;
  end.error_code = "generation_failed";
  end.error_message = "Generation failed";
  return end;
}
ResponseDelta Direct(std::string name, std::string arguments,
                     bool start = true) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = start;
  delta.call.id = "model_call_owned";
  delta.call.name = name;
  delta.call.index = 0;
  delta.call.arguments = arguments;
  return delta;
}
Json Finite(std::string name, std::string arguments) {
  return Object(
      {{"role", String("assistant")},
       {"content", String("")},
       {"tool_calls",
        Array({Object(
            {{"id", String("model_call_owned")},
             {"type", String("function")},
             {"function", Object({{"name", String(name)},
                                  {"arguments", String(arguments)}})}})})}});
}
}  // namespace

Q4T_TEST(realtime_mcp_output_real_parser_stream_matches_public_sdk_and_model) {
  Fixture fixture;
  Q4T_CHECK(fixture.output.Begin().ok());
  Json message;
  Q4T_CHECK(
      fixture.Feed("I will query. " + Tool(Binding().model_name), &message));
  Q4T_CHECK(fixture.output.Finish(Complete(message)).ok());
  Q4T_CHECK(fixture.Count("response.mcp_call_arguments.delta") > 1);
  Q4T_CHECK(fixture.Count("response.mcp_call_arguments.done") == 1 &&
            fixture.Count("response.function_call_arguments.delta") == 0);
  const auto calls = fixture.output.TakeMcpCalls();
  Q4T_CHECK(calls.size() == 1 && calls[0].output_index == 1 &&
            calls[0].binding_index == 0);
  Q4T_CHECK(calls[0].item.GetString("type") == "mcp_call" &&
            calls[0].item.GetString("name") == "read" &&
            calls[0].item.GetString("server_label") == "project");
  std::string streamed;
  for (const auto& event : fixture.events) {
    Q4T_CHECK(OutputJson(event).find(Binding().model_name) ==
              std::string::npos);
    if (event.GetString("type") == "response.mcp_call_arguments.delta") {
      streamed += event.GetString("delta");
      Q4T_CHECK(!event.Has("call_id") && !event.Has("name"));
    }
  }
  Q4T_CHECK(streamed == calls[0].item.GetString("arguments"));
  Q4T_CHECK(fixture.Count("response.mcp_call.completed") == 0 &&
            fixture.Count("response.mcp_call.in_progress") == 0 &&
            fixture.Count("response.output_item.done") == 1);
  return true;
}

Q4T_TEST(realtime_mcp_output_equal_remote_names_keep_distinct_server_bindings) {
  Fixture fixture({Binding("first"), Binding("second")});
  Q4T_CHECK(fixture.output.Begin().ok());
  Json message;
  Q4T_CHECK(fixture.Feed(
      Tool(Binding("first").model_name) + Tool(Binding("second").model_name),
      &message));
  Q4T_CHECK(fixture.output.Finish(Complete(message)).ok());
  const auto calls = fixture.output.TakeMcpCalls();
  Q4T_CHECK(calls.size() == 2 && calls[0].binding_index == 0 &&
            calls[1].binding_index == 1);
  Q4T_CHECK(calls[0].item.GetString("server_label") == "first" &&
            calls[1].item.GetString("server_label") == "second");
  Q4T_CHECK(calls[0].item.GetString("id") != calls[1].item.GetString("id"));
  return true;
}

Q4T_TEST(realtime_mcp_output_mixed_function_and_mcp_terminal_ownership) {
  Fixture fixture;
  Q4T_CHECK(fixture.output.Begin().ok());
  Json message;
  Q4T_CHECK(fixture.Feed(Tool("local") + Tool(Binding().model_name), &message));
  Q4T_CHECK(fixture.output.Finish(Complete(message)).ok());
  Q4T_CHECK(fixture.Count("response.function_call_arguments.done") == 1 &&
            fixture.Count("response.mcp_call_arguments.done") == 1);
  Q4T_CHECK(fixture.Count("conversation.item.done") == 1 &&
            fixture.Count("response.output_item.done") == 1);
  const auto calls = fixture.output.TakeMcpCalls();
  Q4T_CHECK(calls.size() == 1 && calls[0].output_index == 1);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(!fixture.history.Snapshot(nullptr, &snapshot));
  return true;
}

Q4T_TEST(
    realtime_mcp_output_incomplete_generation_never_hands_off_even_closed_args) {
  for (const bool closed : {false, true}) {
    Fixture fixture;
    Q4T_CHECK(fixture.output.Begin().ok());
    Json message;
    const auto tool = closed ? Tool(Binding().model_name)
                             : "<tool_call><function=" + Binding().model_name +
                                   "><parameter=text>partial";
    Q4T_CHECK(fixture.Feed(tool, &message, true));
    auto end = Complete(message);
    end.state = RealtimeResponseEnd::State::kIncomplete;
    end.reason = "max_output_tokens";
    Q4T_CHECK(fixture.output.Finish(end).ok());
    Q4T_CHECK(fixture.output.TakeMcpCalls().empty());
    Q4T_CHECK(fixture.Count("response.mcp_call.failed") == 1 &&
              fixture.Count("response.output_item.done") == 1);
    RealtimeConversationSnapshot snapshot;
    Q4T_CHECK(fixture.history.Snapshot(nullptr, &snapshot));
    Q4T_CHECK(snapshot.items.array[0].Find("error") != nullptr &&
              !snapshot.items.array[0].Has("status"));
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_cancelled_and_failed_generation_never_dispatch) {
  for (const bool cancelled : {false, true}) {
    Fixture fixture;
    Q4T_CHECK(fixture.output.Begin().ok());
    Q4T_CHECK(fixture.output.Append(Direct(Binding().model_name, "{}")).ok());
    auto end = Failed();
    if (cancelled) {
      end.state = RealtimeResponseEnd::State::kCancelled;
      end.reason = "client_cancelled";
    }
    Q4T_CHECK(fixture.output.Finish(end).ok() &&
              fixture.output.TakeMcpCalls().empty());
    Q4T_CHECK(fixture.Count("response.mcp_call.failed") == 1 &&
              fixture.Count("response.done") == 1);
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_malformed_complete_arguments_fail_before_handoff) {
  for (const auto* args : {"{", "[]", "{\"x\":1,\"x\":2}"}) {
    Fixture fixture;
    Q4T_CHECK(fixture.output.Begin().ok() &&
              fixture.output.Append(Direct(Binding().model_name, args)).ok());
    const auto message = Finite(Binding().model_name, args);
    Q4T_CHECK(!fixture.output.Finish(Complete(message)).ok());
    Q4T_CHECK(fixture.Count("response.done") == 0 &&
              fixture.output.TakeMcpCalls().empty());
    Q4T_CHECK(fixture.output.Finish(Failed()).ok() &&
              fixture.output.TakeMcpCalls().empty());
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_finite_message_binds_original_private_identity) {
  for (const bool wrong_name : {false, true}) {
    Fixture fixture;
    Q4T_CHECK(fixture.output.Begin().ok() &&
              fixture.output.Append(Direct(Binding().model_name, "{}")).ok());
    auto message = Finite(Binding().model_name, "{}");
    auto& call = Field(message, "tool_calls").array[0];
    if (wrong_name)
      Set(&Field(call, "function"), "name", String("read"));
    else
      Set(&call, "id", String("different_model_call"));
    Q4T_CHECK(!fixture.output.Finish(Complete(message)).ok() &&
              fixture.output.TakeMcpCalls().empty());
    Q4T_CHECK(fixture.output.Finish(Failed()).ok());
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_fragment_cannot_switch_target_or_call_identity) {
  for (const bool wrong_name : {false, true}) {
    Fixture fixture({Binding("first"), Binding("second")});
    Q4T_CHECK(
        fixture.output.Begin().ok() &&
        fixture.output.Append(Direct(Binding("first").model_name, "{")).ok());
    auto delta = Direct(Binding("first").model_name, "}", false);
    if (wrong_name)
      delta.call.name = Binding("second").model_name;
    else
      delta.call.id = "substitution";
    Q4T_CHECK(!fixture.output.Append(delta).ok() &&
              fixture.output.TakeMcpCalls().empty());
    Q4T_CHECK(fixture.output.Finish(Failed()).ok());
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_deleted_call_never_enters_execution_handoff) {
  Fixture fixture;
  Q4T_CHECK(fixture.output.Begin().ok());
  Json message;
  Q4T_CHECK(fixture.Feed(Tool(Binding().model_name), &message));
  Q4T_CHECK(fixture.tickets.size() == 1);
  Q4T_CHECK(fixture.history.Erase(fixture.tickets.begin()->second));
  fixture.drop_items = true;
  Q4T_CHECK(fixture.output.Finish(Complete(message)).ok() &&
            fixture.output.TakeMcpCalls().empty());
  Q4T_CHECK(fixture.Count("response.done") == 1 &&
            fixture.Count("conversation.item.done") == 0);
  Q4T_CHECK(fixture.history.Items() == 0);
  return true;
}

Q4T_TEST(realtime_mcp_output_failed_terminal_sink_never_hands_off_or_replays) {
  for (const size_t offset : {1, 2}) {
    Fixture fixture;
    Q4T_CHECK(fixture.output.Begin().ok());
    Json message;
    Q4T_CHECK(fixture.Feed(Tool(Binding().model_name), &message));
    fixture.fail_at = fixture.attempts + offset;
    Q4T_CHECK(!fixture.output.Finish(Complete(message)).ok());
    Q4T_CHECK(fixture.output.TakeMcpCalls().empty());
    const auto attempts = fixture.attempts;
    Q4T_CHECK(!fixture.output.Finish(Failed()).ok() &&
              fixture.attempts == attempts);
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_invalid_bindings_rejected_before_any_event) {
  for (int test = 0; test < 3; ++test) {
    auto bindings = std::vector<RealtimeMcpOutputBinding>{Binding()};
    if (test == 0) bindings.push_back(Binding());
    if (test == 1) bindings[0].server_label.clear();
    if (test == 2) bindings[0].model_name = "invalid name";
    Fixture fixture(bindings);
    Q4T_CHECK(!fixture.output.Begin().ok() && fixture.events.empty());
    Q4T_CHECK(fixture.output.TakeMcpCalls().empty());
  }
  // std::string copy may discard spare capacity. Move the actual oversized
  // allocation into the owner, rather than testing a smaller copied string.
  for (int test = 0; test < 4; ++test) {
    auto options = Fixture::Options({Binding()}, true);
    if (test == 3) {
      options.mcp_bindings.reserve(256);
      Q4T_CHECK(options.mcp_bindings.capacity() > 128);
    } else {
      auto& binding = options.mcp_bindings[0];
      auto* field = &binding.name;
      if (test == 1) field = &binding.server_label;
      if (test == 2) field = &binding.model_name;
      field->reserve(4096);
      Q4T_CHECK(field->capacity() > 1024);
    }
    size_t callbacks = 0;
    RealtimeResponseOutput output(
        std::move(options),
        [&](const Json&) {
          ++callbacks;
          return ObjectReply{};
        },
        [&](const Json&, bool, bool) {
          ++callbacks;
          return ObjectReply{};
        },
        [](std::string_view) { return "unused"; });
    Q4T_CHECK(!output.Begin().ok() && callbacks == 0);
    Q4T_CHECK(output.TakeMcpCalls().empty());
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_late_binding_preserves_capacity_guard) {
  for (const bool vector_capacity : {false, true}) {
    Fixture fixture(std::vector<RealtimeMcpOutputBinding>{});
    Q4T_CHECK(fixture.output.Begin().ok());
    auto bindings = std::vector<RealtimeMcpOutputBinding>{Binding()};
    if (vector_capacity)
      bindings.reserve(256);
    else
      bindings[0].name.reserve(4096);
    const auto attempts = fixture.attempts;
    const auto error = fixture.output.BindMcp(std::move(bindings));
    Q4T_CHECK(!error.ok() && error.code == "realtime_mcp_binding_invalid");
    Q4T_CHECK(fixture.attempts == attempts);
    Q4T_CHECK(fixture.output.TakeMcpCalls().empty());
  }
  return true;
}

Q4T_TEST(realtime_mcp_output_late_binding_accepts_limit_and_preserves_mapping) {
  Fixture fixture(std::vector<RealtimeMcpOutputBinding>{});
  Q4T_CHECK(fixture.output.Begin().ok());
  std::vector<RealtimeMcpOutputBinding> bindings;
  bindings.reserve(128);
  for (size_t i = 0; i < 128; ++i)
    bindings.push_back(Binding("project", "read_" + std::to_string(i)));
  // The parser fixture adds its own local tool; select the exercised alias.
  fixture.bindings = {bindings.back()};
  Q4T_CHECK(fixture.output.BindMcp(std::move(bindings)).ok());
  Json message;
  Q4T_CHECK(fixture.Feed(Tool(fixture.bindings.back().model_name), &message));
  Q4T_CHECK(fixture.output.Finish(Complete(message)).ok());
  const auto calls = fixture.output.TakeMcpCalls();
  Q4T_CHECK(calls.size() == 1);
  Q4T_CHECK(calls[0].item.GetString("name") == "read_127");
  Q4T_CHECK(calls[0].item.GetString("server_label") == "project");
  return true;
}

Q4T_TEST(realtime_mcp_output_handoff_once_only_after_done_keeps_tool_pending) {
  Fixture fixture({Binding()}, false);
  Q4T_CHECK(fixture.output.Begin().ok());
  Json message;
  Q4T_CHECK(fixture.Feed(Tool(Binding().model_name), &message));
  Q4T_CHECK(fixture.output.TakeMcpCalls().empty());
  Q4T_CHECK(fixture.output.Finish(Complete(message)).ok());
  const auto calls = fixture.output.TakeMcpCalls();
  Q4T_CHECK(calls.size() == 1 && fixture.output.TakeMcpCalls().empty());
  Q4T_CHECK(fixture.events.back().GetString("type") == "response.done");
  Q4T_CHECK(!calls[0].item.Has("status") && !calls[0].item.Has("output") &&
            !calls[0].item.Has("error"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(fixture.history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  const auto input = Array({Object({{"type", String("item_reference")},
                                    {"id", *calls[0].item.Find("id")}})});
  Q4T_CHECK(!fixture.history.Snapshot(&input, &snapshot));
  Q4T_CHECK(fixture.Count("conversation.item.done") == 0 &&
            fixture.Count("response.output_item.done") == 0);
  return true;
}
