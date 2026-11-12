#include "q4t/server/vector_search.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <numeric>
#include <set>
#include <tuple>

#include "q4t/server/application_routes.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using Terms = std::map<std::string, size_t>;
constexpr size_t kTerms = 1ULL << 20;
Status Failure(ChatPlanError* error, std::string code, std::string message,
               int http = 503) {
  *error = {std::move(code), "query", http};
  return Status::Fail(std::move(message));
}
Status Active(const VectorSearchControl& control, ChatPlanError* error) {
  if (std::chrono::steady_clock::now() >= control.deadline)
    return Failure(error, "request_deadline_exceeded",
                   "Vector search deadline exceeded", 408);
  if (control.cancelled && control.cancelled())
    return Failure(error, "request_cancelled", "Vector search cancelled", 499);
  return {};
}
Status Resource(ChatPlanError* error) {
  return Failure(error, "vector_search_resource_exhausted",
                 "Vector search working set exceeds its budget");
}
bool Norm(std::span<const float> vector, size_t dimensions, double* norm) {
  if (vector.size() != dimensions) return false;
  double squared = 0;
  for (const auto value : vector) {
    if (!std::isfinite(value)) return false;
    squared += double(value) * value;
  }
  *norm = std::sqrt(squared);
  return std::isfinite(*norm) && *norm > 0;
}
// ASCII words are case-folded. Non-ASCII code points and adjacent pairs keep
// Chinese text searchable without another runtime tokenizer or word database.
// This is the sparse channel only; trained embeddings provide semantic search.
Status Lexical(std::string_view text, Terms* terms, size_t* remaining,
               const VectorSearchControl& control, ChatPlanError* error) {
  std::string word, previous;
  const auto add = [&](const std::string& term) {
    if (term.empty()) return true;
    if (!*remaining) return false;
    --*remaining;
    ++(*terms)[term];
    return true;
  };
  size_t next_check = 0;
  for (size_t i = 0; i < text.size();) {
    if (i >= next_check) {
      const auto active = Active(control, error);
      if (!active) return active;
      next_check = i + 4096;
    }
    const auto c = static_cast<unsigned char>(text[i]);
    if (c < 128) {
      previous.clear();
      if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '_') {
        word += c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c;
      } else {
        if (!add(word)) return Resource(error);
        word.clear();
      }
      ++i;
    } else {
      if (!add(word)) return Resource(error);
      word.clear();
      const size_t bytes = c < 0xe0 ? 2 : c < 0xf0 ? 3 : 4;
      const std::string current(text.substr(i, bytes));
      if (!add(current) || (!previous.empty() && !add(previous + current)))
        return Resource(error);
      previous = current;
      i += bytes;
    }
  }
  return add(word) ? Status{} : Resource(error);
}
}  // namespace

