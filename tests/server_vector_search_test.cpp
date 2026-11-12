#include "q4t/server/vector_search.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

#include "../src/server/responses_json.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::responses_detail;
Json Read(const std::string& text) {
  Json result;
  q4t::io::JsonParseLimits limits;
  limits.reject_duplicate_keys = true;
  limits.allow_exact_number_text = true;
  if (!q4t::io::ParseJson(text, &result, true, limits))
    throw std::runtime_error("vector search fixture JSON");
  return result;
}
VectorSearchPlan Plan(const std::string& text = R"({"query":"PLE SSD"})") {
  VectorSearchPlan plan;
  ChatPlanError error;
  if (!PlanVectorSearch(Read(text), &plan, &error))
    throw std::runtime_error("vector search fixture: " + error.param);
  return plan;
}
VectorSearchChunk Chunk(std::string id, std::string text,
                        std::vector<float> vector) {
  return {"file-records",  "MODEL.md", std::move(id),
          std::move(text), Object(),   std::move(vector)};
}
std::vector<VectorSearchChunk> Corpus() {
  return {Chunk("ple", "PLE SSD experts from NVMe", {3, 4, 0}),
          Chunk("tls", "Native TLS gateway certificates", {0, 0, 5}),
          Chunk("negative", "Unrelated evidence", {-3, -4, 0})};
}
bool Matches(const Json& filter, const Json& attrs) {
  bool match = false;
  ChatPlanError error;
  if (!MatchVectorFilter(filter, attrs, &match, &error))
    throw std::runtime_error("vector filter fixture: " + error.param);
  return match;
}
}  // namespace

Q4T_TEST(vector_search_plan_preserves_queries_defaults_and_rewrite_intent) {
  auto plan = Plan();
  Q4T_CHECK(plan.queries == std::vector<std::string>{"PLE SSD"} &&
            plan.max_results == 10 && plan.ranker == "auto" &&
            !plan.rewrite_query && plan.filters.IsNull());
  plan = Plan(R"({"query":["PLE SSD","原生入口"],"filters":null,
    "rewrite_query":true,"max_num_results":50,
    "ranking_options":{"ranker":"default-2024-11-15",
                       "score_threshold":0.5}})");
  Q4T_CHECK(plan.queries.size() == 2 && plan.queries[1] == "原生入口" &&
            plan.rewrite_query && plan.max_results == 50 &&
            plan.score_threshold == 0.5);
  return true;
}

Q4T_TEST(vector_search_plan_rejects_bad_controls_without_partial_plan) {
  for (
      const auto& text :
      {R"({"query":null})", R"({"query":[]})", R"({"query":""})",
       R"({"query":["ok",1]})", R"({"query":"x","rewrite_query":null})",
       R"({"query":"x","max_num_results":1.0000000000000000001})",
       R"({"query":"x","max_num_results":51})",
       R"({"query":"x","ranking_options":null})",
       R"({"query":"x","ranking_options":{"ranker":"invented"}})",
       R"({"query":"x","ranking_options":{"score_threshold":1.00000000000000000001}})",
       R"({"query":"x","ranking_options":{"score_threshold":-1e-500}})",
       R"({"query":"x","metadata":{}})"}) {
    auto plan = Plan();
    ChatPlanError error;
    Q4T_CHECK(!PlanVectorSearch(Read(text), &plan, &error) &&
              !error.param.empty() && plan.queries.empty());
  }
  auto duplicate = Read(R"({"query":"x"})");
  duplicate.object.emplace_back("query", String("y"));
  VectorSearchPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanVectorSearch(duplicate, &plan, &error));
  return true;
}

Q4T_TEST(vector_search_query_work_limits_are_resource_errors) {
  VectorSearchPlan plan;
  ChatPlanError error;
  auto query = Array();
  query.array.assign(kVectorSearchQueries + 1, String("x"));
  Q4T_CHECK(!PlanVectorSearch(Object({{"query", query}}), &plan, &error) &&
            error.http_status == 503 && plan.queries.empty());
  query = Array({String(std::string(65536, 'a')), String("b")});
  Q4T_CHECK(!PlanVectorSearch(Object({{"query", query}}), &plan, &error) &&
            error.code == "vector_search_resource_exhausted");
  query = String(std::string("a\xff", 2));
  Q4T_CHECK(!PlanVectorSearch(Object({{"query", query}}), &plan, &error) &&
            error.http_status == 400);
  return true;
}

