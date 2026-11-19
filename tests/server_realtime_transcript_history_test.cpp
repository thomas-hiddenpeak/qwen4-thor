#include "q4t/server/realtime_transcript_history.h"

#include <utility>

#include "q4t/server/asr_response.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using Ticket = RealtimeTranscriptTicket;
}  // namespace

Q4T_TEST(realtime_history_retrieval_distinguishes_pending_failure_and_silence) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket first, second;
  std::optional<std::string_view> text = "untouched";
  Q4T_CHECK(history.Begin("item_a", 100, &first));
  Q4T_CHECK(!history.Transcript(first, &text) && text == "untouched");
  Q4T_CHECK(history.Commit(first) && history.Transcript(first, &text) && !text);
  Q4T_CHECK(history.Fail(first) && history.Transcript(first, &text) && !text);
  Q4T_CHECK(history.Begin("item_b", 100, &second) && history.Commit(second));
  Q4T_CHECK(history.Complete(second, "") && history.Transcript(second, &text));
  Q4T_CHECK(text && text->empty());
  auto foreign = second;
  foreign.owner = "sess_other";
  Q4T_CHECK(!history.Transcript(foreign, &text) && text && text->empty());
  Q4T_CHECK(history.Erase(second) && !history.Transcript(second, &text));
  return true;
}

Q4T_TEST(realtime_history_committed_visibility_and_last_id_follow_deletion) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket first, pending, last;
  Q4T_CHECK(history.Begin("item_a", 100, &first));
  Q4T_CHECK(!history.Committed(first) && history.LastCommittedId().empty());
  Q4T_CHECK(history.Commit(first));
  Q4T_CHECK(history.Begin("item_b", 100, &pending));
  Q4T_CHECK(history.LastCommittedId() == "item_a");
  Q4T_CHECK(history.Begin("item_c", 100, &last) && history.Commit(last));
  Q4T_CHECK(history.Fail(last) && history.Committed(last));
  Q4T_CHECK(history.LastCommittedId() == "item_c");
  Q4T_CHECK(history.Erase(last) && !history.Committed(last));
  Q4T_CHECK(history.LastCommittedId() == "item_a");
  auto other = first;
  other.owner = "sess_other";
  Q4T_CHECK(!history.Committed(other));
  Q4T_CHECK(history.Erase(first) && history.LastCommittedId().empty());
  return true;
}

Q4T_TEST(realtime_history_only_completed_prior_turns_enter_context) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket first, current, future;
  Q4T_CHECK(history.Begin("item_a", 100, &first));
  Q4T_CHECK(history.Commit(first));
  Q4T_CHECK(history.Begin("item_b", 100, &current));
  Q4T_CHECK(history.Commit(current));
  Q4T_CHECK(history.Begin("item_c", 100, &future));
  Q4T_CHECK(history.Commit(future));
  Q4T_CHECK(history.Complete(future, "Future never leaks"));
  std::string text = "unchanged";
  Q4T_CHECK(!history.ContextBefore(current, 100, &text));
  Q4T_CHECK(text == "unchanged");
  Q4T_CHECK(history.Complete(first, "Earlier actual speech"));
  Q4T_CHECK(history.ContextBefore(current, 100, &text));
  Q4T_CHECK(text == "Earlier actual speech");
  Q4T_CHECK(history.Complete(current, "Current speech"));
  Q4T_CHECK(history.ContextBefore(current, 100, &text));
  Q4T_CHECK(text == "Earlier actual speech");
  return true;
}

Q4T_TEST(realtime_history_requires_commit_and_one_terminal) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket item;
  Q4T_CHECK(history.Begin("item_a", 100, &item));
  Q4T_CHECK(!history.Complete(item, "Uncommitted"));
  Q4T_CHECK(history.ChargedBytes() == 100);
  Q4T_CHECK(history.Commit(item) && !history.Commit(item));
  Q4T_CHECK(history.Complete(item, "Speech"));
  Q4T_CHECK(!history.Complete(item, "Changed") && !history.Fail(item));
  Q4T_CHECK(history.ChargedBytes() == 6);
  return true;
}

