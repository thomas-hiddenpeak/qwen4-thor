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

Q4T_TEST(realtime_audio_reservation_hidden_until_owned_commit) {
  RealtimeConversation history("sess_input", 2, 16384);
  Ticket input, other;
  Q4T_CHECK(history.ReserveAudioInput("input", true, 8192, &input));
  const auto revision = history.Revision();
  Json visible = String("unchanged");
  Q4T_CHECK(!history.Find("input", &other) && !history.Get(input, &visible));
  Q4T_CHECK(visible.str == "unchanged" && history.LastId().empty());
  Q4T_CHECK(!history.Create(Message(), "input", {}, &other));
  Q4T_CHECK(!history.Create(Message(), "other", "input", &other));
  Q4T_CHECK(!history.CompleteAudioInput(input, "premature success"));
  Q4T_CHECK(history.Revision() == revision && history.ChargedBytes() == 8192);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot) &&
            snapshot.items.array.empty());
  Q4T_CHECK(history.CommitAudioInput(input));
  Q4T_CHECK(history.Find("input", &other) && other == input);
  Q4T_CHECK(history.Get(input, &visible) &&
            visible.GetString("status") == "completed");
  Q4T_CHECK(!history.CommitAudioInput(input));
  Q4T_CHECK(!history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(history.CompleteAudioInput(input, "real speech"));
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "real speech");
  return true;
}

Q4T_TEST(realtime_audio_reservation_commit_tracks_mixed_history_position) {
  RealtimeConversation history("sess_input");
  Ticket first, streamed, tail, front, middle;
  Q4T_CHECK(history.Create(Message("first"), "first", {}, &first));
  Q4T_CHECK(history.ReserveAudioInput("stream", true, 8192, &streamed));
  Q4T_CHECK(history.Create(Message("while listening"), "tail", {}, &tail));
  Q4T_CHECK(history.CommitAudioInput(streamed));
  Q4T_CHECK(history.CompleteAudioInput(streamed, "stream end"));
  Q4T_CHECK(history.ReserveAudioInput("front", false, 8192, &front));
  Q4T_CHECK(history.ReserveAudioInput("middle", false, 8192, &middle));
  Q4T_CHECK(history.CommitAudioInput(middle, "first"));
  Q4T_CHECK(history.CommitAudioInput(front, "root"));
  Q4T_CHECK(history.CompleteAudioInput(front, "front") &&
            history.CompleteAudioInput(middle, "middle"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.tickets ==
            std::vector<Ticket>({front, first, middle, tail, streamed}));
  Q4T_CHECK(history.LastId() == "stream");
  return true;
}

Q4T_TEST(realtime_audio_reservation_rejected_commit_is_atomic) {
  RealtimeConversation history("sess_input"), foreign("sess_foreign");
  Ticket a, hidden, outside, output;
  Q4T_CHECK(history.ReserveAudioInput("a", true, 8192, &a));
  Q4T_CHECK(history.ReserveAudioInput("hidden", true, 8192, &hidden));
  Q4T_CHECK(foreign.ReserveAudioInput("a", true, 8192, &outside));
  Q4T_CHECK(history.BeginOutput(Message("", "assistant"), "output", false, 8192,
                                &output));
  const auto revision = history.Revision(), charge = history.ChargedBytes();
  for (const auto previous : {"a", "hidden", "missing", "output"})
    Q4T_CHECK(!history.CommitAudioInput(a, previous));
  Q4T_CHECK(!history.CommitAudioInput(outside) &&
            !history.CommitAudioInput(output));
  Q4T_CHECK(history.Revision() == revision && history.ChargedBytes() == charge);
  Q4T_CHECK(history.CommitAudioInput(a));
  Q4T_CHECK(history.LastId() == "a");
  return true;
}

Q4T_TEST(realtime_audio_reservation_failure_releases_uncommitted_owner) {
  RealtimeConversation history("sess_input", 1, 8192);
  Ticket old, fresh;
  Q4T_CHECK(history.ReserveAudioInput("same", true, 8192, &old));
  Q4T_CHECK(!history.ReserveAudioInput("full", true, 8192, &fresh));
  Q4T_CHECK(history.FailAudioInput(old));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 0);
  Q4T_CHECK(history.ReserveAudioInput("same", true, 8192, &fresh));
  Q4T_CHECK(fresh != old && !history.CommitAudioInput(old));
  Q4T_CHECK(!history.FailAudioInput(old) &&
            !history.CompleteAudioInput(old, "late"));
  Q4T_CHECK(history.Erase(fresh) && history.ChargedBytes() == 0);
  return true;
}

