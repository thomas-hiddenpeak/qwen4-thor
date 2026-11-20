// Effective conversation input settings; native ASR remains the audio owner.
#pragma once

#include <optional>

#include "q4t/server/realtime_transcription_protocol.h"

namespace q4t::server {
struct RealtimeDuplexInput {
  bool enabled = false;
  // Disabling the public transcript does not bypass native audio processing.
  bool transcript_events = false;
  bool create_response = true;
  bool interrupt_response = true;
  std::optional<uint32_t> idle_timeout_ms;
  RealtimeTranscriptionSettings processing;

  bool RespondOnCommit(bool automatic) const {
    return enabled && automatic && create_response &&
           processing.turn != RealtimeTranscriptionSettings::Turn::kManual;
  }
  bool InterruptOnSpeechStart() const {
    return enabled && interrupt_response &&
           processing.turn != RealtimeTranscriptionSettings::Turn::kManual;
  }
};

// Reads the effective session after prompt/default/explicit-field composition,
// not a patch. The caller first applies the wire schema and storage bound.
// Only audio.input and transcription includes are this planner's concern.
// It reuses the transcription parser without treating conversation controls
// as ASR settings. Failure leaves output untouched; this pure plan does not
// assert that an ASR/VAD/idle-response executor is available or connected.
ObjectReply PlanRealtimeDuplexInput(const io::Json& session,
                                    RealtimeDuplexInput* output);
}  // namespace q4t::server
