#include "q4t/server/decision_plan.h"

#include <algorithm>
#include <cmath>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/completion_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/prompt_cache_policy.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message) {
  *error = {"invalid_request", std::move(path), 400};
  return Status::Fail(std::move(message));
}
bool ToolIdentifier(std::string_view name, size_t limit, bool allow_dot) {
  return !name.empty() && name.size() <= limit &&
         std::ranges::all_of(name, [allow_dot](char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-' ||
                  (allow_dot && c == '.');
         });
}
Json Real(double number) {
  Json result;
  result.type = Json::Type::kNumber;
  result.number = number;
  return result;
}
// The JSON payload is caller data. Its object keys are not protocol fields.
bool JsonBudget(const Json& data, size_t containers, size_t* values) {
  if (++*values > 10000) return false;
  if (!data.IsArray() && !data.IsObject()) return true;
  if (containers >= 32) return false;
  for (const auto& child : data.array)
    if (!JsonBudget(child, containers + 1, values)) return false;
  for (const auto& [key, child] : data.object)
    if (!JsonBudget(child, containers + 1, values)) return false;
  return true;
}
Status Parts(const Json& content, const std::string& path, size_t* breakpoints,
             ChatPlanError* error) {
  if (content.IsString()) {
    if (Utf8Characters(content.str) > (10ULL << 20))
      return Invalid(error, path, "Text exceeds the Decision input limit");
    return {};
  }
  if (content.array.size() > 16384)
    return Invalid(error, path, "Too many Decision content parts");
  for (size_t i = 0; i < content.array.size(); ++i) {
    const auto& part = content.array[i];
    const auto field = path + "[" + std::to_string(i) + "]";
    if (Nonnull(part, "prompt_cache_breakpoint") && ++*breakpoints > 4)
      return Invalid(error, field + ".prompt_cache_breakpoint",
                     "At most four explicit prompt cache breakpoints");
    if (const auto* url = part.Find("image_url");
        url && !url->str.starts_with("data:") &&
        !url->str.starts_with("http://") && !url->str.starts_with("https://"))
      return Invalid(error, field + ".image_url",
                     "Expected a data or HTTP(S) URL");
    if (const auto* data = part.Find("data")) {
      size_t count = 0;
      if (!JsonBudget(*data, 0, &count) ||
          OutputJson(*data).size() > (256ULL << 10))
        return Invalid(error, field + ".data",
                       "Decision JSON input exceeds its budget");
    }
  }
  return {};
}
}  // namespace

Status CheckDecisionInputItem(const Json& item, const std::string& path,
                              size_t* breakpoints, ChatPlanError* error) {
  if (!error || !breakpoints)
    return Status::Fail("Missing Decision input owner");
  if (const auto* call = item.Find("call_id");
      call && (call->str.empty() || Utf8Characters(call->str) > 64))
    return Invalid(error, path + ".call_id", "Invalid Decision tool call ID");
  const bool function = item.GetString("type") == "function_call";
  if (const auto* name = item.Find("name");
      name && !ToolIdentifier(name->str, function ? 128 : 64, function))
    return Invalid(error, path + ".name", "Invalid Decision tool name");
  if (const auto* space = item.Find("namespace");
      space && !space->IsNull() &&
      !ToolIdentifier(space->str, function ? 128 : 64, false))
    return Invalid(error, path + ".namespace",
                   "Invalid Decision tool namespace");
  for (const auto key : {"content", "output"})
    if (const auto* content = item.Find(key)) {
      auto checked = Parts(*content, path + "." + key, breakpoints, error);
      if (!checked) return checked;
    }
  return {};
}

Status CheckDecisionRequest(const Json& request, const ChatPlanConfig& config,
                            ChatPlanError* error) {
  auto checked = ValidateDecisionShape(request, DecisionShape::kRequest, error);
  if (!checked) return checked;
  const auto model = request.GetString("model");
  if (!ValidModelIdentifier(model))
    return Invalid(error, "model", "Invalid model identifier");
  if (!IsServedModel(model, config.model, config.model_aliases)) {
    *error = {"model_not_found", "model", 404};
    return Status::Fail("Model is not registered by this server");
  }
  std::string param;
  checked = ValidateSafetyIdentifier(request, &param);
  PromptCacheOptions policy;
  if (checked) checked = ParsePromptCacheOptions(request, &policy, &param);
  if (!checked) return Invalid(error, param, checked.message());
  const auto& questions = request.Find("questions")->array;
  if (questions.empty() || questions.size() > 400)
    return Invalid(error, "questions", "Expected 1 to 400 Decision questions");
  size_t breakpoints = 0;
  for (size_t i = 0; i < questions.size(); ++i) {
    const auto& question = questions[i];
    const auto path = "questions[" + std::to_string(i) + "]";
    const auto& instructions = *question.Find("instructions");
    if (instructions.IsArray() &&
        (instructions.array.empty() || instructions.array.size() > 16384))
      return Invalid(error, path + ".instructions",
                     "Invalid number of instruction parts");
    checked = Parts(instructions, path + ".instructions", &breakpoints, error);
    if (!checked) return checked;
    if (const auto* choices = question.GetArray("choices")) {
      if (choices->array.size() < 2 || choices->array.size() > 255)
        return Invalid(error, path + ".choices", "Expected 2 to 255 choices");
      std::unordered_set<std::string> unique;
      for (const auto& choice : choices->array)
        if (!unique.insert(OutputJson(*choice.Find("value"))).second)
          return Invalid(error, path + ".choices",
                         "Choices must have distinct values");
    }
    if (const auto* levels = question.GetArray("levels");
        levels && (levels->array.size() < 2 || levels->array.size() > 10))
      return Invalid(error, path + ".levels",
                     "Expected 2 to 10 ordered levels");
  }
  std::unordered_map<std::string, std::string> calls;
  std::unordered_set<std::string> answered;
  const auto& input = *request.Find("input");
  if (input.IsString()) return Parts(input, "input", &breakpoints, error);
  if (input.array.size() > 131072)
    return Invalid(error, "input", "Too many Decision input items");
  for (size_t i = 0; i < input.array.size(); ++i) {
    const auto& item = input.array[i];
    const auto path = "input[" + std::to_string(i) + "]";
    const auto kind = item.GetString("type");
    const auto call = item.GetString("call_id");
    if (kind == "function_call" || kind == "custom_tool_call") {
      if (!calls.emplace(call, kind + "_output").second)
        return Invalid(error, path + ".call_id",
                       "Duplicate Decision input tool call");
    } else if (kind.ends_with("_call_output")) {
      auto owner = calls.find(call);
      if (owner == calls.end() || owner->second != kind ||
          !answered.insert(call).second)
        return Invalid(error, path + ".call_id",
                       "No matching unanswered tool call");
    }
    checked = CheckDecisionInputItem(item, path, &breakpoints, error);
    if (!checked) return checked;
  }
  return {};
}

