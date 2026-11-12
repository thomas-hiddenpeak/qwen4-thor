// Native retrieval primitives. Ownership and immutable index publication are
// enforced by the object-store caller, never inferred from document metadata.
#pragma once

#include <chrono>
#include <functional>
#include <span>

#include "q4t/server/chat_plan.h"

namespace q4t::server {
inline constexpr size_t kVectorSearchQueries = 16;
inline constexpr size_t kVectorSearchStores = 16;
inline constexpr size_t kVectorSearchChunks = 4096;
inline constexpr size_t kVectorSearchDimensions = 4096;
inline constexpr size_t kVectorSearchValues = 8ULL << 20;
inline constexpr size_t kVectorSearchTextBytes = 16ULL << 20;

struct VectorChunking {
  size_t max_tokens = 800;
  size_t overlap_tokens = 400;
};
struct VectorSearchPlan {
  std::vector<std::string> queries;
  io::Json filters;
  size_t max_results = 10;
  std::string ranker = "auto";
  double score_threshold = 0;
  bool rewrite_query = false;
  // Responses file_search hybrid ranking. The standalone search planner
  // retains its public default weights; no hidden change to old scores.
  double embedding_weight = 1;
  double text_weight = 1;
};
struct VectorSearchChunk {
  std::string file_id, filename, chunk_id, text;
  io::Json attributes;
  std::vector<float> embedding;
};
struct VectorSearchQuery {
  // Actual rewritten text when requested; this layer never fabricates it.
  std::string text;
  std::vector<float> embedding;
};
struct VectorSearchHit {
  size_t chunk = 0;
  double score = 0;
};
struct VectorSearchControl {
  std::chrono::steady_clock::time_point deadline =
      std::chrono::steady_clock::time_point::max();
  std::function<bool()> cancelled;
};

Status ParseVectorChunking(const io::Json& value, VectorChunking* result,
                           ChatPlanError* error,
                           std::string_view path = "chunking_strategy");
Status ValidateVectorAttributes(const io::Json& attributes,
                                ChatPlanError* error,
                                std::string_view path = "attributes");
Status ValidateVectorFilter(const io::Json& filter, ChatPlanError* error,
                            std::string_view path = "filters");
Status MatchVectorFilter(const io::Json& filter, const io::Json& attributes,
                         bool* matches, ChatPlanError* error);
Status PlanVectorSearch(const io::Json& request, VectorSearchPlan* plan,
                        ChatPlanError* error);

// Validates the full bounded candidate set before publishing any hits. Every
// vector must be real, finite and nonzero, of one dimension. "none" ranks by
// cosine. "auto"/the dated contract use native cosine + BM25 reciprocal-rank
// fusion, not a claim to reproduce OpenAI's private ranker or score values.
// Attribute filtering precedes ranking; ties use stable file/chunk identities.
Status RankVectorSearch(const VectorSearchPlan& plan,
                        std::span<const VectorSearchQuery> queries,
                        std::span<const VectorSearchChunk> chunks,
                        const VectorSearchControl& control,
                        std::vector<VectorSearchHit>* hits,
                        ChatPlanError* error);
}  // namespace q4t::server
