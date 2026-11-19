#include "q4t/server/realtime_conversation.h"

#include "q4t/server/output_schema.h"
#include "q4t/test.h"
#include "../src/server/responses_json.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::responses_detail;
using Ticket = RealtimeItemTicket;
Json Message(std::string text = "Hello", std::string role = "user") {
  const auto type = role == "assistant" ? "output_text" : "input_text";
  return Object(
      {{"type", String("message")},
       {"role", String(role)},
       {"content",
        Array({Object({{"type", String(type)}, {"text", String(text)}})})}});
}
Json Reference(std::string id) {
  return Object({{"type", String("item_reference")}, {"id", String(id)}});
}
}  // namespace

Q4T_TEST(realtime_conversation_orders_root_middle_and_append) {
  RealtimeConversation history("sess_a");
  Ticket a, b, c;
  Q4T_CHECK(history.Create(Message("last"), "a", {}, &a));
  Q4T_CHECK(history.Create(Message("first"), "b", "root", &b));
  Q4T_CHECK(history.Create(Message("middle"), "c", "b", &c));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.owner == "sess_a" && snapshot.revision == 3);
  const auto& items = snapshot.items.array;
  Q4T_CHECK(items.size() == 3 && items[0].GetString("id") == "b" &&
            items[1].GetString("id") == "c" &&
            items[2].GetString("id") == "a" && history.LastId() == "a");
  Q4T_CHECK(items[0].GetString("object") == "realtime.item");
  Q4T_CHECK(items[0].GetString("status") == "completed");
  Q4T_CHECK(history.Erase(a) && history.LastId() == "c");
  return true;
}

Q4T_TEST(realtime_conversation_failed_insert_preserves_all_state) {
  RealtimeConversation history("sess_a");
  Ticket item, untouched{"sentinel", "unchanged", 77};
  Q4T_CHECK(history.Create(Message(), "a", {}, &item));
  const auto bytes = history.ChargedBytes(), revision = history.Revision();
  Q4T_CHECK(!history.Create(Message(), "b", "absent", &untouched));
  Q4T_CHECK(!history.Create(Message(), "a", {}, &untouched));
  Q4T_CHECK(!history.Create(Message(), "root", {}, &untouched));
  auto bad = Message();
  Set(&bad, "extra", Boolean(true));
  Q4T_CHECK(!history.Create(bad, "b", {}, &untouched));
  Q4T_CHECK(history.Items() == 1 && history.ChargedBytes() == bytes &&
            history.Revision() == revision && untouched.owner == "sentinel" &&
            untouched.id == "unchanged" && untouched.generation == 77);
  return true;
}

Q4T_TEST(realtime_conversation_tickets_cannot_cross_session) {
  RealtimeConversation a("sess_a"), b("sess_b");
  Ticket ta, tb;
  Q4T_CHECK(a.Create(Message(), "same", {}, &ta));
  Q4T_CHECK(b.Create(Message("other"), "same", {}, &tb));
  Json result = String("unchanged");
  Q4T_CHECK(!a.Get(tb, &result) && result.str == "unchanged");
  Q4T_CHECK(!a.Erase(tb) && !b.Erase(ta));
  Q4T_CHECK(!a.Update(tb, Message(), true));
  Q4T_CHECK(a.Items() == 1 && b.Items() == 1);
  return true;
}

Q4T_TEST(realtime_conversation_deleted_ticket_cannot_resurrect_reused_id) {
  RealtimeConversation history("sess_a");
  Ticket old, current;
  Q4T_CHECK(
      history.BeginOutput(Message("", "assistant"), "same", true, 8192, &old));
  Json old_item;
  Q4T_CHECK(history.Get(old, &old_item));
  Q4T_CHECK(history.Erase(old) && history.ChargedBytes() == 0);
  Q4T_CHECK(history.BeginOutput(Message("fresh", "assistant"), "same", true,
                                8192, &current));
  Q4T_CHECK(current.generation != old.generation);
  Set(&old_item, "status", String("completed"));
  Q4T_CHECK(!history.Update(old, old_item, true));
  Q4T_CHECK(!history.Erase(old) && history.Items() == 1);
  return true;
}

Q4T_TEST(realtime_conversation_snapshot_owns_original_context) {
  RealtimeConversation history("sess_a");
  Ticket a, b;
  Q4T_CHECK(history.Create(Message("original"), "a", {}, &a));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  const auto original = OutputJson(snapshot.items);
  Q4T_CHECK(history.Erase(a));
  Q4T_CHECK(history.Create(Message("replacement"), "a", {}, &b));
  Q4T_CHECK(OutputJson(snapshot.items) == original && snapshot.revision == 1);
  return true;
}