Q4T_TEST(realtime_history_clear_and_id_reuse_cannot_resurrect_old_result) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket old, fresh;
  Q4T_CHECK(history.Begin("item_a", 100, &old));
  Q4T_CHECK(history.Commit(old) && history.Erase(old));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 0);
  Q4T_CHECK(history.Begin("item_a", 100, &fresh));
  Q4T_CHECK(fresh.generation != old.generation);
  Q4T_CHECK(!history.Complete(old, "Late speech"));
  Q4T_CHECK(!history.Fail(old) && !history.Erase(old));
  Q4T_CHECK(history.Commit(fresh) && history.Complete(fresh, "Fresh"));
  Q4T_CHECK(history.ChargedBytes() == 5);
  return true;
}

Q4T_TEST(realtime_history_snapshot_survives_deletion_without_borrow) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket first, current;
  Q4T_CHECK(history.Begin("item_a", 100, &first) && history.Commit(first));
  Q4T_CHECK(history.Complete(first, "Prior speech"));
  Q4T_CHECK(history.Begin("item_b", 100, &current));
  std::string frozen, next;
  Q4T_CHECK(history.ContextBefore(current, 100, &frozen));
  Q4T_CHECK(history.Erase(first));
  Q4T_CHECK(history.ContextBefore(current, 100, &next));
  Q4T_CHECK(frozen == "Prior speech" && next.empty());
  return true;
}

Q4T_TEST(realtime_history_reservations_and_item_capacity_never_evict) {
  RealtimeTranscriptHistory history("sess_a", 2, 200);
  Ticket a, b, c{"unchanged", "unchanged", 19};
  const auto sentinel = c;
  Q4T_CHECK(history.Begin("item_a", 150, &a));
  Q4T_CHECK(!history.Begin("item_b", 51, &b));
  Q4T_CHECK(history.Begin("item_b", 50, &b));
  Q4T_CHECK(history.Commit(a) && history.Complete(a, "One"));
  Q4T_CHECK(history.ChargedBytes() == 53);
  Q4T_CHECK(!history.Begin("item_c", 1, &c) && c == sentinel);
  Q4T_CHECK(history.Erase(a) && history.Begin("item_c", 150, &c));
  Q4T_CHECK(history.Items() == 2 && history.ChargedBytes() == 200);
  Q4T_CHECK(history.Commit(b) && history.Fail(b));
  Q4T_CHECK(history.ChargedBytes() == 150);
  Q4T_CHECK(!history.Complete(b, "Cannot turn failure into success"));
  return true;
}

Q4T_TEST(realtime_history_rejects_invalid_text_without_repair_or_mutation) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket a, b;
  Q4T_CHECK(history.Begin("item_a", 32, &a) && history.Commit(a));
  for (const auto& invalid : {std::string("\xff"), std::string("\xe4\xb8"),
                              std::string("a\0b", 3), std::string(33, 'x')}) {
    Q4T_CHECK(!history.Complete(a, invalid));
    Q4T_CHECK(history.ChargedBytes() == 32);
  }
  const std::string actual = "中文 � <|im_start|>";
  Q4T_CHECK(history.Complete(a, actual));
  Q4T_CHECK(history.Begin("item_b", 32, &b));
  std::string text;
  Q4T_CHECK(history.ContextBefore(b, 32, &text) && text == actual);
  return true;
}

Q4T_TEST(realtime_history_context_bound_preserves_full_original_or_fails) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket a, b, c;
  Q4T_CHECK(history.Begin("item_a", 10, &a) && history.Commit(a));
  Q4T_CHECK(history.Complete(a, "One"));
  Q4T_CHECK(history.Begin("item_b", 10, &b) && history.Commit(b));
  Q4T_CHECK(history.Complete(b, "Two"));
  Q4T_CHECK(history.Begin("item_c", 10, &c));
  std::string text = "unchanged";
  Q4T_CHECK(!history.ContextBefore(c, 6, &text) && text == "unchanged");
  Q4T_CHECK(history.ContextBefore(c, 7, &text) && text == "One\nTwo");
  return true;
}

