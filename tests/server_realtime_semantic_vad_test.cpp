#include "q4t/server/realtime_semantic_vad.h"

#include <array>
#include <limits>
#include <utility>

#include "q4t/server/realtime_audio_queue.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using K = RealtimeSemanticDecision::Kind;
using S = RealtimeSemanticScore;
constexpr uint64_t Ms(uint64_t value) { return value * 48; }
bool SpeakingThenQuiet(RealtimeSemanticVad* gate, uint64_t turn = 1,
                       uint64_t origin = 0) {
  return gate->Begin(turn, origin) && gate->Observe(origin + Ms(1000), true) &&
         gate->Observe(origin + Ms(1200), false);
}
}  // namespace

Q4T_TEST(semantic_vad_eagerness_enforces_actual_maximum_audio_wait) {
  for (const auto& [name, delay] :
       std::array<std::pair<const char*, uint64_t>, 4>{
           {{"low", 8000}, {"medium", 4000}, {"auto", 4000}, {"high", 2000}}}) {
    RealtimeSemanticVad gate(name);
    Q4T_CHECK(gate.Valid() && SpeakingThenQuiet(&gate));
    Q4T_CHECK(gate.Observe(Ms(1000 + delay) - 1, false));
    Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
    Q4T_CHECK(gate.Observe(Ms(1000 + delay), false));
    const auto end = gate.Inspect();
    Q4T_CHECK(end.kind == K::kEnd && end.maximum_wait);
    Q4T_CHECK(!end.probability_available && end.end_ticks == Ms(1000 + delay));
    RealtimeSemanticTicket ticket;
    Q4T_CHECK(!gate.RequestScore(1, Ms(1000), &ticket));
  }
  return true;
}

Q4T_TEST(semantic_vad_finished_content_still_requires_observed_silence) {
  RealtimeSemanticVad gate("high");
  RealtimeSemanticTicket ticket;
  Q4T_CHECK(gate.Begin(1, 0) && gate.Observe(Ms(1000), true));
  Q4T_CHECK(gate.Inspect().kind == K::kAwaitingSilence);
  Q4T_CHECK(!gate.RequestScore(1, Ms(1000), &ticket));
  Q4T_CHECK(gate.Observe(Ms(1199), false));
  Q4T_CHECK(!gate.RequestScore(1, Ms(1000), &ticket));
  Q4T_CHECK(gate.Observe(Ms(1200), false));
  Q4T_CHECK(gate.RequestScore(1, Ms(1000), &ticket));
  Q4T_CHECK(gate.CompleteScore(ticket, 1) == S::kAccepted);
  const auto end = gate.Inspect();
  Q4T_CHECK(end.kind == K::kEnd && end.probability_available);
  Q4T_CHECK(!end.maximum_wait && end.end_ticks == Ms(1200));
  return true;
}

Q4T_TEST(semantic_vad_incomplete_content_waits_without_repeated_model_work) {
  RealtimeSemanticVad gate;
  RealtimeSemanticTicket ticket;
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(gate.RequestScore(1, Ms(1000), &ticket));
  Q4T_CHECK(gate.CompleteScore(ticket, 0) == S::kAccepted);
  Q4T_CHECK(!gate.RequestScore(2, Ms(1200), &ticket));
  Q4T_CHECK(gate.Observe(Ms(4999), false));
  Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
  Q4T_CHECK(gate.Observe(Ms(5000), false));
  Q4T_CHECK(gate.Inspect().kind == K::kEnd);
  Q4T_CHECK(gate.Inspect().probability_available);
  return true;
}

Q4T_TEST(semantic_vad_intermediate_score_has_one_absolute_deadline) {
  RealtimeSemanticVad gate("high");
  RealtimeSemanticTicket ticket;
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(gate.RequestScore(1, Ms(1000), &ticket));
  Q4T_CHECK(gate.CompleteScore(ticket, .5) == S::kAccepted);
  Q4T_CHECK(gate.Inspect().end_ticks == Ms(2100));
  for (uint64_t milliseconds = 1201; milliseconds < 2100; ++milliseconds) {
    Q4T_CHECK(gate.Observe(Ms(milliseconds), false));
    Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
    Q4T_CHECK(gate.Inspect().end_ticks == Ms(2100));
  }
  Q4T_CHECK(gate.Observe(Ms(2100), false));
  Q4T_CHECK(gate.Inspect().kind == K::kEnd);
  return true;
}