Q4T_TEST(vector_search_chunking_obeys_exact_size_overlap_contract) {
  VectorChunking chunk;
  ChatPlanError error;
  Q4T_CHECK(ParseVectorChunking(Read(R"({"type":"auto"})"), &chunk, &error) &&
            chunk.max_tokens == 800 && chunk.overlap_tokens == 400);
  Q4T_CHECK(ParseVectorChunking(Read(R"({"type":"static","static":{
    "max_chunk_size_tokens":4096,"chunk_overlap_tokens":2048}})"),
                                &chunk, &error) &&
            chunk.max_tokens == 4096);
  for (
      const auto& params :
      {R"({"max_chunk_size_tokens":99,"chunk_overlap_tokens":0})",
       R"({"max_chunk_size_tokens":4097,"chunk_overlap_tokens":0})",
       R"({"max_chunk_size_tokens":100,"chunk_overlap_tokens":51})",
       R"({"max_chunk_size_tokens":100,"chunk_overlap_tokens":-1})",
       R"({"max_chunk_size_tokens":100.000000000000001,"chunk_overlap_tokens":0})",
       R"({"max_chunk_size_tokens":100})"}) {
    Q4T_CHECK(!ParseVectorChunking(
        Object({{"type", String("static")}, {"static", Read(params)}}), &chunk,
        &error));
    Q4T_CHECK(chunk.max_tokens == 800 && chunk.overlap_tokens == 400);
  }
  Q4T_CHECK(!ParseVectorChunking(Read(R"({"type":"auto","static":{}})"), &chunk,
                                 &error));
  return true;
}

Q4T_TEST(vector_search_attributes_count_unicode_not_bytes) {
  ChatPlanError error;
  std::string key, value;
  for (int i = 0; i < 64; ++i) key += "键";
  for (int i = 0; i < 512; ++i) value += "值";
  auto attrs = Object({{key, String(value)},
                       {"private", Boolean(true)},
                       {"big", Read("9007199254740993")}});
  Q4T_CHECK(ValidateVectorAttributes(attrs, &error));
  attrs.object[0].second.str += "值";
  Q4T_CHECK(!ValidateVectorAttributes(attrs, &error));
  attrs.object[0].second.str = value;
  attrs.object[0].first += "键";
  Q4T_CHECK(!ValidateVectorAttributes(attrs, &error));
  attrs = Object({{"a", Json{}}});
  Q4T_CHECK(!ValidateVectorAttributes(attrs, &error));
  Q4T_CHECK(ValidateVectorAttributes(Json{}, &error));
  attrs = Object({{"a", Number(1)}, {"a", Number(2)}});
  Q4T_CHECK(!ValidateVectorAttributes(attrs, &error));
  return true;
}

Q4T_TEST(vector_search_filter_compares_exact_decimals_and_distinct_types) {
  auto attrs = Read(R"({"n":9007199254740993,"b":true,"s":"1"})");
  Q4T_CHECK(Matches(Read(R"({"key":"n","value":9007199254740993})"), attrs));
  Q4T_CHECK(!Matches(Read(R"({"key":"n","value":9007199254740992})"), attrs));
  Q4T_CHECK(Matches(Read(R"({"type":"gt","key":"n","value":9007199254740992})"),
                    attrs));
  Q4T_CHECK(Matches(
      Read(R"({"type":"gte","key":"n","value":9007199254740993.0})"), attrs));
  Q4T_CHECK(!Matches(Read(R"({"type":"eq","key":"b","value":1})"), attrs));
  Q4T_CHECK(!Matches(Read(R"({"type":"eq","key":"s","value":1})"), attrs));
  for (const auto* op : {"ne", "nin"})
    Q4T_CHECK(
        !Matches(Object({{"type", String(op)},
                         {"key", String("absent")},
                         {"value", std::string(op) == "nin" ? Array({Number(1)})
                                                            : Number(1)}}),
                 attrs));
  attrs = Read(R"({"n":1e500})");
  Q4T_CHECK(Matches(Read(R"({"type":"lt","key":"n","value":2e500})"), attrs));
  return true;
}

Q4T_TEST(vector_search_filter_combines_sets_and_nested_clauses) {
  const auto attrs = Read(R"({"team":"core","epoch":12,"public":false})");
  const auto filter = Read(R"({"type":"and","filters":[
    {"type":"in","key":"team","value":["core","serve"]},
    {"type":"or","filters":[
      {"type":"lt","key":"epoch","value":10},
      {"type":"eq","key":"public","value":false}]},
    {"type":"nin","key":"epoch","value":[11,13]}]})");
  Q4T_CHECK(Matches(filter, attrs));
  auto changed = attrs;
  Set(&changed, "team", String("private"));
  Q4T_CHECK(!Matches(filter, changed));
  Q4T_CHECK(!Matches(Read(R"({"type":"in","key":"team","value":[]})"), attrs));
  ChatPlanError error;
  Q4T_CHECK(!ValidateVectorFilter(
      Read(R"({"type":"in","key":"public","value":[false]})"), &error));
  return true;
}