Q4T_TEST(realtime_audio_capture_freezes_existing_context_while_waiting) {
  RealtimeConversation history("sess_input");
  Ticket old, speech, added;
  Q4T_CHECK(history.Create(Message("original context"), "old", {}, &old));
  Q4T_CHECK(history.BeginAudioInput("speech", {}, false, 8192, &speech));
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(nullptr, &captured));
  const auto saved = OutputJson(captured.items);
  const auto revision = captured.revision;
  Q4T_CHECK(captured.pending_inputs == std::vector<size_t>{1});
  bool ready = true;
  Q4T_CHECK(history.Resolve(&captured, &ready) && !ready);
  Q4T_CHECK(OutputJson(captured.items) == saved &&
            captured.revision == revision);
  Q4T_CHECK(history.Erase(old));
  Q4T_CHECK(history.Create(Message("future context"), "new", {}, &added));
  Q4T_CHECK(history.CompleteAudioInput(speech, "actual private speech"));
  Q4T_CHECK(history.Resolve(&captured, &ready) && ready);
  Q4T_CHECK(captured.pending_inputs.empty() && captured.revision == revision);
  Q4T_CHECK(captured.tickets == std::vector<Ticket>({old, speech}));
  Q4T_CHECK(captured.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "original context");
  Q4T_CHECK(captured.items.array[1].GetArray("content")->array[0].GetString(
                "text") == "actual private speech");
  Json public_item;
  Q4T_CHECK(history.Get(speech, &public_item) &&
            !public_item.GetArray("content")->array[0].Has("transcript"));
  Q4T_CHECK(history.Erase(speech));
  Q4T_CHECK(history.Resolve(&captured, &ready) && ready);
  return true;
}

Q4T_TEST(realtime_audio_capture_resolves_exact_order_and_real_empty_text) {
  RealtimeConversation history("sess_input");
  Ticket a, b;
  Q4T_CHECK(history.BeginAudioInput("a", {}, true, 8192, &a));
  Q4T_CHECK(history.BeginAudioInput("b", {}, true, 8192, &b));
  auto input = Array({Reference("b"), Message("between"), Reference("a")});
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(&input, &captured));
  const auto saved = OutputJson(captured.items);
  Q4T_CHECK(captured.pending_inputs == std::vector<size_t>({0, 2}));
  Q4T_CHECK(history.CompleteAudioInput(b, ""));
  bool ready = true;
  Q4T_CHECK(history.Resolve(&captured, &ready) && !ready);
  Q4T_CHECK(OutputJson(captured.items) == saved);
  Q4T_CHECK(history.CompleteAudioInput(a, "first spoken"));
  Q4T_CHECK(history.Resolve(&captured, &ready) && ready);
  const auto& empty = captured.items.array[0].GetArray("content")->array[0];
  Q4T_CHECK(empty.Find("text")->IsString() && empty.GetString("text").empty());
  Q4T_CHECK(captured.items.array[2].GetArray("content")->array[0].GetString(
                "text") == "first spoken");
  return true;
}

Q4T_TEST(realtime_audio_capture_deleted_generation_cannot_be_substituted) {
  RealtimeConversation history("sess_input");
  Ticket old, replacement;
  Q4T_CHECK(history.BeginAudioInput("same", {}, true, 8192, &old));
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(nullptr, &captured));
  const auto saved = OutputJson(captured.items);
  Q4T_CHECK(history.Erase(old));
  Q4T_CHECK(history.BeginAudioInput("same", {}, true, 8192, &replacement));
  Q4T_CHECK(history.CompleteAudioInput(replacement, "different user turn"));
  bool ready = true;
  Q4T_CHECK(!history.Resolve(&captured, &ready) && !ready);
  Q4T_CHECK(OutputJson(captured.items) == saved && captured.tickets[0] == old);
  return true;
}

