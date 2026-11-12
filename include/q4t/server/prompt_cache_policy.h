#pragma once

#include <cstddef>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

struct PromptCacheOptions {
  bool requested = false;
  bool explicit_only = false;
  bool prewarm = false;
  bool modern_controls = false;
  bool retention_requested = false;
  int ttl_seconds = 1800;
  std::string retention = "in_memory";
  std::string key;
  std::string comparison_response_id;
};

// Shared policy, without storage or model work. The enclosing API separately
// validates its top-level fields. Null optional fields preserve defaults.
Status ParsePromptCacheOptions(const io::Json& request,
                               PromptCacheOptions* options,
                               std::string* error_param);
io::Json PublicPromptCacheOptions(const PromptCacheOptions& options);
struct SessionCacheLifetime;
SessionCacheLifetime PromptCacheLifetime(const PromptCacheOptions& options,
                                         int write_ttl, bool responses_api);
// An omitted/null block breakpoint has ttl_seconds=0 and present=false.
Status ParsePromptCacheBreakpoint(const io::Json* value, std::string_view path,
                                  bool* present, int* ttl_seconds,
                                  std::string* error_param);

struct PromptCacheBoundary {
  enum class Kind { kExplicit, kEligibleMessage, kInitialInstructions };
  int tokens = 0;
  int ttl_seconds = 0;  // Inherit request TTL when zero.
  Kind kind = Kind::kExplicit;
};

struct PromptCacheWrite {
  int tokens = 0;
  int ttl_seconds = 1800;
};

struct PromptCachePlan {
  std::vector<int> reads;  // Sorted, unique eligible exact token boundaries.
  std::vector<PromptCacheWrite> writes;  // At most four, in token order.
};

struct PromptCacheInputBoundary {
  size_t message_index = 0;
  size_t part_index = 0;
  int ttl_seconds = 0;
};
struct ChatPromptLayout;
Status PlanPromptCacheLayout(
    const PromptCacheOptions& options, const io::Json& messages,
    const ChatPromptLayout& layout,
    std::span<const PromptCacheInputBoundary> explicit_boundaries,
    std::span<const size_t> token_starts, size_t prompt_bytes,
    PromptCachePlan* plan);

// Input boundaries refer to the final rendered/tokenized context, after any
// truncation/compaction. No guessed character counts or partial-token states.
Status PlanPromptCache(const PromptCacheOptions& options,
                       std::span<const PromptCacheBoundary> boundaries,
                       int prompt_tokens, PromptCachePlan* plan);

// Token starts come from Tokenizer::EchoByteOffsets on the actual full prompt.
// NFC clusters may have repeated starts; never reuse a partial source cluster
// when a block boundary falls inside it. No tokenizer vocabulary is assumed.
Status CompletePromptPrefix(std::span<const size_t> token_starts,
                            size_t prompt_bytes, size_t boundary_bytes,
                            int* tokens);

}  // namespace q4t::server
