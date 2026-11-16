#pragma once

#include <cstdint>
#include <string>

namespace q4t::server {
enum class ModerationMode { kScore, kBlock };
enum class ModerationDecision { kAllow, kBlocked, kUnavailable };

struct ModerationUsage {
  int64_t input_tokens = 0;
  int64_t output_tokens = 0;
  int64_t model_steps = 0;
};

struct ModerationOptions {
  bool enabled = false;
  std::string model;
  ModerationMode input = ModerationMode::kScore;
  ModerationMode output = ModerationMode::kScore;
};
}  // namespace q4t::server