Q4T_TEST(realtime_audio_capture_failed_asr_does_not_execute_old_context) {
  RealtimeConversation history("sess_input");
  Ticket waiting, failed, text;
  Q4T_CHECK(history.Create(Message("old context"), "text", {}, &text));
  Q4T_CHECK(history.BeginAudioInput("waiting", {}, true, 8192, &waiting));
  Q4T_CHECK(history.BeginAudioInput("failed", {}, true, 8192, &failed));
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(nullptr, &captured));
  Q4T_CHECK(history.FailAudioInput(failed));
  bool ready = true;
  // Inspect every captured generation; an earlier wait cannot mask a failure.
  Q4T_CHECK(!history.Resolve(&captured, &ready) && !ready);
  Q4T_CHECK(captured.pending_inputs.size() == 2);
  Q4T_CHECK(history.FailAudioInput(waiting));
  RealtimeConversationSnapshot next;
  Q4T_CHECK(history.Snapshot(nullptr, &next));
  Q4T_CHECK(next.tickets == std::vector<Ticket>{text});
  return true;
}

Q4T_TEST(realtime_audio_capture_rejects_output_hidden_and_foreign_owners) {
  RealtimeConversation history("sess_input"), other("sess_other");
  Ticket output, hidden;
  Q4T_CHECK(history.ReserveAudioInput("hidden", true, 8192, &hidden));
  RealtimeConversationSnapshot captured{"unchanged", 1, String("kept")};
  auto input = Array({Reference("hidden")});
  Q4T_CHECK(!history.Capture(&input, &captured));
  Q4T_CHECK(captured.owner == "unchanged" && captured.items.str == "kept");
  Q4T_CHECK(history.BeginOutput(Message("", "assistant"), "output", true, 8192,
                                &output));
  Q4T_CHECK(!history.Capture(nullptr, &captured));
  Q4T_CHECK(history.Erase(output));
  Q4T_CHECK(history.Capture(nullptr, &captured));
  bool ready = true;
  Q4T_CHECK(!other.Resolve(&captured, &ready) && !ready);
  return true;
}

Q4T_TEST(realtime_audio_capture_explicit_input_ignores_unrelated_waits) {
  RealtimeConversation history("sess_input");
  Ticket pending, text;
  Q4T_CHECK(history.BeginAudioInput("pending", {}, true, 8192, &pending));
  Q4T_CHECK(history.Create(Message("selected text"), "text", {}, &text));
  for (auto input : {Array(), Array({Reference("text")})}) {
    RealtimeConversationSnapshot captured;
    Q4T_CHECK(history.Capture(&input, &captured));
    bool ready = false;
    Q4T_CHECK(captured.pending_inputs.empty() && !captured.default_context);
    Q4T_CHECK(history.Resolve(&captured, &ready) && ready);
    Q4T_CHECK(captured.items.array.size() == input.array.size());
  }
  return true;
}

Q4T_TEST(realtime_audio_capture_resolution_capacity_failure_is_atomic) {
  RealtimeConversation history("sess_input", 2, 32768);
  Ticket speech;
  Q4T_CHECK(history.BeginAudioInput("speech", {}, true, 32768, &speech));
  auto input = Array({Reference("speech"), Message(std::string(10000, 'x'))});
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(&input, &captured));
  const auto saved = OutputJson(captured.items);
  Q4T_CHECK(history.CompleteAudioInput(speech, std::string(24000, 's')));
  bool ready = true;
  Q4T_CHECK(!history.Resolve(&captured, &ready) && !ready);
  Q4T_CHECK(OutputJson(captured.items) == saved &&
            captured.pending_inputs == std::vector<size_t>{0});
  return true;
}

Q4T_TEST(realtime_audio_capture_invalid_positions_cannot_mutate_snapshot) {
  RealtimeConversation history("sess_input");
  Ticket speech;
  Q4T_CHECK(history.BeginAudioInput("speech", {}, true, 8192, &speech));
  RealtimeConversationSnapshot captured;
  Q4T_CHECK(history.Capture(nullptr, &captured));
  const size_t keep = 0;
  Q4T_CHECK(!history.TruncateDefault(captured, {&keep, 1}));
  for (auto positions : {std::vector<size_t>{1}, std::vector<size_t>{0, 0}}) {
    auto invalid = captured;
    invalid.pending_inputs = positions;
    bool ready = true;
    Q4T_CHECK(!history.Resolve(&invalid, &ready) && !ready);
    Q4T_CHECK(invalid.pending_inputs == positions &&
              OutputJson(invalid.items) == OutputJson(captured.items));
  }
  Q4T_CHECK(!history.Resolve(&captured, nullptr));
  return true;
}