Status RankVectorSearch(const VectorSearchPlan& plan,
                        std::span<const VectorSearchQuery> queries,
                        std::span<const VectorSearchChunk> chunks,
                        const VectorSearchControl& control,
                        std::vector<VectorSearchHit>* hits,
                        ChatPlanError* error) {
  hits->clear();
  *error = {};
  auto status = Active(control, error);
  if (!status) return status;
  const auto invalid = [&] {
    return Failure(error, "vector_index_invalid",
                   "Invalid or inconsistent native retrieval inputs");
  };
  if (queries.empty() || queries.size() != plan.queries.size() ||
      !plan.max_results || plan.max_results > 50 ||
      !std::isfinite(plan.embedding_weight) || plan.embedding_weight < 0 ||
      !std::isfinite(plan.text_weight) || plan.text_weight < 0 ||
      (plan.embedding_weight == 0 && plan.text_weight == 0) ||
      !std::isfinite(plan.score_threshold) || plan.score_threshold < 0 ||
      plan.score_threshold > 1 ||
      (plan.ranker != "none" && plan.ranker != "auto" &&
       plan.ranker != "default-2024-11-15"))
    return invalid();
  if (queries.size() > kVectorSearchQueries ||
      chunks.size() > kVectorSearchChunks)
    return Resource(error);
  status = ValidateVectorFilter(plan.filters, error);
  if (!status) return status;
  const auto dimensions = queries[0].embedding.size();
  if (!dimensions || dimensions > kVectorSearchDimensions) return invalid();
  size_t values = 0, bytes = 0, term_budget = kTerms;
  const auto budget = [&](size_t count, size_t text) {
    if (count > kVectorSearchValues - values ||
        text > kVectorSearchTextBytes - bytes)
      return false;
    values += count;
    bytes += text;
    return true;
  };
  const bool hybrid = plan.ranker != "none";
  std::vector<double> query_norms(queries.size());
  std::vector<Terms> query_terms(queries.size());
  size_t query_bytes = 0;
  for (size_t i = 0; i < queries.size(); ++i) {
    const auto& query = queries[i];
    status = Active(control, error);
    if (!status) return status;
    if (query.text.size() > (64ULL << 10) - query_bytes ||
        !budget(query.embedding.size(), query.text.size()))
      return Resource(error);
    query_bytes += query.text.size();
    if (query.text.empty() || detail::NormalizeUtf8(query.text) != query.text ||
        (!plan.rewrite_query && query.text != plan.queries[i]) ||
        !Norm(query.embedding, dimensions, &query_norms[i]))
      return invalid();
    if (hybrid) {
      status =
          Lexical(query.text, &query_terms[i], &term_budget, control, error);
      if (!status) return status;
    }
  }
  std::vector<size_t> selected;
  std::vector<double> norms(chunks.size()), lengths(chunks.size());
  std::vector<Terms> terms(chunks.size());
  std::map<std::string, size_t> frequency;
  std::set<std::pair<std::string, std::string>> identities;
  double total_length = 0;
  for (size_t i = 0; i < chunks.size(); ++i) {
    status = Active(control, error);
    if (!status) return status;
    const auto& chunk = chunks[i];
    if (!budget(chunk.embedding.size(), chunk.text.size()))
      return Resource(error);
    if (!ValidApplicationId(chunk.file_id) ||
        !ValidApplicationId(chunk.chunk_id) || chunk.filename.empty() ||
        chunk.filename.size() > 1024 ||
        detail::NormalizeUtf8(chunk.filename) != chunk.filename ||
        chunk.text.empty() || detail::NormalizeUtf8(chunk.text) != chunk.text ||
        !identities.emplace(chunk.file_id, chunk.chunk_id).second ||
        !Norm(chunk.embedding, dimensions, &norms[i]))
      return invalid();
    bool matches = false;
    if (!MatchVectorFilter(plan.filters, chunk.attributes, &matches, error))
      return invalid();
    if (!budget(0, chunk.filename.size() + chunk.file_id.size() +
                       chunk.chunk_id.size()))
      return Resource(error);
    for (const auto& [key, value] : chunk.attributes.object)
      if (!budget(0, key.size() + value.str.size() + 64))
        return Resource(error);
    if (!matches) continue;
    selected.push_back(i);
    if (hybrid) {
      status = Lexical(chunk.text, &terms[i], &term_budget, control, error);
      if (!status) return status;
      for (const auto& [term, count] : terms[i]) {
        ++frequency[term];
        lengths[i] += count;
      }
      total_length += lengths[i];
    }
  }
  const auto identity_less = [&](size_t a, size_t b) {
    return std::tie(chunks[a].file_id, chunks[a].chunk_id) <
           std::tie(chunks[b].file_id, chunks[b].chunk_id);
  };
  const double average = selected.empty() ? 1 : total_length / selected.size();
  std::vector<double> scores(chunks.size());
  std::vector<size_t> order = selected;
  for (size_t q = 0; q < queries.size(); ++q) {
    std::vector<double> cosine(chunks.size()), lexical(chunks.size());
    for (const auto i : selected) {
      status = Active(control, error);
      if (!status) return status;
      double dot = 0;
      for (size_t d = 0; d < dimensions; ++d)
        dot += double(queries[q].embedding[d]) * chunks[i].embedding[d];
      cosine[i] = std::clamp(dot / (query_norms[q] * norms[i]), 0.0, 1.0);
      if (!hybrid) {
        scores[i] = std::max(scores[i], cosine[i]);
        continue;
      }
      for (const auto& [term, ignored] : query_terms[q]) {
        (void)ignored;
        const auto it = terms[i].find(term);
        if (it == terms[i].end()) continue;
        const double df = frequency.at(term), tf = it->second;
        const double idf =
            std::log1p((selected.size() - df + 0.5) / (df + 0.5));
        lexical[i] +=
            idf * (tf * 2.2) /
            (tf + 1.2 * (0.25 + 0.75 * lengths[i] / std::max(average, 1.0)));
      }
    }
    if (hybrid) {
      // Two nonnegative real evidence channels per query. Dividing by the
      // theoretical best reciprocal rank bounds scores to [0,1]. A zero
      // evidence channel contributes nothing, including at threshold zero.
      size_t channel_index = 0;
      const double scale = std::max(plan.embedding_weight, plan.text_weight);
      const double weights[] = {plan.embedding_weight / scale,
                                 plan.text_weight / scale};
      for (const auto* channel : {&cosine, &lexical}) {
        const auto weight = weights[channel_index++];
        if (!weight) continue;
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
          return (*channel)[a] == (*channel)[b] ? identity_less(a, b)
                                                : (*channel)[a] > (*channel)[b];
        });
        for (size_t rank = 0; rank < order.size(); ++rank)
          if ((*channel)[order[rank]] > 0)
            scores[order[rank]] += plan.embedding_weight == plan.text_weight
                ? 61.0 / ((61.0 + rank) * 2.0 * queries.size())
                : (61.0 / ((61.0 + rank) * queries.size())) *
                      (weight / (weights[0] + weights[1]));
      }
    }
  }
  status = Active(control, error);
  if (!status) return status;
  std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
    return scores[a] == scores[b] ? identity_less(a, b) : scores[a] > scores[b];
  });
  std::vector<VectorSearchHit> candidate;
  for (const auto i : order) {
    const auto score = std::clamp(scores[i], 0.0, 1.0);
    if (score > 0 && score >= plan.score_threshold)
      candidate.push_back({i, score});
    if (candidate.size() == plan.max_results) break;
  }
  status = Active(control, error);
  if (!status) return status;
  *hits = std::move(candidate);
  return {};
}
}  // namespace q4t::server
