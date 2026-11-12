#include "q4t/server/vector_search.h"

#include <algorithm>
#include <cmath>
#include <unordered_set>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/schema_decimal.h"

namespace q4t::server {
namespace {
using io::Json;
Status Invalid(ChatPlanError* error, std::string param, std::string message,
               bool resource = false) {
  *error = {resource ? "vector_search_resource_exhausted" : "invalid_request",
            std::move(param), resource ? 503 : 400};
  return Status::Fail(std::move(message));
}
bool Text(std::string_view value, size_t characters, bool nonempty = false) {
  return (!nonempty || !value.empty()) && value.size() <= characters * 4 &&
         detail::NormalizeUtf8(value) == value &&
         static_cast<size_t>(std::count_if(
             value.begin(), value.end(),
             [](unsigned char c) { return (c & 0xc0) != 0x80; })) <= characters;
}
Status Scalar(const Json& value, ChatPlanError* error, const std::string& path,
              bool boolean = true) {
  if ((value.IsBool() && boolean) || (value.IsString() && Text(value.str, 512)))
    return {};
  if (value.IsNumber()) {
    SchemaDecimal number;
    const auto parsed = SchemaDecimal::Parse(OutputJson(value), &number);
    if (parsed) return {};
    if (parsed.message().starts_with("schema_resource_exhausted:"))
      return Invalid(error, path, "Exact numeric operand budget exceeded",
                     true);
  }
  return Invalid(error, path, "Expected a bounded string, number or boolean");
}
Status Fields(const Json& value, std::initializer_list<std::string_view> fields,
              ChatPlanError* error, std::string_view path) {
  const auto status = InputFields(value, fields, &error->param, path);
  if (!status) return status;
  std::unordered_set<std::string> names;
  for (const auto& [key, ignored] : value.object) {
    (void)ignored;
    if (!names.insert(key).second)
      return Invalid(error, InputParamPath(path, key), "Duplicate field");
  }
  return {};
}
Status Filter(const Json& value, const std::string& path, size_t depth,
              size_t* nodes, size_t* bytes, ChatPlanError* error) {
  if (++*nodes > 256 || depth > 16)
    return Invalid(error, path, "Filter exceeds depth/node budget", true);
  const auto type = value.GetString("type");
  if (type == "and" || type == "or") {
    auto status = Fields(value, {"type", "filters"}, error, path);
    if (!status) return status;
    const auto* children = value.GetArray("filters");
    if (!children || children->array.empty())
      return Invalid(error, path + ".filters", "Expected nonempty filters");
    if (children->array.size() > 256)
      return Invalid(error, path + ".filters", "Filter node budget", true);
    for (size_t i = 0; i < children->array.size(); ++i) {
      status = Filter(children->array[i],
                      path + ".filters[" + std::to_string(i) + ']', depth + 1,
                      nodes, bytes, error);
      if (!status) return status;
    }
    return {};
  }
  auto status = Fields(value, {"type", "key", "value"}, error, path);
  if (!status) return status;
  const auto* kind = value.Find("type");
  // The comparison operator has an official default of eq.
  const auto operation = kind ? type : "eq";
  if (operation != "eq" && operation != "ne" && operation != "gt" &&
      operation != "gte" && operation != "lt" && operation != "lte" &&
      operation != "in" && operation != "nin")
    return Invalid(error, path + ".type", "Invalid comparison operator");
  const auto* key = value.Find("key");
  if (!key || !key->IsString() || !Text(key->str, 64, true))
    return Invalid(error, path + ".key", "Expected attribute key <=64 chars");
  const auto* operand = value.Find("value");
  if (!operand) return Invalid(error, path + ".value", "Missing operand");
  const auto charge = [&](const Json& item) {
    const auto size = item.str.size() + key->str.size() + 64;
    if (size > (64ULL << 10) - *bytes) return false;
    *bytes += size;
    return true;
  };
  if (!charge(*operand))
    return Invalid(error, path + ".value", "Filter operand byte budget", true);
  if (operation != "in" && operation != "nin")
    return Scalar(*operand, error, path + ".value");
  if (!operand->IsArray())
    return Invalid(error, path + ".value", "Expected string/number array");
  if (operand->array.size() > 128)
    return Invalid(error, path + ".value", "Filter set budget", true);
  for (size_t i = 0; i < operand->array.size(); ++i) {
    if (!charge(operand->array[i]))
      return Invalid(error, path + ".value", "Filter operand byte budget",
                     true);
    status = Scalar(operand->array[i], error,
                    path + ".value[" + std::to_string(i) + ']', false);
    if (!status) return status;
  }
  return {};
}
int Compare(const Json& a, const Json& b) {
  if (a.IsString()) return a.str.compare(b.str);
  if (a.IsBool()) return int(a.boolean) - int(b.boolean);
  SchemaDecimal left, right;
  // Both values were validated, including their exact decimal spelling.
  (void)SchemaDecimal::Parse(OutputJson(a), &left);
  (void)SchemaDecimal::Parse(OutputJson(b), &right);
  return left.Compare(right);
}
bool Matches(const Json& filter, const Json& attributes) {
  if (filter.IsNull()) return true;
  const auto type = filter.GetString("type", "eq");
  if (type == "and" || type == "or") {
    const auto& children = filter.Find("filters")->array;
    const auto test = [&](const auto& item) {
      return Matches(item, attributes);
    };
    return type == "and" ? std::all_of(children.begin(), children.end(), test)
                         : std::any_of(children.begin(), children.end(), test);
  }
  const auto* attribute = attributes.Find(filter.GetString("key"));
  // A missing attribute never satisfies a comparison, including ne/nin.
  if (!attribute) return false;
  const auto& operand = *filter.Find("value");
  if (type == "in" || type == "nin") {
    const bool found = std::any_of(
        operand.array.begin(), operand.array.end(), [&](const auto& item) {
          return attribute->type == item.type && Compare(*attribute, item) == 0;
        });
    return type == "in" ? found : !found;
  }
  if (attribute->type != operand.type) return type == "ne";
  const auto order = Compare(*attribute, operand);
  if (type == "eq") return order == 0;
  if (type == "ne") return order != 0;
  if (type == "gt") return order > 0;
  if (type == "gte") return order >= 0;
  if (type == "lt") return order < 0;
  return order <= 0;
}
}  // namespace

Status ValidateVectorAttributes(const Json& attributes, ChatPlanError* error,
                                std::string_view path) {
  *error = {};
  if (attributes.IsNull()) return {};
  if (!attributes.IsObject() || attributes.object.size() > 16)
    return Invalid(error, std::string(path), "Expected up to 16 attributes");
  std::unordered_set<std::string> names;
  for (const auto& [key, value] : attributes.object) {
    const auto at = InputParamPath(path, key);
    if (!Text(key, 64, true) || !names.insert(key).second)
      return Invalid(error, at, "Expected distinct UTF-8 keys <=64 chars");
    const auto status = Scalar(value, error, at);
    if (!status) return status;
  }
  return {};
}
Status ValidateVectorFilter(const Json& filter, ChatPlanError* error,
                            std::string_view path) {
  *error = {};
  if (filter.IsNull()) return {};
  size_t nodes = 0, bytes = 0;
  return Filter(filter, std::string(path), 0, &nodes, &bytes, error);
}
Status MatchVectorFilter(const Json& filter, const Json& attributes,
                         bool* matches, ChatPlanError* error) {
  *matches = false;
  auto status = ValidateVectorFilter(filter, error);
  if (status) status = ValidateVectorAttributes(attributes, error);
  if (status) *matches = Matches(filter, attributes);
  return status;
}
Status ParseVectorChunking(const Json& value, VectorChunking* result,
                           ChatPlanError* error, std::string_view path) {
  *result = {};
  *error = {};
  if (value.IsNull()) return {};
  const auto type = value.GetString("type");
  auto status = type == "auto" ? Fields(value, {"type"}, error, path)
                               : Fields(value, {"type", "static"}, error, path);
  if (!status) return status;
  if (type == "auto") return {};
  if (type != "static")
    return Invalid(error, std::string(path) + ".type", "Expected auto/static");
  const auto* params = value.Find("static");
  const auto at = std::string(path) + ".static";
  if (!params) return Invalid(error, at, "Missing static chunking parameters");
  status = Fields(*params, {"max_chunk_size_tokens", "chunk_overlap_tokens"},
                  error, at);
  if (!status) return status;
  const auto* size = params->Find("max_chunk_size_tokens");
  const auto* overlap = params->Find("chunk_overlap_tokens");
  if (!size || !IsInputInteger(*size) || size->number < 100 ||
      size->number > 4096)
    return Invalid(error, at + ".max_chunk_size_tokens",
                   "Expected integer chunk size 100..4096");
  if (!overlap || !IsInputInteger(*overlap) || overlap->number < 0 ||
      overlap->number > size->number / 2)
    return Invalid(error, at + ".chunk_overlap_tokens",
                   "Expected integer overlap no greater than half the chunk");
  result->max_tokens = size->AsInt();
  result->overlap_tokens = overlap->AsInt();
  return {};
}
Status PlanVectorSearch(const Json& request, VectorSearchPlan* plan,
                        ChatPlanError* error) {
  *plan = {};
  *error = {};
  auto status = Fields(request,
                       {"query", "filters", "max_num_results",
                        "ranking_options", "rewrite_query"},
                       error, "");
  if (!status) return status;
  VectorSearchPlan result;
  const auto* query = request.Find("query");
  if (!query || (!query->IsString() && !query->IsArray()) ||
      (query->IsArray() && query->array.empty()))
    return Invalid(error, "query", "Expected nonempty text or text array");
  size_t bytes = 0;
  const auto add = [&](const Json& item, const std::string& path) -> Status {
    if (!item.IsString() || item.str.empty() ||
        detail::NormalizeUtf8(item.str) != item.str)
      return Invalid(error, path, "Expected nonempty UTF-8 query");
    if (item.str.size() > (64ULL << 10) - bytes)
      return Invalid(error, path, "Query byte budget exceeded", true);
    bytes += item.str.size();
    result.queries.push_back(item.str);
    return {};
  };
  if (query->IsString()) {
    status = add(*query, "query");
  } else {
    if (query->array.size() > kVectorSearchQueries)
      return Invalid(error, "query", "Query count budget exceeded", true);
    for (size_t i = 0; i < query->array.size(); ++i) {
      status = add(query->array[i], "query[" + std::to_string(i) + ']');
      if (!status) break;
    }
  }
  if (!status) return status;
  if (const auto* filters = request.Find("filters")) {
    status = ValidateVectorFilter(*filters, error);
    if (!status) return status;
    result.filters = *filters;
  }
  if (const auto* limit = request.Find("max_num_results")) {
    if (!IsInputInteger(*limit) || limit->number < 1 || limit->number > 50)
      return Invalid(error, "max_num_results", "Expected integer 1..50");
    result.max_results = limit->AsInt();
  }
  if (const auto* rewrite = request.Find("rewrite_query")) {
    if (!rewrite->IsBool())
      return Invalid(error, "rewrite_query", "Expected boolean");
    result.rewrite_query = rewrite->boolean;
  }
  if (const auto* ranking = request.Find("ranking_options")) {
    status = Fields(*ranking, {"ranker", "score_threshold"}, error,
                    "ranking_options");
    if (!status) return status;
    if (const auto* ranker = ranking->Find("ranker")) {
      if (!ranker->IsString() ||
          (ranker->str != "none" && ranker->str != "auto" &&
           ranker->str != "default-2024-11-15"))
        return Invalid(error, "ranking_options.ranker", "Invalid ranker");
      result.ranker = ranker->str;
    }
    if (const auto* threshold = ranking->Find("score_threshold")) {
      SchemaDecimal actual, zero, one;
      (void)SchemaDecimal::Parse("0", &zero);
      (void)SchemaDecimal::Parse("1", &one);
      if (!threshold->IsNumber() || !std::isfinite(threshold->number) ||
          !SchemaDecimal::Parse(OutputJson(*threshold), &actual) ||
          actual.Compare(zero) < 0 || actual.Compare(one) > 0)
        return Invalid(error, "ranking_options.score_threshold",
                       "Expected number in [0,1]");
      result.score_threshold = threshold->number;
    }
  }
  *plan = std::move(result);
  return {};
}
}  // namespace q4t::server