Q4T_TEST(realtime_barge_capture_waits_exact_output_and_input_generations) {
  RealtimeConversation history("sess_input");
  Ticket output, spoken, later;
  Q4T_CHECK(history.BeginOutput(Message("", "assistant"), "answer", true, 8192,
                                &output));
  Q4T_CHECK(history.BeginAudioInput("spoken", {}, false, 8192, &spoken));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(!history.Capture(nullptr, &snapshot));
  Q4T_CHECK(history.Capture(nullptr, &snapshot, true));
  Q4T_CHECK(snapshot.pending_outputs == std::vector<size_t>{0} &&
            snapshot.pending_inputs == std::vector<size_t>{1});
  Q4T_CHECK(history.Create(Message("later"), "later", {}, &later));
  Q4T_CHECK(history.CompleteAudioInput(spoken, "interrupting words"));
  bool ready = true;
  Q4T_CHECK(history.Resolve(&snapshot, &ready) && !ready);
  auto terminal = Message("actual prior output", "assistant");
  Set(&terminal, "id", String("answer"));
  Set(&terminal, "status", String("incomplete"));
  Q4T_CHECK(history.Update(output, terminal, true));
  Q4T_CHECK(history.Resolve(&snapshot, &ready) && ready);
  Q4T_CHECK(snapshot.tickets == std::vector<Ticket>({output, spoken}));
  Q4T_CHECK(snapshot.pending_outputs.empty() &&
            snapshot.pending_inputs.empty());
  Q4T_CHECK(snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "actual prior output");
  return true;
}

Q4T_TEST(realtime_barge_capture_incomplete_tool_never_becomes_executable) {
  RealtimeConversation history("sess_input");
  auto tool = Object({{"type", String("function_call")},
                      {"name", String("lookup")},
                      {"arguments", String("{")}});
  Ticket ticket;
  Q4T_CHECK(history.BeginOutput(tool, "call", true, 8192, &ticket));
  RealtimeConversationSnapshot normal, explicit_reference;
  auto input = Array({Reference("call")});
  Q4T_CHECK(history.Capture(nullptr, &normal, true));
  Q4T_CHECK(history.Capture(&input, &explicit_reference, true));
  Q4T_CHECK(history.Get(ticket, &tool));
  Set(&tool, "status", String("incomplete"));
  Q4T_CHECK(history.Update(ticket, tool, true));
  bool ready = false;
  Q4T_CHECK(history.Resolve(&normal, &ready) && ready);
  Q4T_CHECK(normal.items.array.empty() && normal.tickets.empty());
  Q4T_CHECK(!history.Resolve(&explicit_reference, &ready) && !ready);
  Q4T_CHECK(explicit_reference.items.array.size() == 1);
  return true;
}

Q4T_TEST(realtime_barge_capture_deleted_output_cannot_bind_replacement) {
  RealtimeConversation history("sess_input");
  Ticket old, replacement;
  Q4T_CHECK(
      history.BeginOutput(Message("", "assistant"), "same", true, 8192, &old));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Capture(nullptr, &snapshot, true));
  const auto saved = OutputJson(snapshot.items);
  Q4T_CHECK(history.Erase(old));
  Q4T_CHECK(history.Create(Message("replacement", "assistant"), "same", {},
                           &replacement));
  bool ready = true;
  Q4T_CHECK(!history.Resolve(&snapshot, &ready) && !ready);
  Q4T_CHECK(OutputJson(snapshot.items) == saved && snapshot.tickets[0] == old);
  return true;
}

Q4T_TEST(realtime_barge_capture_rejects_forged_output_and_overlap) {
  RealtimeConversation history("sess_input");
  Ticket spoken;
  Q4T_CHECK(history.BeginAudioInput("spoken", {}, true, 8192, &spoken));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Capture(nullptr, &snapshot, true));
  snapshot.pending_outputs = {0};
  bool ready = true;
  Q4T_CHECK(!history.Resolve(&snapshot, &ready) && !ready);
  snapshot.pending_inputs.clear();
  Q4T_CHECK(!history.Resolve(&snapshot, &ready) && !ready);
  return true;
}

