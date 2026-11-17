// Native transcription input contract. Parsing does no codec/model work.
#pragma once

#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/asr_service.h"
#include "q4t/server/chat_plan.h"

namespace q4t::server {
inline constexpr size_t kAsrFileBytes = 25ULL << 20;
inline constexpr size_t kAsrFormBytes = 32ULL << 20;
inline constexpr size_t kAsrPromptBytes = 64ULL << 10;

enum class AsrFormat { kJson, kText, kSrt, kVerboseJson, kVtt, kDiarizedJson };
struct AsrChunking {
  enum class Mode { kSingle, kAuto, kServerVad };
  Mode mode = Mode::kSingle;
  int prefix_padding_ms = 300;
  int silence_duration_ms = 200;
  double threshold = 0.5;
};
struct AsrRequest {
  // Exact binary source bytes. Metadata is never used as a filesystem path.
  std::string file;
  std::string filename;
  std::string model;
  std::string language;
  std::string prompt;
  std::vector<std::string> languages, keywords;
  std::vector<std::string> speaker_names, speaker_references;
  AsrChunking chunking;
  AsrFormat format = AsrFormat::kJson;
  double temperature = 0;
  bool stream = false;
  bool logprobs = false;
  bool word_timestamps = false;
  bool segment_timestamps = false;
};

// Unknown/duplicate fields and malformed typed values are rejected. Failure
// preserves the previous plan. Parsed advanced fields remain explicit until
// their execution path is selected; no parameter is silently discarded.
Status ParseAsrRequest(std::string_view content_type, std::string_view body,
                       AsrRequest* out, ChatPlanError* error);
std::string_view AsrLanguageName(std::string_view code);
std::string_view AsrLanguageCode(std::string_view name);

struct AsrPrompt {
  std::vector<int32_t> tokens;
  size_t audio_start = 0;
  size_t audio_tokens = 0;
  size_t output_tokens = 0;
};
// special=false is required for the user context. The callback serializes
// the actual tokenizer and supplies request cancellation checkpoints.
using AsrEncode = std::function<Status(std::string_view, bool special,
                                       std::vector<uint32_t>*)>;
Status BuildAsrPrompt(const AsrRequest& request, size_t audio_tokens,
                      const AsrServiceLimits& limits, const AsrEncode& encode,
                      AsrPrompt* out, ChatPlanError* error);
}  // namespace q4t::server
