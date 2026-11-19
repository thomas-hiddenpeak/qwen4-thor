#include "q4t/server/realtime_speech_history.h"

#include "q4t/test.h"

namespace {
using namespace q4t::server;
const RealtimeItemTicket kItem{"sess_a", "item_a", 1};
}  // namespace

Q4T_TEST(realtime_speech_history_reserves_before_actual_pcm_and_complete) {
  RealtimeSpeechHistory history("sess_a", 8192);
  Q4T_CHECK(history.Begin(kItem, {}, 4096));
  const auto reserved = history.ChargedBytes();
  Q4T_CHECK(reserved >= 4096 && reserved <= 8192);
  Q4T_CHECK(history.Append(kItem, std::string(2000, '\1')));
  Q4T_CHECK(history.Append(kItem, std::string(2000, '\2')));
  Q4T_CHECK(history.ChargedBytes() == reserved);
  Q4T_CHECK(history.Complete(kItem));
  RealtimeSpeechView view;
  Q4T_CHECK(history.Get(kItem, &view));
  Q4T_CHECK(view.generated_samples == 2000 && view.bytes.size() == 4000);
  Q4T_CHECK(view.complete && !view.truncated && !view.interrupted);
  Q4T_CHECK(view.bytes == std::string(2000, '\1') + std::string(2000, '\2'));
  Q4T_CHECK(!history.Append(kItem, std::string(2, '\0')));
  Q4T_CHECK(!history.Complete(kItem));
  return true;
}

Q4T_TEST(realtime_speech_history_capacity_and_item_rejections_are_atomic) {
  RealtimeSpeechHistory history("sess_a", 128, 1);
  Q4T_CHECK(!history.Begin(kItem, {}, 129));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 0);
  Q4T_CHECK(history.Begin(kItem, {}, 64));
  const auto charge = history.ChargedBytes();
  Q4T_CHECK(!history.Begin({"sess_a", "item_b", 2}, {}, 32));
  Q4T_CHECK(history.Append(kItem, std::string(62, '\1')));
  Q4T_CHECK(!history.Append(kItem, std::string(4, '\2')));
  Q4T_CHECK(!history.Append(kItem, std::string(1, '\2')));
  RealtimeSpeechView view;
  Q4T_CHECK(history.Get(kItem, &view));
  Q4T_CHECK(view.bytes == std::string(62, '\1') &&
            view.generated_samples == 31);
  Q4T_CHECK(history.Items() == 1 && history.ChargedBytes() == charge);
  Q4T_CHECK(history.Append(kItem, std::string(2, '\2')));
  return true;
}

Q4T_TEST(realtime_speech_history_owner_generation_and_delete_prevent_revival) {
  RealtimeSpeechHistory history("sess_a");
  Q4T_CHECK(history.Begin(kItem, {}, 64));
  for (const auto& bad : {RealtimeItemTicket{"sess_b", "item_a", 1},
                          RealtimeItemTicket{"sess_a", "item_a", 2},
                          RealtimeItemTicket{"sess_a", "item_b", 1}}) {
    RealtimeSpeechView untouched;
    untouched.generated_samples = 91;
    Q4T_CHECK(!history.Get(bad, &untouched));
    Q4T_CHECK(untouched.generated_samples == 91);
    Q4T_CHECK(!history.Append(bad, std::string(2, '\0')));
    Q4T_CHECK(!history.Complete(bad));
    Q4T_CHECK(!history.Truncate(bad, 0));
    Q4T_CHECK(!history.Erase(bad));
  }
  Q4T_CHECK(history.Erase(kItem));
  Q4T_CHECK(history.ChargedBytes() == 0 && history.Items() == 0);
  const RealtimeItemTicket next{"sess_a", "item_a", 2};
  Q4T_CHECK(history.Begin(next, {}, 64));
  Q4T_CHECK(!history.Append(kItem, std::string(2, '\0')));
  Q4T_CHECK(history.Append(next, std::string(2, '\1')));
  return true;
}

Q4T_TEST(realtime_speech_history_pcm_truncate_keeps_generation_and_charge) {
  RealtimeSpeechHistory history("sess_a");
  Q4T_CHECK(history.Begin(kItem, {}, 9600));
  Q4T_CHECK(history.Append(kItem, std::string(4800, '\1')));
  const auto charge = history.ChargedBytes();
  Q4T_CHECK(!history.Truncate(kItem, UINT64_MAX));
  Q4T_CHECK(!history.Truncate(kItem, 101));
  Q4T_CHECK(history.Truncate(kItem, 37));
  RealtimeSpeechView view;
  Q4T_CHECK(history.Get(kItem, &view));
  Q4T_CHECK(view.bytes.size() == 37 * 24 * 2);
  Q4T_CHECK(view.generated_samples == 2400 && view.truncated && !view.complete);
  Q4T_CHECK(history.ChargedBytes() == charge);
  Q4T_CHECK(!history.Append(kItem, std::string(2, '\0')));
  Q4T_CHECK(!history.Complete(kItem));
  Q4T_CHECK(!history.Truncate(kItem, 38));
  Q4T_CHECK(history.Truncate(kItem, 0));
  Q4T_CHECK(history.Get(kItem, &view));
  Q4T_CHECK(view.bytes.empty() && view.generated_samples == 2400);
  return true;
}