Q4T_TEST(semantic_vad_idle_and_unvoiced_audio_never_invent_a_turn_end) {
  RealtimeSemanticVad gate;
  Q4T_CHECK(gate.Begin(1, 0));
  for (uint64_t second = 1; second <= 20; ++second) {
    Q4T_CHECK(gate.Observe(second * Ms(1000), false));
    Q4T_CHECK(gate.Inspect().kind == K::kAwaitingSilence);
  }
  Q4T_CHECK(gate.Observe(Ms(21000), true));
  Q4T_CHECK(gate.Observe(Ms(21200), false));
  for (int poll = 0; poll < 1000; ++poll) {
    Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
    Q4T_CHECK(gate.Inspect().end_ticks == Ms(25000));
  }
  return true;
}

Q4T_TEST(semantic_vad_score_requires_current_hypothesis_coverage) {
  RealtimeSemanticVad gate;
  RealtimeSemanticTicket ticket;
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(!gate.RequestScore(0, Ms(1000), &ticket));
  Q4T_CHECK(!gate.RequestScore(1, Ms(999), &ticket));
  Q4T_CHECK(!gate.RequestScore(1, Ms(1201), &ticket));
  Q4T_CHECK(!gate.RequestScore(1, Ms(1000), nullptr));
  Q4T_CHECK(!gate.ScoreInFlight());
  Q4T_CHECK(gate.RequestScore(2, Ms(1200), &ticket));
  Q4T_CHECK(ticket.transcript_revision == 2 &&
            ticket.speech_end_ticks == Ms(1000));
  return true;
}

Q4T_TEST(semantic_vad_resumed_speech_fences_late_score_without_worker_growth) {
  RealtimeSemanticVad gate;
  RealtimeSemanticTicket first, next;
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(gate.RequestScore(1, Ms(1000), &first));
  Q4T_CHECK(gate.Observe(Ms(1500), true));
  Q4T_CHECK(gate.Observe(Ms(1700), false));
  Q4T_CHECK(gate.ScoreInFlight());
  Q4T_CHECK(!gate.RequestScore(2, Ms(1500), &next));
  Q4T_CHECK(gate.CompleteScore(first, 1) == S::kStale);
  Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
  Q4T_CHECK(!gate.Inspect().probability_available);
  Q4T_CHECK(gate.RequestScore(2, Ms(1500), &next));
  Q4T_CHECK(next.sequence != first.sequence && next.quiet != first.quiet);
  Q4T_CHECK(gate.CompleteScore(first, 1) == S::kInvalid);
  Q4T_CHECK(gate.ScoreInFlight());
  Q4T_CHECK(gate.CompleteScore(next, 0) == S::kAccepted);
  Q4T_CHECK(gate.Inspect().end_ticks == Ms(5500));
  return true;
}

Q4T_TEST(semantic_vad_finished_turn_retains_unreaped_score_slot) {
  RealtimeSemanticVad gate;
  RealtimeSemanticTicket old, current;
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(gate.RequestScore(1, Ms(1000), &old));
  Q4T_CHECK(!gate.Finish(2) && gate.Finish(1));
  Q4T_CHECK(gate.Inspect().kind == K::kIdle && gate.ScoreInFlight());
  Q4T_CHECK(SpeakingThenQuiet(&gate, 2, Ms(1200)));
  Q4T_CHECK(!gate.RequestScore(1, Ms(2200), &current));
  Q4T_CHECK(gate.CompleteScore(old, 1) == S::kStale);
  Q4T_CHECK(gate.RequestScore(1, Ms(2200), &current));
  Q4T_CHECK(gate.CompleteScore(current, 1) == S::kAccepted);
  Q4T_CHECK(gate.Inspect().end_ticks == Ms(2400));
  return true;
}

