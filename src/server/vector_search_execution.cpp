#include "q4t/server/vector_search_execution.h"

#include <algorithm>
#include <cmath>
#include <set>

#include "q4t/server/application_routes.h"

#include "object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
using namespace object_detail;
namespace {
ObjectReply Active(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Search cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Search deadline exceeded");
  return {};
}
ObjectReply PlanningError(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
ObjectReply InvalidResult() {
  return Error(502, "embedding_result_invalid",
               "Native search embedding did not complete valid input");
}
Json QueryArray(const std::vector<std::string>& queries) {
  auto output = Array();
  for (const auto& query : queries) output.array.push_back(String(query));
  return output;
}
}  // namespace

ObjectReply ExecuteVectorSearchRequest(ObjectStore& objects,
                                       const BinaryAccess& access,
                                       const std::string& id,
                                       const Json& request, size_t dimensions,
                                       const VectorSearchCallbacks& callbacks,
                                       VectorSearchUsage* usage) {
  *usage = {};
  VectorSearchPlan plan;
  ChatPlanError error;
  auto status = PlanVectorSearch(request, &plan, &error);
  if (!status) return PlanningError(status, error);
  return ExecuteVectorSearchPlan(objects, access, {id}, plan, dimensions,
                                 callbacks, usage);
}

ObjectReply ExecuteVectorSearchPlan(
    ObjectStore& objects, const BinaryAccess& access,
    const std::vector<std::string>& store_ids, const VectorSearchPlan& plan,
    size_t dimensions, const VectorSearchCallbacks& callbacks,
    VectorSearchUsage* usage) {
  *usage = {};
  // Validate trusted plans too: empty corpora must not bypass query/control
  // checks merely because ranking would have no work to perform.
  Json threshold;
  threshold.type = Json::Type::kNumber;
  threshold.number = plan.score_threshold;
  VectorSearchPlan checked;
  ChatPlanError error;
  auto status = PlanVectorSearch(
      Object({{"query", QueryArray(plan.queries)},
              {"filters", plan.filters},
              {"max_num_results", Number(plan.max_results)},
              {"rewrite_query", Boolean(plan.rewrite_query)},
              {"ranking_options", Object({{"ranker", String(plan.ranker)},
                                          {"score_threshold", threshold}})}}),
      &checked, &error);
  if (!status) return PlanningError(status, error);
  if (!std::isfinite(plan.embedding_weight) || plan.embedding_weight < 0 ||
      !std::isfinite(plan.text_weight) || plan.text_weight < 0 ||
      (plan.embedding_weight == 0 && plan.text_weight == 0))
    return Error(400, "invalid_request", "Invalid hybrid ranking weights");
  if (store_ids.empty())
    return Error(400, "invalid_request", "Expected vector store IDs");
  if (store_ids.size() > kVectorSearchStores)
    return Error(503, "vector_search_resource_exhausted",
                 "Vector store working set exceeds request budget");
  std::set<std::string> ids;
  for (const auto& id : store_ids)
    if (!ValidApplicationId(id) || !id.starts_with("vs_") ||
        !ids.insert(id).second)
      return Error(400, "invalid_request", "Invalid or duplicate vector store ID");
  if (!callbacks.reserve || !callbacks.encode || !callbacks.embed ||
      (plan.rewrite_query && !callbacks.rewrite))
    return Error(503, "vector_search_backend_unavailable",
                 "Native search execution is unavailable");
  auto active = Active(access);
  if (!active.ok()) return active;
  VectorStores stores(objects);
  std::vector<VectorSearchCorpus> corpora;
  std::vector<VectorSearchChunk> chunks;
  std::set<std::pair<std::string, std::string>> identities;
  size_t encountered = 0, values = 0, bytes = 0;
  ObjectReply reply;
  const auto capacity = [] {
    return Error(503, "vector_search_resource_exhausted",
                 "Complete multi-store search corpus exceeds bounded capacity");
  };
  for (const auto& id : store_ids) {
    VectorSearchCorpus corpus;
    reply = stores.ReadSearchCorpus(access, id, plan.filters, dimensions,
                                    &corpus);
    if (!reply.ok()) return reply;
    // Charge all inspected memberships, including duplicates. Reading many
    // aliases cannot evade the aggregate work bound. Only one temporary
    // corpus and the bounded combined candidate set are retained at a time.
    if (corpus.chunks.size() > kVectorSearchChunks - encountered)
      return capacity();
    encountered += corpus.chunks.size();
    for (auto& chunk : corpus.chunks) {
      const auto metadata = chunk.file_id.size() + chunk.filename.size() +
                            chunk.chunk_id.size() +
                            OutputJson(chunk.attributes).size();
      if (chunk.embedding.size() > kVectorSearchValues - values ||
          metadata > kVectorSearchTextBytes - bytes)
        return capacity();
      values += chunk.embedding.size();
      bytes += metadata;
      if (chunk.text.size() > kVectorSearchTextBytes - bytes) return capacity();
      bytes += chunk.text.size();
      if (identities.emplace(chunk.file_id, chunk.chunk_id).second)
        chunks.push_back(std::move(chunk));
    }
    std::vector<VectorSearchChunk>().swap(corpus.chunks);
    corpora.push_back(std::move(corpus));
  }
  auto queries = plan.queries;
  if (plan.rewrite_query) {
    queries.clear();
    reply = callbacks.rewrite(plan.queries, &queries);
    if (!reply.ok()) return reply;
    VectorSearchPlan checked;
    status = PlanVectorSearch(Object({{"query", QueryArray(queries)}}),
                              &checked, &error);
    if (!status || queries.size() != plan.queries.size())
      return Error(502, "vector_query_rewrite_invalid",
                   "Model query rewrite is incomplete or invalid");
  }
  const VectorSearchControl control{access.deadline, access.cancelled};
  std::vector<std::vector<int32_t>> inputs;
  uint64_t total = 0;
  // An empty, fully checked corpus has no vectors to compare. Still perform
  // requested rewriting and request admission; do not manufacture vectors.
  if (!chunks.empty()) {
    for (const auto& query : queries) {
      active = Active(access);
      if (!active.ok()) return active;
      VectorEncodedText encoded;
      reply = callbacks.encode(std::string(kVectorQueryInstruction) + query,
                               control, &encoded);
      if (!reply.ok()) return reply;
      if (encoded.tokens.empty() ||
          encoded.tokens.size() >= model::EmbeddingConfig::kInputTokens) {
        auto invalid = Error(400, "vector_query_token_limit_exceeded",
                             "Search query exceeds native token budget");
        invalid.value = Object({{"param", String("query")}});
        return invalid;
      }
      if (std::any_of(encoded.tokens.begin(), encoded.tokens.end(),
                      [](uint32_t token) {
                        return token >= model::EmbeddingConfig::kVocab;
                      }))
        return Error(502, "vector_query_encoding_invalid",
                     "Native query tokenizer returned invalid token IDs");
      inputs.emplace_back(encoded.tokens.begin(), encoded.tokens.end());
      inputs.back().push_back(model::EmbeddingConfig::kEos);
      total += inputs.back().size();
    }
  }
  active = Active(access);
  if (!active.ok()) return active;
  reply = callbacks.reserve(total);
  if (!reply.ok()) return reply;
  usage->admitted_input = total;
  std::vector<VectorSearchQuery> vectors;
  for (size_t i = 0; i < inputs.size(); ++i) {
    active = Active(access);
    if (!active.ok()) return active;
    const auto& tokens = inputs[i];
    EmbeddingStepResult step;
    ScopeExit receipt([&] {
      if (usage->unknown || step.committed_input > tokens.size() ||
          step.committed_input > total - usage->committed_input) {
        usage->unknown = true;
      } else {
        usage->committed_input += step.committed_input;
      }
    });
    reply = callbacks.embed(tokens, &step);
    if (!reply.ok()) return reply;
    if (step.committed_input != tokens.size() ||
        step.hidden.size() != model::EmbeddingConfig::kHidden)
      return InvalidResult();
    VectorSearchQuery vector{queries[i], {}};
    status =
        NormalizeEmbeddingHidden(step.hidden, dimensions, &vector.embedding);
    if (!status) return InvalidResult();
    vectors.push_back(std::move(vector));
  }
  auto rows = Array();
  if (!chunks.empty()) {
    std::vector<VectorSearchHit> hits;
    status =
        RankVectorSearch(plan, vectors, chunks, control, &hits, &error);
    if (!status) return PlanningError(status, error);
    for (const auto& hit : hits) {
      const auto& chunk = chunks[hit.chunk];
      Json score;
      score.type = Json::Type::kNumber;
      score.number = hit.score;
      rows.array.push_back(Object(
          {{"file_id", String(chunk.file_id)},
           {"filename", String(chunk.filename)},
           {"score", score},
           {"attributes", chunk.attributes},
           {"content", Array({Object({{"type", String("text")},
                                      {"text", String(chunk.text)}})})}}));
    }
  }
  for (const auto& corpus : corpora) {
    reply = stores.TouchSearch(access, corpus);
    if (!reply.ok()) return reply;
  }
  reply.value = Object({{"object", String("vector_store.search_results.page")},
                        {"search_query", QueryArray(queries)},
                        {"data", std::move(rows)},
                        {"has_more", Boolean(false)},
                        {"next_page", Json()}});
  return reply;
}
}  // namespace q4t::server