Q4T_TEST(realtime_audio_input_pending_commit_cannot_become_empty_prompt) {
  RealtimeConversation history("sess_input");
  Ticket ticket;
  Q4T_CHECK(history.BeginAudioInput("input", {}, true, 8192, &ticket));
  Json item;
  Q4T_CHECK(history.Get(ticket, &item));
  Q4T_CHECK(item.GetString("status") == "completed");
  Q4T_CHECK(!item.GetArray("content")->array[0].Has("transcript"));
  RealtimeConversationSnapshot snapshot{"keep", 9, String("unchanged")};
  Q4T_CHECK(!history.Snapshot(nullptr, &snapshot));
  auto input = Array({Reference("input")});
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.owner == "keep" && snapshot.revision == 9 &&
            snapshot.items.str == "unchanged");
  Q4T_CHECK(history.CompleteAudioInput(ticket, "Actual ASR words"));
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  const auto& part = snapshot.items.array[0].GetArray("content")->array[0];
  Q4T_CHECK(part.GetString("type") == "input_text" &&
            part.GetString("text") == "Actual ASR words");
  Q4T_CHECK(history.Get(ticket, &item));
  Q4T_CHECK(item.GetArray("content")->array[0].GetString("type") ==
            "input_audio");
  Q4T_CHECK(item.GetArray("content")->array[0].GetString("transcript") ==
            "Actual ASR words");
  return true;
}

Q4T_TEST(realtime_audio_input_private_transcript_still_drives_owned_context) {
  RealtimeConversation history("sess_input");
  Ticket ticket;
  Q4T_CHECK(history.BeginAudioInput("input", {}, false, 8192, &ticket));
  Q4T_CHECK(history.CompleteAudioInput(ticket, "private ASR text"));
  Json item;
  Q4T_CHECK(history.Get(ticket, &item));
  Q4T_CHECK(!item.GetArray("content")->array[0].Has("transcript"));
  RealtimeConversationSnapshot snapshot;
  auto input = Array({Reference("input")});
  Q4T_CHECK(history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.tickets[0] == ticket);
  Q4T_CHECK(snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "private ASR text");
  return true;
}