Q4T_TEST(realtime_conversation_out_of_band_is_retrievable_not_default) {
  RealtimeConversation history("sess_a");
  Ticket a, hidden, rejected;
  Q4T_CHECK(history.Create(Message(), "a", {}, &a));
  Q4T_CHECK(history.BeginOutput(Message("analysis", "assistant"), "hidden",
                                false, 8192, &hidden));
  Json item;
  Q4T_CHECK(history.Get(hidden, &item));
  Set(&item, "status", String("completed"));
  Q4T_CHECK(history.Update(hidden, item, true));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1 && history.LastId() == "a");
  auto input = Array({Reference("hidden")});
  Q4T_CHECK(history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.items.array[0].GetString("id") == "hidden");
  Q4T_CHECK(!history.Create(Message(), "b", "hidden", &rejected));
  return true;
}

Q4T_TEST(realtime_conversation_explicit_empty_and_reference_contexts) {
  RealtimeConversation history("sess_a");
  Ticket a;
  Q4T_CHECK(history.Create(Message("history"), "a", {}, &a));
  RealtimeConversationSnapshot snapshot;
  auto input = Array();
  Q4T_CHECK(history.Snapshot(&input, &snapshot) &&
            snapshot.items.array.empty());
  input = Array({Reference("a"), Message("direct")});
  Q4T_CHECK(history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 2 &&
            snapshot.items.array[0].GetString("id") == "a" &&
            !snapshot.items.array[1].Has("id") && history.Items() == 1);
  const auto original = OutputJson(snapshot.items);
  input = Array({Reference("foreign_or_deleted")});
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  Q4T_CHECK(OutputJson(snapshot.items) == original);
  input = Array({Object({{"id", String("a")}})});
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  Q4T_CHECK(OutputJson(snapshot.items) == original);
  return true;
}

Q4T_TEST(realtime_conversation_partial_model_output_does_not_enter_prompt) {
  RealtimeConversation history("sess_a");
  Ticket user, pending;
  Q4T_CHECK(history.Create(Message(), "user", {}, &user));
  Q4T_CHECK(history.BeginOutput(Message("partial", "assistant"), "pending",
                                true, 8192, &pending));
  RealtimeConversationSnapshot snapshot{"unchanged", 44, String("sentinel")};
  Q4T_CHECK(!history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.owner == "unchanged" && snapshot.revision == 44);
  auto input = Array({Reference("pending")});
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  input = Array({Reference("user")});
  Q4T_CHECK(history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_conversation_incomplete_server_tool_is_retained_not_reexecuted) {
  RealtimeConversation history("sess_a");
  Ticket user, tool;
  Q4T_CHECK(history.Create(Message(), "user", {}, &user));
  auto call = Object({{"type", String("function_call")},
                      {"name", String("lookup")},
                      {"call_id", String("call_a")},
                      {"arguments", String("{")}});
  Q4T_CHECK(history.BeginOutput(call, "tool", true, 8192, &tool));
  Q4T_CHECK(history.Get(tool, &call));
  Set(&call, "status", String("incomplete"));
  Q4T_CHECK(history.Update(tool, call, true));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1 && history.Items() == 2);
  Json retained;
  Q4T_CHECK(history.Get(tool, &retained) &&
            retained.GetString("arguments") == "{");
  auto explicit_input = Array({Reference("tool")});
  Q4T_CHECK(!history.Snapshot(&explicit_input, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_conversation_client_status_cannot_hide_call_or_incomplete_text) {
  RealtimeConversation history("sess_a");
  Ticket tool, text;
  const auto call = Object({{"type", String("function_call")},
                            {"name", String("lookup")},
                            {"call_id", String("call_a")},
                            {"status", String("incomplete")},
                            {"arguments", String("{}")}});
  Q4T_CHECK(history.Create(call, "tool", {}, &tool));
  auto message = Message("partial text", "assistant");
  Q4T_CHECK(history.BeginOutput(message, "text", true, 8192, &text));
  Q4T_CHECK(history.Get(text, &message));
  Set(&message, "status", String("incomplete"));
  Q4T_CHECK(history.Update(text, message, true));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 2);
  auto explicit_input = Array({Reference("tool")});
  Q4T_CHECK(history.Snapshot(&explicit_input, &snapshot));
  return true;
}

Q4T_TEST(realtime_conversation_context_cut_persists_but_history_is_retrievable) {
  RealtimeConversation history("sess_a");
  Ticket old, current, next;
  Q4T_CHECK(history.Create(Message("old"), "old", {}, &old));
  Q4T_CHECK(history.Create(Message("current"), "current", {}, &current));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  const size_t keep[] = {1};
  Q4T_CHECK(history.TruncateDefault(snapshot, keep));
  Q4T_CHECK(history.Create(Message("next"), "next", {}, &next));
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 2 &&
            snapshot.items.array[0].GetString("id") == "current");
  Json retained;
  Q4T_CHECK(history.Get(old, &retained));
  auto reference = Array({Reference("old")});
  Q4T_CHECK(history.Snapshot(&reference, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1 && !snapshot.default_context);
  Q4T_CHECK(!history.TruncateDefault(snapshot, {}));
  return true;
}

Q4T_TEST(realtime_conversation_context_cut_never_hides_recreated_generation) {
  RealtimeConversation history("sess_a");
  Ticket old, current, replacement;
  Q4T_CHECK(history.Create(Message("old"), "same", {}, &old));
  Q4T_CHECK(history.Create(Message("current"), "current", {}, &current));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(history.Erase(old));
  Q4T_CHECK(history.Create(Message("old"), "same", {}, &replacement));
  const size_t keep[] = {1};
  Q4T_CHECK(history.TruncateDefault(snapshot, keep));
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 2 &&
            snapshot.items.array[1].GetString("id") == "same");
  return true;
}

