#include "q4t/server/vector_search_execution.h"

#include <algorithm>

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
  if (!callbacks.reserve || !callbacks.encode || !callbacks.embed ||
      (plan.rewrite_query && !callbacks.rewrite))
    return Error(503, "vector_search_backend_unavailable",
                 "Native search execution is unavailable");
  auto active = Active(access);
  if (!active.ok()) return active;
  VectorStores stores(objects);
  VectorSearchCorpus corpus;
  auto reply =
      stores.ReadSearchCorpus(access, id, plan.filters, dimensions, &corpus);
  if (!reply.ok()) return reply;
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
  if (!corpus.chunks.empty()) {
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
  if (!corpus.chunks.empty()) {
    std::vector<VectorSearchHit> hits;
    status =
        RankVectorSearch(plan, vectors, corpus.chunks, control, &hits, &error);
    if (!status) return PlanningError(status, error);
    for (const auto& hit : hits) {
      const auto& chunk = corpus.chunks[hit.chunk];
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
  reply = stores.TouchSearch(access, corpus);
  if (!reply.ok()) return reply;
  reply.value = Object({{"object", String("vector_store.search_results.page")},
                        {"search_query", QueryArray(queries)},
                        {"data", std::move(rows)},
                        {"has_more", Boolean(false)},
                        {"next_page", Json()}});
  return reply;
}
}  // namespace q4t::server