Q4T_TEST(realtime_speech_history_g711_truncate_uses_negotiated_clock) {
  for (auto encoding :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    RealtimeSpeechHistory history("sess_a");
    Q4T_CHECK(history.Begin(kItem, {encoding, 8000}, 4000));
    Q4T_CHECK(history.Append(kItem, std::string(801, '\1')));
    Q4T_CHECK(history.Complete(kItem));
    Q4T_CHECK(!history.Truncate(kItem, 101));
    Q4T_CHECK(history.Truncate(kItem, 100));
    RealtimeSpeechView view;
    Q4T_CHECK(history.Get(kItem, &view));
    Q4T_CHECK(view.bytes.size() == 800 && view.generated_samples == 801);
    Q4T_CHECK(view.format.encoding == encoding && view.format.rate == 8000);
    Q4T_CHECK(view.truncated && !view.complete);
  }
  return true;
}

Q4T_TEST(realtime_speech_history_interruption_preserves_real_partial_extent) {
  for (const bool empty : {false, true}) {
    RealtimeSpeechHistory history("sess_a");
    Q4T_CHECK(history.Begin(kItem, {}, 4000));
    if (!empty) Q4T_CHECK(history.Append(kItem, std::string(1000, '\1')));
    Q4T_CHECK(history.Interrupt(kItem));
    Q4T_CHECK(history.Interrupt(kItem));
    Q4T_CHECK(!history.Complete(kItem));
    Q4T_CHECK(!history.Append(kItem, std::string(2, '\0')));
    RealtimeSpeechView view;
    Q4T_CHECK(history.Get(kItem, &view));
    Q4T_CHECK(view.interrupted && !view.complete && !view.truncated);
    Q4T_CHECK(view.bytes.size() == size_t(empty ? 0 : 1000));
    Q4T_CHECK(history.Truncate(kItem, 0));
    Q4T_CHECK(history.Get(kItem, &view));
    Q4T_CHECK(view.truncated && view.bytes.empty());
  }
  return true;
}

Q4T_TEST(
    realtime_speech_history_invalid_format_and_reservation_never_allocate) {
  Q4T_CHECK(!RealtimeSpeechHistory("").Valid());
  Q4T_CHECK(!RealtimeSpeechHistory("sess_a", 0).Valid());
  Q4T_CHECK(!RealtimeSpeechHistory("sess_a", 128, 129).Valid());
  Q4T_CHECK(
      !RealtimeSpeechHistory("sess_a", RealtimeSpeechHistory::kMaximumBytes + 1)
           .Valid());
  RealtimeSpeechHistory history("sess_a");
  for (const auto format :
       {RealtimeAudioFormat{RealtimeAudioEncoding::kPcm16, 16000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 24000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kFloat32, 24000}})
    Q4T_CHECK(!history.Begin(kItem, format, 64));
  Q4T_CHECK(!history.Begin(kItem, {}, 0));
  Q4T_CHECK(!history.Begin(kItem, {}, 63));
  Q4T_CHECK(!history.Begin(kItem, {}, kRealtimeAudioBytes + 2));
  Q4T_CHECK(!history.Begin({"sess_a", "item_a", 0}, {}, 64));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 0);
  return true;
}

Q4T_TEST(realtime_speech_history_empty_and_duplicate_completion_keep_owner) {
  RealtimeSpeechHistory history("sess_a");
  Q4T_CHECK(history.Begin(kItem, {}, 64));
  Q4T_CHECK(!history.Complete(kItem));
  Q4T_CHECK(!history.Begin(kItem, {}, 64));
  Q4T_CHECK(!history.Begin({"sess_a", "item_a", 2}, {}, 64));
  Q4T_CHECK(!history.Append(kItem, {}));
  Q4T_CHECK(history.Append(kItem, std::string(2, '\0')));
  Q4T_CHECK(history.Complete(kItem));
  Q4T_CHECK(history.Interrupt(kItem));
  RealtimeSpeechView view;
  Q4T_CHECK(history.Get(kItem, &view));
  Q4T_CHECK(view.complete && !view.interrupted);
  Q4T_CHECK(history.Erase(kItem));
  Q4T_CHECK(history.ChargedBytes() == 0);
  return true;
}
