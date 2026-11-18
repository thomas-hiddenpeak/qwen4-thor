// Speech API input contract. Parsing preserves voice identity and instructions;
// model/voice resolution and actual synthesis are separate required steps.
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

#include "q4t/server/chat_plan.h"

namespace q4t::server {
inline constexpr size_t kSpeechBodyBytes = 128 * 1024;
enum class SpeechFormat { kMp3, kOpus, kAac, kFlac, kWav, kPcm };
struct SpeechRequest {
  std::string model, input, instructions, voice;
  bool voice_id = false;
  SpeechFormat format = SpeechFormat::kMp3;
  double speed = 1;
  bool sse = false;
};
Status ParseSpeechRequest(std::string_view body, SpeechRequest* out,
                          ChatPlanError* error);
std::string_view SpeechFormatName(SpeechFormat format);
std::string_view SpeechContentType(SpeechFormat format);
}  // namespace q4t::server
