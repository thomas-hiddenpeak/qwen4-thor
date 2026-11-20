#include "q4t/server/realtime_trace.h"

#include <cmath>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("bad fixture JSON");
  return value;
}
RealtimeTraceTime Time(int64_t ns = 0) {
  return {1795185600000 + ns / 1000000, ns};
}
RealtimeTraceConfiguration Config(const std::string& text = "\"auto\"") {
  const auto value = Parse(text);
  RealtimeTraceConfiguration result;
  if (!ParseRealtimeTraceConfiguration(&value, &result).ok())
    throw std::runtime_error("bad fixture configuration");
  return result;
}
RealtimeTraceCollector Trace(size_t maximum = 256) {
  return {"sess_trace", "conv_trace", Config(), Time(), maximum};
}
Json Span(const Json& trace, std::string_view entity) {
  const auto* spans = trace.GetArray("spans");
  if (spans)
    for (const auto& span : spans->array)
      if (span.GetString("entity_id") == entity) return span;
  return Json();
}
bool HasSecret(const Json& value) {
  if (value.str.find("PRIVATE_PAYLOAD") != value.str.npos) return true;
  for (const auto& child : value.array)
    if (HasSecret(child)) return true;
  for (const auto& [key, child] : value.object)
    if (key.find("PRIVATE_PAYLOAD") != key.npos || HasSecret(child))
      return true;
  return false;
}
bool Observe(RealtimeTraceCollector& trace, const std::string& text,
             int64_t ns = 0) {
  return ObserveRealtimeTraceEvent(&trace, Parse(text), Time(ns));
}
}  // namespace

Q4T_TEST(realtime_trace_configuration_preserves_arbitrary_metadata) {
  auto value = Config(R"({"group_id":"project","workflow_name":"资料查询",
    "metadata":[1,true,null,{"purpose":"review"}]})");
  Q4T_CHECK(value.enabled && value.group_id == "project");
  Q4T_CHECK(value.workflow_name == "资料查询" && value.metadata.IsArray());
  Q4T_CHECK(value.metadata.array.size() == 4);
  Q4T_CHECK(Config().enabled && Config().workflow_name == "Realtime session");
  Q4T_CHECK(ParseRealtimeTraceConfiguration(nullptr, &value).ok());
  Q4T_CHECK(!value.enabled && value.group_id.empty());
  Q4T_CHECK(!Config("null").enabled);
  return true;
}

Q4T_TEST(realtime_trace_invalid_configuration_is_atomic) {
  auto current = Config();
  const auto before = current;
  for (const auto* text : {"true", "[]", "\"enabled\"",
                           "{\"workflow_name\":null}", "{\"extra\":1}"}) {
    const auto bad = Parse(text);
    Q4T_CHECK(!ParseRealtimeTraceConfiguration(&bad, &current).ok());
    Q4T_CHECK(SameRealtimeTraceConfiguration(before, current));
  }
  auto oversized = Parse("\"auto\"");
  oversized.str.reserve(1 << 20);
  const auto reply = ParseRealtimeTraceConfiguration(&oversized, &current);
  Q4T_CHECK(reply.http_status == 413 &&
            reply.code == "realtime_tracing_capacity");
  Q4T_CHECK(SameRealtimeTraceConfiguration(before, current));
  return true;
}

Q4T_TEST(realtime_trace_configuration_equality_respects_metadata) {
  const auto left = Config(R"({"metadata":{"a":1,"b":[2,3]}})");
  const auto reordered = Config(R"({"metadata":{"b":[2,3],"a":1}})");
  const auto changed = Config(R"({"metadata":{"b":[3,2],"a":1}})");
  Q4T_CHECK(SameRealtimeTraceConfiguration(left, reordered));
  Q4T_CHECK(!SameRealtimeTraceConfiguration(left, changed));
  Q4T_CHECK(!SameRealtimeTraceConfiguration(Config(), Config("null")));
  return true;
}

