// OpenAI Realtime transcription configuration and command contract.
#pragma once

#include <string>
#include <string_view>

#include "q4t/audio/asr_config.h"
#include "q4t/server/asr_request.h"
#include "q4t/server/object_store.h"
#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
struct RealtimeTranscriptionSettings {
  enum class Noise { kNone, kNearField, kFarField };
  enum class Turn { kManual, kServerVad, kSemanticVad };
  RealtimeAudioFormat format;
  AsrRequest request;
  Noise noise = Noise::kNone;
  Turn turn = Turn::kManual;
  std::string eagerness = "auto", delay;
  RealtimeTranscriptionSettings() {
    request.model = audio::AsrConfig::kModelId;
  }
};

enum class RealtimeTranscriptionCommandKind {
  kUpdate,
  kAppend,
  kClear,
  kCommit,
  kDelete,
  kRetrieve
};
struct RealtimeTranscriptionCommand {
  RealtimeTranscriptionCommandKind kind =
      RealtimeTranscriptionCommandKind::kClear;
  std::string event_id, audio, item_id;
  io::Json session;
  bool legacy = false;
};
// Strict original envelope, including duplicate/unknown fields, finite JSON
// budgets and canonical base64. Never changes output on failure.
ObjectReply ParseRealtimeTranscriptionCommand(
    std::string_view message, size_t message_bytes,
    RealtimeTranscriptionCommand* output);
ObjectReply DecodeRealtimeAudioBase64(std::string_view text,
                                      std::string* output);
// Preserves omitted fields and distinguishes explicit null/empty updates.
// Parsing does not claim that optional processing backends have been enabled;
// the execution adapter must validate and execute every accepted setting.
ObjectReply UpdateRealtimeTranscriptionSettings(
    const io::Json& session, bool legacy,
    const RealtimeTranscriptionSettings& current,
    RealtimeTranscriptionSettings* output);
io::Json RealtimeTranscriptionSettingsJson(
    const RealtimeTranscriptionSettings& settings, bool legacy);
}  // namespace q4t::server