Q4T_TEST(vector_search_filter_limits_fail_closed) {
  ChatPlanError error;
  for (const auto& text : {R"({"type":"or","filters":[]})",
                           R"({"type":"mystery","key":"a","value":1})",
                           R"({"key":"a","value":null})"})
    Q4T_CHECK(!ValidateVectorFilter(Read(text), &error));
  auto value = Read(R"({"key":"a","value":1})");
  for (int i = 0; i < 18; ++i)
    value = Object({{"type", String("and")}, {"filters", Array({value})}});
  Q4T_CHECK(!ValidateVectorFilter(value, &error) && error.http_status == 503);
  auto children = Array();
  children.array.assign(256, Read(R"({"key":"a","value":1})"));
  value = Object({{"type", String("or")}, {"filters", children}});
  Q4T_CHECK(!ValidateVectorFilter(value, &error) && error.http_status == 503);
  auto operand = Read(std::string(16000, '9'));
  auto values = Array();
  values.array.assign(5, operand);
  value =
      Object({{"type", String("in")}, {"key", String("a")}, {"value", values}});
  Q4T_CHECK(!ValidateVectorFilter(value, &error) && error.http_status == 503);
  return true;
}

Q4T_TEST(vector_search_cosine_uses_vectors_and_actual_norms) {
  auto plan =
      Plan(R"({"query":"PLE SSD","ranking_options":{"ranker":"none"}})");
  const std::vector<VectorSearchQuery> queries{{"PLE SSD", {6, 8, 0}}};
  auto corpus = Corpus();
  std::vector<VectorSearchHit> hits;
  ChatPlanError error;
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error));
  Q4T_CHECK(hits.size() == 1 && hits[0].chunk == 0 &&
            std::abs(hits[0].score - 1) < 1e-12);
  corpus[1].text = "PLE SSD PLE SSD";
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            hits.size() == 1);  // none does not rerank by text.
  return true;
}

Q4T_TEST(vector_search_multiple_queries_and_ties_are_stable) {
  auto plan = Plan(
      R"({"query":["PLE SSD","TLS"],"ranking_options":{"ranker":"none"}})");
  const std::vector<VectorSearchQuery> queries{{"PLE SSD", {3, 4, 0}},
                                               {"TLS", {0, 0, 1}}};
  auto corpus = Corpus();
  std::vector<VectorSearchHit> hits;
  ChatPlanError error;
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            hits.size() == 2 && corpus[hits[0].chunk].chunk_id == "ple");
  std::reverse(corpus.begin(), corpus.end());
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            hits.size() == 2 && corpus[hits[0].chunk].chunk_id == "ple");
  return true;
}

Q4T_TEST(vector_search_hybrid_uses_keyword_and_chinese_evidence) {
  for (const auto& query :
       {std::string("NATIVE TLS"), std::string("原生入口")}) {
    auto plan = Plan();
    plan.queries = {query};
    const std::vector<VectorSearchQuery> queries{{query, {1, 0}}};
    const std::vector<VectorSearchChunk> corpus{
        Chunk("a", "unrelated content", {1, 0}),
        Chunk("b",
              query == "NATIVE TLS" ? "native tls certificates"
                                    : "服务的原生入口与证书",
              {0.9f, 0.1f})};
    std::vector<VectorSearchHit> hits;
    ChatPlanError error;
    Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
              hits.size() == 2 && hits[0].chunk == 1 && hits[0].score <= 1);
  }
  return true;
}

Q4T_TEST(vector_search_filters_precede_rank_and_limits_follow_scores) {
  auto plan =
      Plan(R"({"query":"PLE SSD","filters":{"key":"public","value":true},
    "max_num_results":1,"ranking_options":{"ranker":"none","score_threshold":0.5}})");
  auto corpus = Corpus();
  corpus[0].attributes = Read(R"({"public":false})");
  corpus[1].attributes = Read(R"({"public":true})");
  corpus[1].embedding = {3, 0, 4};
  const std::vector<VectorSearchQuery> queries{{"PLE SSD", {1, 0, 0}}};
  std::vector<VectorSearchHit> hits;
  ChatPlanError error;
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            hits.size() == 1 && hits[0].chunk == 1 &&
            std::abs(hits[0].score - 0.6) < 1e-12);
  plan.score_threshold = 0.7;
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            hits.empty());
  return true;
}