Q4T_TEST(realtime_trace_response_uses_monotonic_time_and_numeric_usage) {
  auto trace = Trace();
  Q4T_CHECK(trace.Begin("response", "resp_1", {}, Time()));
  const auto usage =
      Parse(R"({"input_tokens":4,"output_tokens":2,"total_tokens":6,
    "credentials":"PRIVATE_PAYLOAD","output_token_details":{"text_tokens":2,
    "audio_tokens":0,"secret":"PRIVATE_PAYLOAD"}})");
  auto end = Time(125000);
  end.unix_ms -= 5000;
  Q4T_CHECK(trace.End("response", "resp_1", "completed", end, &usage));
  const auto snapshot = trace.Snapshot();
  const auto response = Span(snapshot, "resp_1");
  Q4T_CHECK(std::abs(response.GetNumber("duration_ms") - 0.125) < 1e-12);
  Q4T_CHECK(response.Find("usage")->GetInt("total_tokens") == 6);
  Q4T_CHECK(!HasSecret(snapshot));
  return true;
}

Q4T_TEST(realtime_trace_capacity_does_not_drop_owned_terminal) {
  auto trace = Trace(2);
  Q4T_CHECK(trace.Begin("response", "resp_1", {}, Time()));
  Q4T_CHECK(trace.Begin("mcp_call", "call_1", "resp_1", Time(1)));
  Q4T_CHECK(!trace.Begin("mcp_import", "catalog_1", {}, Time(2)));
  Q4T_CHECK(trace.End("response", "resp_1", "completed", Time(3)));
  Q4T_CHECK(trace.End("mcp_call", "call_1", "failed", Time(4)));
  const auto snapshot = trace.Snapshot();
  Q4T_CHECK(snapshot.GetInt("dropped_span_events") == 1);
  Q4T_CHECK(snapshot.GetArray("spans")->array.size() == 2);
  Q4T_CHECK(Span(snapshot, "call_1").GetString("parent_span_id") == "span_1");
  Q4T_CHECK(Span(snapshot, "call_1").GetString("status") == "failed");
  return true;
}

Q4T_TEST(realtime_trace_late_enable_never_invents_start_or_duration) {
  auto trace = Trace();
  Q4T_CHECK(
      trace.End("response", "resp_already_running", "cancelled", Time(10)));
  const auto response = Span(trace.Snapshot(), "resp_already_running");
  Q4T_CHECK(response.GetBool("partial"));
  Q4T_CHECK(response.Find("started_at_ms")->IsNull());
  Q4T_CHECK(response.Find("duration_ms")->IsNull());
  return true;
}

Q4T_TEST(realtime_trace_terminal_is_immutable_and_disconnect_interrupts_work) {
  auto trace = Trace();
  Q4T_CHECK(trace.Begin("response", "finished", {}, Time()));
  Q4T_CHECK(trace.End("response", "finished", "completed", Time(1)));
  Q4T_CHECK(!trace.End("response", "finished", "failed", Time(100)));
  Q4T_CHECK(trace.Begin("mcp_call", "active", "finished", Time(2)));
  trace.Close("client_disconnected", Time(3));
  const auto revision = trace.Revision();
  Q4T_CHECK(!trace.Begin("response", "revived", {}, Time(4)));
  Q4T_CHECK(!trace.End("mcp_call", "active", "completed", Time(5)));
  trace.EventSeen();
  Q4T_CHECK(trace.Revision() == revision);
  Q4T_CHECK(trace.Snapshot().GetInt("events_seen") == 0);
  Q4T_CHECK(Span(trace.Snapshot(), "finished").GetString("status") ==
            "completed");
  Q4T_CHECK(Span(trace.Snapshot(), "active").GetString("status") ==
            "interrupted");
  return true;
}

Q4T_TEST(realtime_trace_mcp_response_done_is_not_tool_completion) {
  auto trace = Trace();
  Q4T_CHECK(
      Observe(trace, R"({"type":"response.created","response":{"id":"r"}})"));
  Q4T_CHECK(Observe(trace,
                    R"({"type":"response.output_item.added","response_id":"r",
    "item":{"type":"mcp_call","id":"m","arguments":"PRIVATE_PAYLOAD"}})",
                    1));
  Q4T_CHECK(Observe(trace, R"({"type":"response.done","response":{"id":"r",
    "status":"completed","usage":{"total_tokens":6}}})",
                    2));
  Q4T_CHECK(Span(trace.Snapshot(), "m").GetString("status") == "in_progress");
  Q4T_CHECK(Observe(trace, R"({"type":"conversation.item.done",
    "item":{"type":"mcp_approval_request","id":"a","arguments":"PRIVATE_PAYLOAD"}})",
                    3));
  Q4T_CHECK(Observe(trace, R"({"type":"conversation.item.done",
    "item":{"type":"mcp_approval_response","approval_request_id":"a","approve":true}})",
                    4));
  Q4T_CHECK(Observe(
      trace, R"({"type":"response.mcp_call.in_progress","item_id":"m"})", 5));
  Q4T_CHECK(Observe(trace,
                    R"({"type":"response.output_item.done","response_id":"r",
    "item":{"type":"mcp_call","id":"m","approval_request_id":"a","output":"PRIVATE_PAYLOAD"}})",
                    6));
  const auto snapshot = trace.Snapshot();
  Q4T_CHECK(Span(snapshot, "m").GetString("status") == "completed");
  Q4T_CHECK(Span(snapshot, "m").GetString("approval_request_id") == "a");
  Q4T_CHECK(Span(snapshot, "a").GetString("status") == "completed");
  Q4T_CHECK(!HasSecret(snapshot));
  return true;
}

