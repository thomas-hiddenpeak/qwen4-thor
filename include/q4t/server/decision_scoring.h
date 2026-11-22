#pragma once

#include <functional>
#include <span>

#include "q4t/server/decision_plan.h"

namespace q4t::server {
// Bare and space-prefixed spellings must each be one actual tokenizer token.
struct DecisionLabel {
  std::string text;
  std::vector<uint32_t> tokens;
};
using DecisionEncoder =
    std::function<Status(std::string_view, std::vector<uint32_t>*)>;
Status MakeDecisionLabels(size_t options, const DecisionEncoder& encode,
                          std::vector<DecisionLabel>* labels);
// Construct shared evidence once, then append one question per native prefill.
Status DecisionSharedMessages(const io::Json& request, io::Json* messages);
Status DecisionStepRequest(const io::Json& request, const io::Json& shared,
                           const io::Json& question,
                           std::span<const DecisionLabel> labels,
                           io::Json* step);
// The last label is refusal. Score the first generation position using the
// real BF16 vocabulary row. Selection and probability use the same row.
struct DecisionScore {
  const io::Json& question;
  std::span<const DecisionLabel> labels;
  io::Json answer;
  int32_t token = -1;
  Status Read(std::span<const uint16_t> logits);
};
}  // namespace q4t::server