Q4T_TEST(vector_search_corrupt_index_never_yields_partial_hits) {
  auto plan = Plan();
  const std::vector<VectorSearchQuery> queries{{"PLE SSD", {3, 4, 0}}};
  for (int failure = 0; failure < 6; ++failure) {
    auto corpus = Corpus();
    if (failure == 0) corpus[1].embedding = {1, 2};
    if (failure == 1) corpus[1].embedding = {0, 0, 0};
    if (failure == 2)
      corpus[1].embedding[0] = std::numeric_limits<float>::quiet_NaN();
    if (failure == 3) corpus[1].chunk_id = corpus[0].chunk_id;
    if (failure == 4) corpus[1].text = std::string("\xff", 1);
    if (failure == 5) corpus[1].attributes = Array();
    std::vector<VectorSearchHit> hits{{0, 1}};
    ChatPlanError error;
    Q4T_CHECK(!RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
              hits.empty() && error.code == "vector_index_invalid");
  }
  return true;
}

Q4T_TEST(vector_search_cancellation_and_deadline_discard_all_results) {
  const auto plan = Plan();
  const auto corpus = Corpus();
  const std::vector<VectorSearchQuery> queries{{"PLE SSD", {3, 4, 0}}};
  std::vector<VectorSearchHit> hits;
  ChatPlanError error;
  size_t calls = 0;
  VectorSearchControl control;
  control.cancelled = [&] {
    ++calls;
    return false;
  };
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, control, &hits, &error));
  const auto checkpoints = calls;
  for (size_t stop : {size_t(1), size_t(4), checkpoints}) {
    calls = 0;
    control.cancelled = [&] { return ++calls == stop; };
    hits = {{0, 1}};
    Q4T_CHECK(
        !RankVectorSearch(plan, queries, corpus, control, &hits, &error) &&
        hits.empty() && error.http_status == 499);
  }
  control.deadline = std::chrono::steady_clock::now();
  Q4T_CHECK(!RankVectorSearch(plan, queries, corpus, control, &hits, &error) &&
            error.http_status == 408 && hits.empty());
  auto long_plan = plan;
  long_plan.queries = {std::string(16384, 'x')};
  const std::vector<VectorSearchQuery> long_query{
      {long_plan.queries[0], {3, 4, 0}}};
  calls = 0;
  control.deadline = std::chrono::steady_clock::time_point::max();
  control.cancelled = [&] { return ++calls == 4; };
  Q4T_CHECK(!RankVectorSearch(long_plan, long_query, corpus, control, &hits,
                              &error) &&
            error.http_status == 499 && hits.empty() && calls == 4);
  return true;
}

Q4T_TEST(vector_search_resource_failure_and_empty_corpus_are_distinct) {
  auto plan = Plan();
  const std::vector<VectorSearchQuery> queries{{"PLE SSD", {3, 4, 0}}};
  std::vector<VectorSearchHit> hits;
  ChatPlanError error;
  Q4T_CHECK(RankVectorSearch(plan, queries, {}, {}, &hits, &error) &&
            hits.empty());
  std::vector<VectorSearchChunk> corpus(kVectorSearchChunks + 1);
  Q4T_CHECK(!RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            error.code == "vector_search_resource_exhausted");
  corpus = Corpus();
  corpus[0].text.assign(kVectorSearchTextBytes, 'a');
  Q4T_CHECK(!RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            error.code == "vector_search_resource_exhausted" && hits.empty());
  return true;
}

Q4T_TEST(vector_search_rewrite_requires_the_executed_query_and_valid_vectors) {
  auto plan = Plan();
  auto corpus = Corpus();
  std::vector<VectorSearchQuery> queries{{"SSD expert lookup", {3, 4, 0}}};
  std::vector<VectorSearchHit> hits;
  ChatPlanError error;
  Q4T_CHECK(!RankVectorSearch(plan, queries, corpus, {}, &hits, &error));
  plan.rewrite_query = true;
  Q4T_CHECK(RankVectorSearch(plan, queries, corpus, {}, &hits, &error));
  queries[0].embedding = {0, 0, 0};
  Q4T_CHECK(!RankVectorSearch(plan, queries, corpus, {}, &hits, &error) &&
            hits.empty());
  return true;
}