Q4T_TEST(semantic_vad_malformed_ticket_cannot_release_actual_worker) {
  RealtimeSemanticVad gate;
  RealtimeSemanticTicket ticket;
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(gate.RequestScore(3, Ms(1000), &ticket));
  for (int field = 0; field < 5; ++field) {
    auto bad = ticket;
    std::array<uint64_t*, 5> values{&bad.sequence, &bad.turn, &bad.quiet,
                                    &bad.speech_end_ticks,
                                    &bad.transcript_revision};
    ++*values[field];
    Q4T_CHECK(gate.CompleteScore(bad, 1) == S::kInvalid);
    Q4T_CHECK(gate.ScoreInFlight());
  }
  Q4T_CHECK(gate.CompleteScore(ticket, .5) == S::kAccepted);
  Q4T_CHECK(gate.CompleteScore(ticket, 1) == S::kInvalid);
  Q4T_CHECK(gate.Inspect().end_ticks == Ms(3100));
  return true;
}

Q4T_TEST(semantic_vad_failed_or_invalid_model_score_keeps_unknown_bounded) {
  for (auto probability : std::array<std::optional<double>, 5>{
           std::nullopt, -1, 2, std::numeric_limits<double>::infinity(),
           std::numeric_limits<double>::quiet_NaN()}) {
    RealtimeSemanticVad gate("high");
    RealtimeSemanticTicket ticket;
    Q4T_CHECK(SpeakingThenQuiet(&gate));
    Q4T_CHECK(gate.RequestScore(1, Ms(1000), &ticket));
    Q4T_CHECK(gate.CompleteScore(ticket, probability) ==
              (probability ? S::kInvalid : S::kUnavailable));
    Q4T_CHECK(!gate.ScoreInFlight() && !gate.Inspect().probability_available);
    Q4T_CHECK(!gate.RequestScore(2, Ms(1200), &ticket));
    Q4T_CHECK(gate.Observe(Ms(2999), false));
    Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
    Q4T_CHECK(gate.Observe(Ms(3000), false));
    Q4T_CHECK(gate.Inspect().kind == K::kEnd);
    Q4T_CHECK(gate.Inspect().maximum_wait);
  }
  return true;
}

Q4T_TEST(semantic_vad_invalid_settings_and_clock_mutations_preserve_state) {
  for (RealtimeSemanticVad gate :
       {RealtimeSemanticVad("unknown"), RealtimeSemanticVad("high", 0),
        RealtimeSemanticVad("high", 2001)}) {
    Q4T_CHECK(!gate.Valid() && !gate.Begin(1, 0));
    Q4T_CHECK(gate.Inspect().kind == K::kIdle);
  }
  RealtimeSemanticVad gate;
  Q4T_CHECK(!gate.Begin(0, 0));
  Q4T_CHECK(!gate.Begin(1, kRealtimeSessionTicks + 1));
  Q4T_CHECK(SpeakingThenQuiet(&gate));
  Q4T_CHECK(!gate.Begin(2, Ms(1200)));
  Q4T_CHECK(!gate.Observe(Ms(1199), true));
  Q4T_CHECK(!gate.Observe(Ms(1200), true));
  Q4T_CHECK(!gate.Observe(kRealtimeSessionTicks + 1, true));
  Q4T_CHECK(gate.Inspect().end_ticks == Ms(5000));
  Q4T_CHECK(gate.Finish(1) && !gate.Finish(1));
  Q4T_CHECK(!gate.Begin(1, Ms(1200)) && !gate.Begin(2, Ms(1199)));
  Q4T_CHECK(gate.Begin(2, Ms(1200)));
  return true;
}

Q4T_TEST(semantic_vad_absolute_clock_and_supported_sample_grids_are_kept) {
  for (uint64_t sample_step : {1, 2, 3, 6}) {
    const uint64_t origin = kRealtimeSessionTicks - Ms(10000) + sample_step;
    RealtimeSemanticVad gate("low");
    RealtimeSemanticTicket ticket;
    Q4T_CHECK(SpeakingThenQuiet(&gate, 7, origin));
    Q4T_CHECK(gate.RequestScore(1, origin + Ms(1000), &ticket));
    Q4T_CHECK(gate.CompleteScore(ticket, .5) == S::kAccepted);
    const auto end = gate.Inspect().end_ticks;
    Q4T_CHECK(end == origin + Ms(5100));
    Q4T_CHECK((end - origin) % sample_step == 0);
    Q4T_CHECK(gate.Observe(end - sample_step, false));
    Q4T_CHECK(gate.Inspect().kind == K::kWaiting);
    Q4T_CHECK(gate.Observe(end, false) && gate.Inspect().kind == K::kEnd);
  }
  return true;
}
