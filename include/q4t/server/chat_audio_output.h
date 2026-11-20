// Chat audio wire contract. Availability, synthesis, private history and
// durable completion are owned by the serving execution layer.
#pragma once

#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/speech_request.h"
#include "q4t/server/actual_usage.h"

namespace q4t::server {
struct ObjectReply;
class RequestControl;

// A cancelled synthesis belongs to the existing Chat request's abort counter.
// Backend failures and unconfirmed mutations must retain their error outcome.
bool ChatAudioWasCancelled(const ObjectReply& reply, RequestControl* control,
                           bool shared_backend_healthy);

struct ChatAudioOptions {
  bool enabled = false;
  bool text = true;
  SpeechRequest speech;
};

// Atomic parse. The resolved speech input is supplied only after actual LLM
// generation; the parser never invents spoken content or maps voice identities.
Status ParseChatAudioOptions(const io::Json& request, ChatAudioOptions* out,
                             ChatPlanError* error);
// Validate the actual generated utterance without losing resolved native
// voice/tempo options. Atomic; fractional speed must survive serialization.
Status ValidateChatSpeechRequest(const SpeechRequest& request,
                                 SpeechRequest* validated,
                                 ChatPlanError* error);
std::string_view ChatAudioFormatName(SpeechFormat format);

// Input history accepts only an assistant audio reference. Retrieval and
// expiry must be checked with the authenticated client before rendering.
Status ChatAudioHistoryId(const io::Json& message, std::string* id,
                          ChatPlanError* error);

// These return delta payloads, not complete Chat SSE envelopes. In particular,
// the expiry-only update must omit finish_reason in its surrounding choice.
Status ChatAudioIdDelta(std::string_view id, std::string* out);
Status ChatAudioDataDelta(std::string_view bytes, std::string* out);
Status ChatAudioTranscriptDelta(std::string_view text, std::string* out);
Status ChatAudioExpiryDelta(int64_t expires_at, std::string* out);
// Canonical concatenable base64: padding is emitted only at actual EOF.
// A sink failure ends the stream; callers cannot replay a partially emitted
// Append. Storage is bounded by one source chunk plus two carry bytes.
class ChatAudioDataStream {
 public:
  Status Append(std::string_view bytes, std::vector<std::string>* deltas);
  Status Finish(std::vector<std::string>* deltas);

 private:
  std::string carry_;
  bool finished_ = false;
};
Status AttachChatAudio(const io::Json& message, const ChatAudioOptions& options,
                       std::string_view id, int64_t expires_at,
                       std::string_view bytes, std::string_view transcript,
                       size_t max_json_bytes, io::Json* out);
Status AddChatAudioUsage(const ActualUsage& synthesis, uint64_t audio_tokens,
                         io::Json* usage);
}  // namespace q4t::server
