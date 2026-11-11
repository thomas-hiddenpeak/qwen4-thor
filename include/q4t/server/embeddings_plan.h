// Native pooled final hidden states and the standard embeddings wire contract.
#pragma once

#include <span>

#include "q4t/server/chat_plan.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
inline constexpr size_t kEmbeddingInputs = 2048;
inline constexpr size_t kEmbeddingInputTokens = 8192;
inline constexpr size_t kEmbeddingTotalTokens = 300000;
inline constexpr size_t kEmbeddingDimensions = 2560;
inline constexpr io::JsonParseLimits kEmbeddingInputJsonLimits{
    .max_depth = 64,
    .max_values = 400000,
    .max_string_bytes = kChatBodyBytes,
    .reject_duplicate_keys = true,
    .allow_exact_number_text = true};

struct EmbeddingInput {
  bool token_input = false;
  std::string text;
  std::vector<uint32_t> tokens;
};
struct EmbeddingsRequestPlan {
  std::string model;
  size_t hidden_size = 0, dimensions = 0;
  bool base64 = false;
  std::vector<EmbeddingInput> inputs;
};

Status PlanEmbeddingsRequest(const io::Json& request,
                             const ChatPlanConfig& config,
                             EmbeddingsRequestPlan* plan, ChatPlanError* error);
// Raw model input: no chat template, implicit BOS/EOS, or token truncation.
// Returns every input or none; tokenization has the caller's cancellation.
Status PrepareEmbeddingInputs(const EmbeddingsRequestPlan& plan,
                              const ChatPlanConfig& config,
                              const PromptEncoder& encode,
                              std::vector<std::vector<int32_t>>* inputs,
                              uint64_t* total, ChatPlanError* error);
// Prefix of the actual final-mixer BF16 row, normalized in FP32. This is a
// local numerical transform, not a claim of Matryoshka/retrieval training.
Status NormalizeEmbeddingHidden(std::span<const uint16_t> hidden,
                                size_t dimensions, std::vector<float>* out);
// Bounded direct serialization avoids millions of Json heap objects. Floats
// round-trip as FP32; base64 contains explicitly little-endian FP32 bytes.
Status BuildEmbeddingsResponse(const EmbeddingsRequestPlan& plan,
                               std::span<const std::vector<float>> vectors,
                               uint64_t prompt_tokens, std::string* response);
}  // namespace q4t::server