Q4T_TEST(realtime_conversation_context_cut_invalid_selection_is_atomic) {
  RealtimeConversation history("sess_a");
  Ticket a, b;
  Q4T_CHECK(history.Create(Message(), "a", {}, &a));
  Q4T_CHECK(history.Create(Message(), "b", {}, &b));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  const auto revision = history.Revision();
  const size_t duplicates[] = {1, 1}, outside[] = {2};
  Q4T_CHECK(!history.TruncateDefault(snapshot, duplicates));
  Q4T_CHECK(!history.TruncateDefault(snapshot, outside));
  snapshot.tickets[0].owner = "other_session";
  Q4T_CHECK(!history.TruncateDefault(snapshot, {}));
  Q4T_CHECK(history.Revision() == revision);
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.size() == 2);
  return true;
}

Q4T_TEST(realtime_conversation_reservation_and_terminal_release) {
  RealtimeConversation history("sess_a", 3, 32768);
  Ticket item;
  Q4T_CHECK(
      history.BeginOutput(Message("", "assistant"), "a", true, 8192, &item));
  Q4T_CHECK(history.ChargedBytes() == 8192);
  Json partial;
  Q4T_CHECK(history.Get(item, &partial));
  auto next = Message("More output", "assistant");
  Set(&partial, "content", *next.Find("content"));
  Q4T_CHECK(history.Update(item, partial, false));
  Q4T_CHECK(history.ChargedBytes() == 8192);
  Set(&partial, "status", String("completed"));
  Q4T_CHECK(history.Update(item, partial, true));
  Q4T_CHECK(history.ChargedBytes() < 8192);
  const auto bytes = history.ChargedBytes(), revision = history.Revision();
  Q4T_CHECK(!history.Update(item, partial, true));
  Q4T_CHECK(!history.Update(item, partial, false));
  Q4T_CHECK(history.ChargedBytes() == bytes && history.Revision() == revision);
  return true;
}

Q4T_TEST(realtime_conversation_output_updates_preserve_identity_and_budget) {
  RealtimeConversation history("sess_a");
  Ticket item;
  Json call = Object({{"type", String("function_call")},
                      {"name", String("lookup")},
                      {"call_id", String("call_a")},
                      {"arguments", String("{")}});
  Q4T_CHECK(history.BeginOutput(call, "item_a", true, 8192, &item));
  Json original;
  Q4T_CHECK(history.Get(item, &original));
  for (const auto* field : {"id", "name", "call_id"}) {
    auto changed = original;
    Set(&changed, field, String("different"));
    Q4T_CHECK(!history.Update(item, changed, false));
  }
  auto oversized = original;
  Set(&oversized, "arguments", String(std::string(8192, 'x')));
  Q4T_CHECK(!history.Update(item, oversized, false));
  auto wrong_status = original;
  Set(&wrong_status, "status", String("completed"));
  Q4T_CHECK(!history.Update(item, wrong_status, false));
  Q4T_CHECK(!history.Update(item, original, true));
  Json actual;
  Q4T_CHECK(history.Get(item, &actual));
  Q4T_CHECK(OutputJson(actual) == OutputJson(original));
  Set(&original, "status", String("incomplete"));
  Q4T_CHECK(history.Update(item, original, true));
  return true;
}

Q4T_TEST(realtime_conversation_capacity_failures_do_not_evict_history) {
  RealtimeConversation history("sess_a", 2, 20000);
  Ticket a, b, rejected;
  Q4T_CHECK(
      history.BeginOutput(Message("", "assistant"), "a", true, 16000, &a));
  Q4T_CHECK(!history.BeginOutput(Message("", "assistant"), "b", true, 5000,
                                 &rejected));
  Q4T_CHECK(
      !history.Create(Message(std::string(5000, 'x')), "b", {}, &rejected));
  Q4T_CHECK(history.ChargedBytes() == 16000 && history.Items() == 1);
  Q4T_CHECK(history.Create(Message(), "b", {}, &b));
  Q4T_CHECK(!history.Create(Message(), "c", {}, &rejected));
  Q4T_CHECK(history.Items() == 2 && history.LastId() == "b");
  return true;
}