Q4T_TEST(realtime_trace_failed_mcp_retains_approval_link_without_reopening) {
  auto trace = Trace();
  Q4T_CHECK(trace.Begin("mcp_call", "m", {}, Time()));
  Q4T_CHECK(Observe(trace,
                    R"({"type":"response.mcp_call.failed","item_id":"m"})", 1));
  Q4T_CHECK(Observe(trace, R"({"type":"response.output_item.done","item":{
    "type":"mcp_call","id":"m","approval_request_id":"a",
    "error":{"message":"PRIVATE_PAYLOAD"}}})",
                    2));
  const auto span = Span(trace.Snapshot(), "m");
  Q4T_CHECK(span.GetString("approval_request_id") == "a");
  Q4T_CHECK(span.GetString("status") == "failed");
  Q4T_CHECK(!HasSecret(span));
  return true;
}

Q4T_TEST(realtime_trace_delta_payloads_never_become_retained_records) {
  auto trace = Trace();
  for (const auto* type :
       {"response.output_audio.delta", "response.output_text.delta",
        "response.mcp_call_arguments.delta"}) {
    auto event = Object(
        {{"type", String(type)},
         {"delta", String(std::string(1 << 20, 'x') + "PRIVATE_PAYLOAD")}});
    Q4T_CHECK(!ObserveRealtimeTraceEvent(&trace, event, Time()));
  }
  const auto snapshot = trace.Snapshot();
  Q4T_CHECK(snapshot.GetArray("spans")->array.empty());
  Q4T_CHECK(snapshot.GetInt("events_seen") == 3);
  Q4T_CHECK(!HasSecret(snapshot));
  return true;
}

Q4T_TEST(realtime_trace_audio_parts_keep_independent_terminals) {
  auto trace = Trace();
  Q4T_CHECK(Observe(trace, R"({"type":"conversation.item.added","item":{
    "id":"i","role":"user","content":[{"type":"input_audio","audio":"PRIVATE_PAYLOAD"},
    {"type":"input_audio","audio":"PRIVATE_PAYLOAD"}]}})"));
  Q4T_CHECK(Observe(
      trace, R"({"type":"conversation.item.input_audio_transcription.completed",
    "item_id":"i","content_index":0,"transcript":"PRIVATE_PAYLOAD"})",
      1));
  Q4T_CHECK(Observe(
      trace, R"({"type":"conversation.item.input_audio_transcription.failed",
    "item_id":"i","content_index":1,"error":{"message":"PRIVATE_PAYLOAD"}})",
      2));
  Q4T_CHECK(Span(trace.Snapshot(), "i_part_0").GetString("status") ==
            "completed");
  Q4T_CHECK(Span(trace.Snapshot(), "i_part_1").GetString("status") == "failed");
  Q4T_CHECK(!HasSecret(trace.Snapshot()));
  return true;
}

Q4T_TEST(realtime_trace_function_arguments_do_not_claim_external_execution) {
  auto trace = Trace();
  Q4T_CHECK(
      Observe(trace, R"({"type":"response.output_item.added","response_id":"r",
    "item":{"type":"function_call","id":"f","arguments":"PRIVATE_PAYLOAD"}})"));
  Q4T_CHECK(Observe(trace,
                    R"({"type":"response.output_item.done","response_id":"r",
    "item":{"type":"function_call","id":"f","status":"completed"}})",
                    1));
  const auto span = Span(trace.Snapshot(), "f");
  Q4T_CHECK(span.GetString("kind") == "function_arguments");
  Q4T_CHECK(span.Find("dispatched_at_ms")->IsNull());
  Q4T_CHECK(!HasSecret(span));
  return true;
}