Status ProjectDecisionAnswer(const Json& question,
                             std::span<const double> log_weights,
                             double confidence, bool refused, Json* answer) {
  if (!answer) return Status::Fail("Missing Decision answer owner");
  const auto type = question.GetString("type");
  const auto* options =
      question.GetArray(type == "score" ? "levels" : "choices");
  const bool predicate = type == "predicate";
  if (!predicate && (!options || (type != "choice" && type != "score")))
    return Status::Fail("Invalid Decision question");
  auto result = Object(
      {{"type", String(refused ? "refusal" : type)},
       {"name", question.Has("name") ? *question.Find("name") : Json{}}});
  if (!refused) {
    if (log_weights.size() != (predicate ? 2 : options->array.size()) ||
        log_weights.empty() || !std::isfinite(confidence) || confidence < 0 ||
        confidence > 1)
      return Status::Fail("Invalid native Decision result");
    const auto best = std::max_element(log_weights.begin(), log_weights.end());
    if (!std::isfinite(*best)) return Status::Fail("No finite Decision weight");
    double mass = 0;
    for (double value : log_weights) mass += std::exp(value - *best);
    if (!std::isfinite(mass))
      return Status::Fail("Nonfinite Decision distribution");
    if (predicate) {
      Set(&result, "probability",
          Real(std::exp(log_weights[1] - *best) / mass));
    } else {
      auto probabilities = Array();
      double score = 0;
      for (size_t i = 0; i < log_weights.size(); ++i) {
        const double probability = std::exp(log_weights[i] - *best) / mass;
        const auto* label =
            options->array[i].Find(type == "score" ? "label" : "value");
        if (!label) return Status::Fail("Missing Decision option");
        auto entry = Object({{"probability", Real(probability)},
                             {"value", type == "score" ? Number(i) : *label}});
        if (type == "score") {
          Set(&entry, "label", *label);
          score += i * probability;
        }
        probabilities.array.push_back(std::move(entry));
      }
      if (type == "score")
        Set(&result, "score", Real(score));
      else
        Set(&result, "choice",
            *options->array[best - log_weights.begin()].Find("value"));
      Set(&result, "confidence", Real(confidence));
      Set(&result, "probabilities", std::move(probabilities));
    }
  }
  *answer = std::move(result);
  return {};
}
Status BuildDecisionResult(const Json& request, Json answers,
                           const ActualUsage& usage, Json* result) {
  const auto* questions = request.GetArray("questions");
  if (!result || !questions || !answers.IsArray() ||
      questions->array.size() != answers.array.size() ||
      !ValidActualUsage(usage) || usage.input + usage.output > INT32_MAX)
    return Status::Fail("Incomplete Decision answers or invalid actual usage");
  for (size_t i = 0; i < questions->array.size(); ++i) {
    const auto& question = questions->array[i];
    const auto& answer = answers.array[i];
    const auto* name = question.Find("name");
    const auto* observed = answer.Find("name");
    if (!observed ||
        (name ? !observed->IsString() || observed->str != name->str
              : !observed->IsNull()) ||
        (answer.GetString("type") != "refusal" &&
         answer.GetString("type") != question.GetString("type")))
      return Status::Fail("Decision answer order or identity changed");
  }
  auto receipt =
      Object({{"input_tokens", Number(usage.input)},
              {"input_tokens_details",
               Object({{"cached_tokens", Number(usage.cached)},
                       {"cache_write_tokens", Number(usage.cache_write)}})},
              {"output_tokens", Number(usage.output)},
              {"output_tokens_details",
               Object({{"reasoning_tokens", Number(usage.reasoning)}})},
              {"total_tokens", Number(usage.input + usage.output)}});
  auto value = Object({{"model", String(request.GetString("model"))},
                       {"answers", std::move(answers)},
                       {"usage", std::move(receipt)}});
  ChatPlanError error;
  auto status = ValidateDecisionShape(value, DecisionShape::kResult, &error);
  if (status) *result = std::move(value);
  return status;
}
}  // namespace q4t::server