Q4T_TEST(realtime_conversation_snapshot_duplicate_refs_respect_total_budget) {
  RealtimeConversation history("sess_a", 8, 10000);
  Ticket item;
  Q4T_CHECK(history.Create(Message(std::string(4000, 'x')), "a", {}, &item));
  auto input = Array({Reference("a"), Reference("a"), Reference("a")});
  RealtimeConversationSnapshot snapshot{"unchanged", 3, String("sentinel")};
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.owner == "unchanged" && snapshot.items.str == "sentinel");
  Q4T_CHECK(history.Items() == 1);
  return true;
}

Q4T_TEST(realtime_conversation_client_status_does_not_hide_history) {
  RealtimeConversation history("sess_a");
  Ticket call, result;
  auto input = Object({{"type", String("function_call")},
                       {"name", String("lookup")},
                       {"arguments", String("{")},
                       {"status", String("incomplete")}});
  Q4T_CHECK(history.Create(input, "call_a", {}, &call));
  auto output = Object({{"type", String("function_call_output")},
                        {"call_id", String("call_a")},
                        {"output", String("")}});
  Q4T_CHECK(history.Create(output, "result_a", {}, &result));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 2);
  Q4T_CHECK(snapshot.items.array[0].GetString("arguments") == "{");
  Q4T_CHECK(snapshot.items.array[0].GetString("call_id") == "call_a");
  Q4T_CHECK(snapshot.items.array[1].GetString("output") == "");
  // Shape/history acceptance is not authorization to execute a partial call.
  return true;
}

Q4T_TEST(realtime_conversation_mcp_items_preserve_their_actual_shape) {
  RealtimeConversation history("sess_a");
  Ticket item;
  const auto raw = Object({{"type", String("mcp_approval_response")},
                           {"id", String("approval_a")},
                           {"approval_request_id", String("request_a")},
                           {"approve", Boolean(false)}});
  Q4T_CHECK(history.Create(raw, "unused", {}, &item));
  Json result;
  Q4T_CHECK(history.Get(item, &result));
  Q4T_CHECK(OutputJson(raw) == OutputJson(result));
  Q4T_CHECK(!result.Has("status") && !result.Has("object"));
  return true;
}

Q4T_TEST(realtime_conversation_retained_capacity_and_depth_are_bounded) {
  RealtimeConversation history("sess_a", 2, 10000);
  Ticket item;
  auto raw = Message();
  raw.object.reserve(1000);
  Q4T_CHECK(!history.Create(raw, "a", {}, &item));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 0);
  RealtimeConversation bad_owner("bad owner");
  RealtimeConversation bad_items("sess_a", 129);
  RealtimeConversation bad_bytes("sess_a", 2,
                                 RealtimeConversation::kMaximumBytes + 1);
  Q4T_CHECK(!bad_owner.Valid() && !bad_items.Valid() && !bad_bytes.Valid());
  Q4T_CHECK(!bad_owner.Create(Message(), "a", {}, &item));
  return true;
}

Q4T_TEST(realtime_conversation_reference_duplicate_fields_fail_atomically) {
  RealtimeConversation history("sess_a");
  Ticket item;
  Q4T_CHECK(history.Create(Message(), "a", {}, &item));
  auto reference = Reference("a");
  reference.object.push_back({"id", String("a")});
  auto input = Array({reference});
  RealtimeConversationSnapshot snapshot{"unchanged", 7, String("sentinel")};
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.owner == "unchanged" && snapshot.revision == 7);
  return true;
}

Q4T_TEST(realtime_conversation_mcp_nullable_identity_survives_output_updates) {
  RealtimeConversation history("sess_a");
  Ticket item;
  auto call = Object({{"type", String("mcp_call")},
                      {"id", String("call_a")},
                      {"name", String("lookup")},
                      {"server_label", String("project")},
                      {"approval_request_id", Json()},
                      {"arguments", String("{}")},
                      {"output", Json()}});
  Q4T_CHECK(history.BeginOutput(call, "unused", true, 8192, &item));
  auto changed = call;
  Set(&changed, "approval_request_id", String("different"));
  Q4T_CHECK(!history.Update(item, changed, false));
  Set(&call, "output", String("actual result"));
  Q4T_CHECK(history.Update(item, call, true));
  Json result;
  Q4T_CHECK(history.Get(item, &result));
  Q4T_CHECK(result.Find("approval_request_id")->IsNull());
  Q4T_CHECK(result.GetString("output") == "actual result");
  return true;
}