Q4T_TEST(realtime_audio_input_failed_asr_keeps_item_and_healthy_text_context) {
  RealtimeConversation history("sess_input");
  Ticket bad, text;
  Q4T_CHECK(history.BeginAudioInput("bad", {}, true, 8192, &bad));
  Q4T_CHECK(history.FailAudioInput(bad));
  Q4T_CHECK(history.Create(Message("new healthy text"), "text", {}, &text));
  Q4T_CHECK(history.Items() == 2);
  Json item;
  Q4T_CHECK(history.Get(bad, &item));
  Q4T_CHECK(!item.GetArray("content")->array[0].Has("transcript"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1 && snapshot.tickets[0] == text);
  auto input = Array({Reference("bad")});
  Q4T_CHECK(!history.Snapshot(&input, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1 && snapshot.tickets[0] == text);
  Q4T_CHECK(!history.CompleteAudioInput(bad, "late false success"));
  return true;
}

Q4T_TEST(
    realtime_audio_input_client_transcript_cannot_forge_native_provenance) {
  RealtimeConversation history("sess_input");
  Ticket native, client;
  Q4T_CHECK(history.BeginAudioInput("native", {}, true, 8192, &native));
  Json item;
  Q4T_CHECK(history.Get(native, &item));
  Set(&Field(item, "content").array[0], "transcript", String("forged"));
  Q4T_CHECK(!history.Update(native, item, true, true));
  Set(&item, "id", String("client"));
  Q4T_CHECK(history.Create(item, "client", {}, &client));
  Q4T_CHECK(!history.CompleteAudioInput(client, "forged native text"));
  Q4T_CHECK(!history.FailAudioInput(client));
  Q4T_CHECK(history.FailAudioInput(native));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.items.array.size() == 1);
  const auto& part = snapshot.items.array[0].GetArray("content")->array[0];
  Q4T_CHECK(part.GetString("type") == "input_audio" &&
            part.GetString("transcript") == "forged");
  return true;
}

Q4T_TEST(realtime_audio_input_late_completion_cannot_cross_deleted_generation) {
  RealtimeConversation a("sess_a"), b("sess_b");
  Ticket old, fresh, other;
  Q4T_CHECK(a.BeginAudioInput("same", {}, true, 8192, &old));
  Q4T_CHECK(b.BeginAudioInput("same", {}, true, 8192, &other));
  Q4T_CHECK(a.Erase(old) && a.ChargedBytes() == 0);
  Q4T_CHECK(a.BeginAudioInput("same", {}, true, 8192, &fresh));
  Q4T_CHECK(!a.CompleteAudioInput(old, "late") && !a.FailAudioInput(old));
  Q4T_CHECK(!a.CompleteAudioInput(other, "foreign"));
  Q4T_CHECK(!b.FailAudioInput(fresh));
  Q4T_CHECK(a.CompleteAudioInput(fresh, "actual") && b.FailAudioInput(other));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(a.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.tickets[0] == fresh);
  Q4T_CHECK(snapshot.items.array[0].GetArray("content")->array[0].GetString(
                "text") == "actual");
  return true;
}

Q4T_TEST(realtime_audio_input_reservation_and_failed_growth_are_atomic) {
  RealtimeConversation history("sess_input", 2, 16384);
  Ticket input, untouched{"kept", "kept", 99};
  Q4T_CHECK(history.BeginAudioInput("input", {}, true, 8192, &input));
  const auto revision = history.Revision(), bytes = history.ChargedBytes();
  Q4T_CHECK(!history.BeginAudioInput("bad", {}, true, 0, &untouched));
  Q4T_CHECK(!history.BeginAudioInput("bad", "absent", true, 8192, &untouched));
  Q4T_CHECK(!history.CompleteAudioInput(input, std::string(8192, 'x')));
  Q4T_CHECK(history.Revision() == revision && history.ChargedBytes() == bytes &&
            untouched.owner == "kept" && untouched.generation == 99);
  Q4T_CHECK(history.CompleteAudioInput(input, ""));
  Q4T_CHECK(history.ChargedBytes() < bytes);
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  const auto& part = snapshot.items.array[0].GetArray("content")->array[0];
  Q4T_CHECK(part.Find("text")->IsString() && part.GetString("text").empty());
  Q4T_CHECK(!history.FailAudioInput(input) &&
            !history.CompleteAudioInput(input, "again"));
  return true;
}

Q4T_TEST(realtime_audio_input_snapshot_survives_delete_and_context_cut) {
  RealtimeConversation history("sess_input");
  Ticket audio, text;
  Q4T_CHECK(history.BeginAudioInput("audio", {}, true, 8192, &audio));
  Q4T_CHECK(history.CompleteAudioInput(audio, "immutable spoken input"));
  Q4T_CHECK(history.Create(Message("new text"), "text", {}, &text));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  const auto original = OutputJson(snapshot.items);
  const size_t keep = 1;
  Q4T_CHECK(history.TruncateDefault(snapshot, {&keep, 1}));
  Json retained;
  Q4T_CHECK(history.Get(audio, &retained));
  Q4T_CHECK(retained.GetArray("content")->array[0].GetString("type") ==
            "input_audio");
  Q4T_CHECK(history.Erase(audio));
  Q4T_CHECK(OutputJson(snapshot.items) == original);
  RealtimeConversationSnapshot next;
  Q4T_CHECK(history.Snapshot(nullptr, &next));
  Q4T_CHECK(next.tickets.size() == 1 && next.tickets[0] == text);
  return true;
}

Q4T_TEST(realtime_audio_input_preserves_mixed_conversation_predecessor) {
  RealtimeConversation history("sess_input");
  Ticket first, spoken, last;
  Q4T_CHECK(history.Create(Message("first"), "first", {}, &first));
  Q4T_CHECK(history.Create(Message("last"), "last", {}, &last));
  Q4T_CHECK(history.BeginAudioInput("spoken", "first", true, 8192, &spoken));
  Q4T_CHECK(history.CompleteAudioInput(spoken, "middle actual speech"));
  RealtimeConversationSnapshot snapshot;
  Q4T_CHECK(history.Snapshot(nullptr, &snapshot));
  Q4T_CHECK(snapshot.tickets == std::vector<Ticket>({first, spoken, last}));
  Q4T_CHECK(history.LastId() == "last");
  Q4T_CHECK(!history.TruncateAudioTranscript(spoken));
  return true;
}

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

Q4T_TEST(
    realtime_conversation_incomplete_server_tool_is_retained_not_reexecuted) {
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

Q4T_TEST(
    realtime_conversation_client_status_cannot_hide_call_or_incomplete_text) {
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

Q4T_TEST(
    realtime_conversation_context_cut_persists_but_history_is_retrievable) {
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
