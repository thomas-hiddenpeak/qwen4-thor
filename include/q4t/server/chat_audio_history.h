// Private, expiring references to speech actually synthesized by this server.
#pragma once

#include "q4t/server/binary_store.h"
#include "q4t/server/speech_output.h"

namespace q4t::server {
inline constexpr int64_t kChatAudioLifetimeSeconds = 3600;
bool HasChatAudioHistory(const io::Json& request);
ObjectReply PublishChatAudioHistory(ObjectStore& objects,
                                    const BinaryAccess& access,
                                    const std::string& id, int64_t created_at,
                                    std::string_view transcript,
                                    SpeechFormat format,
                                    const SpeechEncodingReceipt& receipt);
// Resolve every reference before publishing the normalized request. IDs remain
// private to the authenticated scope and are not usable after their deadline.
// The text is the exact source spoken by the native pipeline, not an ASR guess.
ObjectReply ResolveChatAudioHistory(ObjectStore* objects,
                                    const BinaryAccess& access,
                                    const io::Json& request, io::Json* out);
}  // namespace q4t::server
