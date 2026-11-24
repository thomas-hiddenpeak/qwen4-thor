// Reference definitions and oracle versions are recorded with the Evals
// evidence.
#include "eval_grading.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <unordered_map>

#include "eval_run_state.h"
#include "eval_model_grading.h"
#include "q4t/model/embedding_config.h"

extern "C" {
#include <cutils.h>
}

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;
constexpr size_t kMaxUnits = 65536;
using Units = std::vector<uint32_t>;

ObjectReply TooMuch() {
  return Error(413, "eval_metric_capacity_exceeded",
               "Text comparison exceeds the evaluation row work budget");
}

bool Spend(size_t amount, size_t* remaining) {
  if (amount > *remaining) return false;
  *remaining -= amount;
  return true;
}

bool Space(uint32_t ch) {
  return (ch >= 9 && ch <= 13) || ch == ' ' || ch == 0x85 || ch == 0xa0 ||
         ch == 0x1680 || (ch >= 0x2000 && ch <= 0x200a) || ch == 0x2028 ||
         ch == 0x2029 || ch == 0x202f || ch == 0x205f || ch == 0x3000;
}

using Vocabulary = std::unordered_map<std::string_view, uint32_t>;

// BLEU/GLEU operate on whitespace-delimited tokens. ROUGE uses the Google
// rouge-score default lowercase ASCII alphanumeric tokenizer without stemming.
ObjectReply Tokenize(std::string* body, bool rouge, bool characters,
                     Vocabulary* vocabulary, Units* units) {
  size_t start = 0;
  auto flush = [&](size_t end) {
    if (end > start) {
      const auto token = std::string_view(*body).substr(start, end - start);
      auto [where, added] = vocabulary->try_emplace(token, vocabulary->size());
      units->push_back(where->second);
    }
  };
  for (size_t at = 0; at < body->size();) {
    const auto* first = reinterpret_cast<const uint8_t*>(body->data() + at);
    const auto* next = first;
    const int point =
        unicode_from_utf8(first, std::min<size_t>(4, body->size() - at), &next);
    if (point < 0 || point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff))
      return BadInput("testing_criteria", "Invalid Unicode in metric input");
    const size_t end = at + (next - first);
    if (characters) {
      units->push_back(point);
    } else if (rouge ? !((point >= 'a' && point <= 'z') ||
                         (point >= 'A' && point <= 'Z') ||
                         (point >= '0' && point <= '9'))
                     : Space(point)) {
      flush(at);
      start = end;
    } else if (rouge && point >= 'A' && point <= 'Z') {
      (*body)[at] = static_cast<char>(point + ('a' - 'A'));
    }
    if (units->size() > kMaxUnits) return TooMuch();
    at = end;
  }
  if (!characters) flush(body->size());
  return units->size() <= kMaxUnits ? ObjectReply{} : TooMuch();
}

ObjectReply Subsequence(std::span<const uint32_t> a,
                        std::span<const uint32_t> b, size_t* budget,
                        size_t* matched) {
  *matched = 0;
  while (!a.empty() && !b.empty() && a.front() == b.front()) {
    ++*matched;
    a = a.subspan(1);
    b = b.subspan(1);
  }
  while (!a.empty() && !b.empty() && a.back() == b.back()) {
    ++*matched;
    a = a.first(a.size() - 1);
    b = b.first(b.size() - 1);
  }
  if (a.empty() || b.empty()) return {};
  if (a.size() < b.size()) std::swap(a, b);
  if (a.size() > *budget / b.size()) return TooMuch();
  *budget -= a.size() * b.size();
  std::vector<uint32_t> previous(b.size() + 1);
  for (const auto left : a) {
    uint32_t diagonal = 0;
    for (size_t i = 0; i < b.size(); ++i) {
      const auto above = previous[i + 1];
      previous[i + 1] =
          left == b[i] ? diagonal + 1 : std::max(above, previous[i]);
      diagonal = above;
    }
  }
  *matched += previous.back();
  return {};
}

using Gram = std::array<uint32_t, 5>;
std::vector<Gram> Ngrams(const Units& text, size_t n) {
  std::vector<Gram> grams;
  if (text.size() < n) return grams;
  grams.resize(text.size() + 1 - n);
  for (size_t index = 0; index < grams.size(); ++index)
    std::copy_n(text.begin() + index, n, grams[index].begin());
  std::sort(grams.begin(), grams.end());
  return grams;
}

size_t Intersect(const std::vector<Gram>& a, const std::vector<Gram>& b) {
  size_t i = 0, j = 0, matches = 0;
  while (i < a.size() && j < b.size()) {
    if (a[i] < b[j])
      ++i;
    else if (b[j] < a[i])
      ++j;
    else {
      ++i;
      ++j;
      ++matches;
    }
  }
  return matches;
}

