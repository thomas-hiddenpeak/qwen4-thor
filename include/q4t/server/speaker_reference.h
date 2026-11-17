#pragma once

#include "q4t/server/file_audio.h"

namespace q4t::server {
// Inline audio data URL only. Reuses bounded canonical base64 and the owned
// codec worker; never fetches URLs, opens caller paths or looks up identities.
ObjectReply ReadSpeakerReference(std::string_view data,
                                 const BinaryAccess& access,
                                 FileInputContent* out);
ObjectReply DecodeSpeakerReference(std::string_view data,
                                   const BinaryAccess& access,
                                   FileAudioPcm* out);
}  // namespace q4t::server