Q4T_TEST(realtime_history_failure_and_empty_success_have_distinct_lifetimes) {
  RealtimeTranscriptHistory history("sess_a");
  Ticket cleared, failed, empty, current, found;
  Q4T_CHECK(history.Begin("item_clear", 10, &cleared));
  Q4T_CHECK(history.Fail(cleared) && !history.Find("item_clear", &found));
  Q4T_CHECK(history.Begin("item_failed", 10, &failed) &&
            history.Commit(failed));
  Q4T_CHECK(history.Fail(failed) && history.Find("item_failed", &found));
  Q4T_CHECK(found == failed && !history.Complete(failed, ""));
  Q4T_CHECK(history.Begin("item_empty", 10, &empty) && history.Commit(empty));
  Q4T_CHECK(history.Complete(empty, "") && !history.Fail(empty));
  Q4T_CHECK(history.Begin("item_current", 10, &current));
  std::string text = "unchanged";
  Q4T_CHECK(history.ContextBefore(current, 0, &text) && text.empty());
  Q4T_CHECK(history.Items() == 3 && history.ChargedBytes() == 10);
  return true;
}

Q4T_TEST(realtime_history_identity_checks_include_session_and_generation) {
  RealtimeTranscriptHistory a("sess_a"), b("sess_b");
  Ticket one, other, found;
  Q4T_CHECK(a.Begin("item_a", 10, &one) && b.Begin("item_a", 10, &other));
  Q4T_CHECK(!a.Commit(other) && !b.Erase(one));
  other = one;
  ++other.generation;
  Q4T_CHECK(!a.Fail(other));
  Q4T_CHECK(!a.Begin("item_a", 10, &other));
  Q4T_CHECK(a.Find("item_a", &found) && found == one);
  Q4T_CHECK(a.Items() == 1 && b.Items() == 1);
  return true;
}

Q4T_TEST(realtime_history_invalid_limits_and_ids_never_admit) {
  for (const auto& limits : {std::pair<size_t, size_t>{0, 10},
                             {129, 10},
                             {1, 0},
                             {1, kRealtimeTranscriptHistoryBytes + 1}}) {
    RealtimeTranscriptHistory invalid("sess_a", limits.first, limits.second);
    Ticket ticket;
    Q4T_CHECK(!invalid.Valid() && !invalid.Begin("item_a", 1, &ticket));
  }
  RealtimeTranscriptHistory invalid_owner("bad owner");
  Q4T_CHECK(!invalid_owner.Valid());
  RealtimeTranscriptHistory history("sess_a");
  Ticket ticket;
  for (const auto& id : {std::string(), std::string("root"),
                         std::string("../item"), std::string(129, 'x')})
    Q4T_CHECK(!history.Begin(id, 10, &ticket));
  Q4T_CHECK(!history.Begin("item_a", 0, &ticket));
  Q4T_CHECK(!history.Begin("item_a", kAsrTranscriptBytes + 1, &ticket));
  Q4T_CHECK(!history.Begin("item_a", 10, nullptr));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 0);
  return true;
}

Q4T_TEST(realtime_history_explicit_native_output_budget_is_fully_reserved) {
  RealtimeTranscriptHistory history("sess_a", 128,
                                      kRealtimeTranscriptHistoryBytes);
  Ticket ticket, first;
  for (size_t i = 0; i < 32; ++i) {
    Q4T_CHECK(history.Begin("item_" + std::to_string(i),
                            kAsrTranscriptBytes, &ticket));
    if (!i) first = ticket;
  }
  Q4T_CHECK(history.ChargedBytes() == kRealtimeTranscriptHistoryBytes);
  Q4T_CHECK(!history.Begin("item_extra", 1, &ticket));
  Q4T_CHECK(history.Commit(first));
  Q4T_CHECK(history.Complete(first, std::string(kAsrTranscriptBytes, 'x')));
  Q4T_CHECK(history.ChargedBytes() == kRealtimeTranscriptHistoryBytes);
  Q4T_CHECK(history.Erase(first));
  Q4T_CHECK(history.Begin("item_extra", kAsrTranscriptBytes, &ticket));
  Q4T_CHECK(history.ChargedBytes() == kRealtimeTranscriptHistoryBytes);
  return true;
}
