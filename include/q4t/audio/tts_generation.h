// Native TTS conditioning and generation control; no CUDA dependencies.
#pragma once

#include <array>
#include <random>
#include <span>
#include <string_view>

#include "q4t/audio/tts_config.h"

namespace q4t::audio {

struct TtsPromptRow {
  int32_t text_index = 0;
  int32_t codec_id = -1;  // -1 means pure projected text.
};

struct TtsPrompt {
  std::vector<int32_t> text_ids;
  std::vector<TtsPromptRow> prefill;
  std::vector<int32_t> trailing_text;
  int32_t pad_index = 0;
};

// Inputs are literal BPE content from the TTS tokenizer (special recognition
// disabled). Structural role/turn markers are inserted here, not by callers.
// This retains spoken control-like text without permitting template injection.
Status PlanTtsPrompt(TtsVariant variant, std::span<const int32_t> text,
                     std::span<const int32_t> instruction,
                     std::string_view voice, std::string_view language,
                     bool non_streaming, size_t max_context, TtsPrompt* out);
Status ValidateTtsPrompt(const TtsPrompt& prompt);

struct TtsSampling {
  bool sample = true;
  float temperature = 0.9f;
  int top_k = 50;
  float top_p = 1.0f;
  float repetition_penalty = 1.05f;
};

// FP32 head logits retain their precision through penalties/temperature.
// The BF16 overload expands legacy operands exactly. RNG is per request; IDs
// uncommitted until an entire 16-code frame has been generated successfully.
class TtsCodeSampler {
 public:
  Status Init(const TtsSampling& options, uint64_t seed);
  Status Select(std::span<const uint16_t> logits, bool group_zero,
                 bool allow_eos, std::span<const int32_t> history,
                 int32_t* token);
  Status Select(std::span<const float> logits, bool group_zero,
                 bool allow_eos, std::span<const int32_t> history,
                 int32_t* token);

 private:
  TtsSampling options_;
  std::mt19937_64 random_;
  bool initialized_ = false;
};

using TtsCodes = std::array<int32_t, TtsConfig::kCodeGroups>;

class TtsGenerationState {
 public:
  Status Init(size_t max_frames);
  Status Accept(size_t group, int32_t token);
  size_t next_group() const { return next_group_; }
  size_t frames() const { return frames_.size(); }
  bool allow_eos() const { return frames_.size() >= 2; }
  bool ended() const { return eos_; }
  bool at_limit() const { return initialized_ && frames() == max_frames_; }
  const TtsCodes& pending() const { return pending_; }
  std::span<const TtsCodes> completed() const { return frames_; }
  std::span<const int32_t> first_code_history() const { return first_codes_; }

 private:
  std::vector<TtsCodes> frames_;
  std::vector<int32_t> first_codes_;
  TtsCodes pending_{};
  size_t max_frames_ = 0, next_group_ = 0;
  bool initialized_ = false, eos_ = false;
};

}  // namespace q4t::audio