ObjectReply Metric(std::string_view metric, std::string* candidate,
                   std::string* reference, size_t* budget, double* score) {
  const bool fuzzy = metric == "fuzzy_match",
             rouge = metric.starts_with("rouge_");
  if (!fuzzy && !rouge && metric != "bleu" && metric != "gleu")
    return BadInput("testing_criteria.evaluation_metric",
                    "Metric requires its trained or lexical provider");
  if (!Spend(candidate->size() + reference->size(), budget)) return TooMuch();
  Units a, b;
  Vocabulary vocabulary;
  auto tokens = Tokenize(candidate, rouge, fuzzy, &vocabulary, &a);
  if (tokens.ok()) tokens = Tokenize(reference, rouge, fuzzy, &vocabulary, &b);
  if (!tokens.ok()) return tokens;
  *score = 0;
  if (a.empty() || b.empty()) {
    if (fuzzy && a.empty() && b.empty()) *score = 1;
    return {};
  }
  if (fuzzy || metric == "rouge_l") {
    size_t matches = 0;
    auto result = Subsequence(a, b, budget, &matches);
    if (result.ok()) *score = 2.0 * matches / (a.size() + b.size());
    return result;
  }
  if (rouge &&
      (metric.size() != 7 || metric.back() < '1' || metric.back() > '5'))
    return BadInput("testing_criteria.evaluation_metric",
                    "Unknown ROUGE order");
  const size_t first = rouge ? metric.back() - '0' : 1;
  const size_t last = rouge ? first : 4;
  double log_precision = 0;
  size_t matched = 0, all_a = 0, all_b = 0;
  for (size_t n = first; n <= last; ++n) {
    const size_t work = (a.size() + b.size()) *
                        std::bit_width(std::max(a.size(), b.size())) * n;
    if (!Spend(work, budget)) return TooMuch();
    const auto left = Ngrams(a, n), right = Ngrams(b, n);
    const size_t hits = Intersect(left, right);
    if (rouge) {
      if (hits) *score = 2.0 * hits / (left.size() + right.size());
      return {};
    }
    if (metric == "bleu") {
      if (!hits) return {};
      log_precision += std::log(static_cast<double>(hits) / left.size());
    } else {
      matched += hits;
      all_a += left.size();
      all_b += right.size();
    }
  }
  if (metric == "gleu")
    *score = static_cast<double>(matched) / std::max(all_a, all_b);
  else
    *score =
        std::exp(std::min(0.0, 1.0 - static_cast<double>(b.size()) / a.size()) +
                 log_precision / 4.0);
  return {};
}
}  // namespace

ObjectReply GradeCosine(const Json& criterion, const Json& row,
                        const std::function<ObjectReply(const Json&)>& embed,
                        Json* grade, Json* resource) {
  if (!embed)
    return Error(422, "eval_embedding_model_unconfigured",
                 "Cosine grading requires the native trained embedding model");
  size_t budget = kChatBodyBytes;
  std::string input, reference;
  auto read = RenderText(criterion.GetString("input"), row,
                         "testing_criteria.input", &budget, &input);
  if (read.ok())
    read = RenderText(criterion.GetString("reference"), row,
                      "testing_criteria.reference", &budget, &reference);
  if (!read.ok()) return read;
  auto result =
      embed(Array({String(std::move(input)), String(std::move(reference))}));
  if (!result.ok()) return result;
  const auto& response = result.value;
  const auto* vectors = response.GetArray("data");
  const auto* usage = response.Find("usage");
  const auto invalid = [] {
    return Error(502, "eval_embedding_invalid",
                 "Expected two complete trained embeddings and their usage");
  };
  if (response.GetString("object") != "list" ||
      response.GetString("model") != model::EmbeddingConfig::kModelId ||
      !vectors || vectors->array.size() != 2 || !usage ||
      usage->GetInt("prompt_tokens") <= 0 ||
      usage->GetInt("total_tokens") != usage->GetInt("prompt_tokens"))
    return invalid();
  const auto* a = vectors->array[0].GetArray("embedding");
  const auto* b = vectors->array[1].GetArray("embedding");
  if (vectors->array[0].GetString("object") != "embedding" ||
      vectors->array[1].GetString("object") != "embedding" || !a || !b ||
      a->array.size() != model::EmbeddingConfig::kHidden ||
      b->array.size() != a->array.size() ||
      vectors->array[0].GetInt("index", -1) != 0 ||
      vectors->array[1].GetInt("index", -1) != 1)
    return invalid();
  double dot = 0, aa = 0, bb = 0;
  bool identical = true;
  for (size_t i = 0; i < a->array.size(); ++i) {
    const auto& x = a->array[i];
    const auto& y = b->array[i];
    if (!x.IsNumber() || !y.IsNumber()) return invalid();
    identical = identical && x.number == y.number;
    dot += x.number * y.number;
    aa += x.number * x.number;
    bb += y.number * y.number;
  }
  if (!std::isfinite(dot) || !std::isfinite(aa) || !std::isfinite(bb) ||
      aa <= 0 || bb <= 0)
    return invalid();
  // Evals similarity grades have a [0, 1] range. Scores use the actual
  // configured model's vectors with FP64 accumulation.
  Json score;
  score.type = Json::Type::kNumber;
  score.number =
      identical ? 1.0
                : std::clamp(dot / (std::sqrt(aa) * std::sqrt(bb)), 0.0, 1.0);
  *grade = Object({{"name", String(criterion.GetString("name"))},
                   {"type", String("text_similarity")},
                   {"score", score},
                   {"passed", Boolean(score.number >=
                                      criterion.GetNumber("pass_threshold"))}});
  RecordModelUsage(resource, response);
  return {};
}

ObjectReply GradeSimilarity(const Json& criterion, const Json& row,
                            size_t* bytes, size_t* work) {
  std::string input, reference;
  auto result = RenderText(criterion.GetString("input"), row,
                           "testing_criteria.input", bytes, &input);
  if (result.ok())
    result = RenderText(criterion.GetString("reference"), row,
                        "testing_criteria.reference", bytes, &reference);
  if (!result.ok()) return result;
  double score = 0;
  result = Metric(criterion.GetString("evaluation_metric"), &input, &reference,
                  work, &score);
  if (!result.ok()) return result;
  Json value;
  value.type = Json::Type::kNumber;
  value.number = score;
  result.value = Object(
      {{"name", String(criterion.GetString("name"))},
       {"type", String("text_similarity")},
       {"score", std::move(value)},
       {"passed", Boolean(score >= criterion.GetNumber("pass_threshold"))}});
  return result;
}
}  // namespace q4t::server::eval_detail
