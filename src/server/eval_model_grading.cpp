#include "eval_model_grading.h"

#include <algorithm>
#include <cmath>

#include "eval_run_state.h"

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;

Json RecordSchema(Json properties, Json required) {
  return Object({{"type", String("object")},
                 {"properties", std::move(properties)},
                 {"required", std::move(required)},
                 {"additionalProperties", Boolean(false)}});
}

ObjectReply GraderSource(const Json& criterion, Json* source) {
  const bool label = criterion.GetString("type") == "label_model";
  const auto* input = criterion.GetArray("input");
  if (!input) return InvalidRecord();
  auto messages = *input, options = Object(), schema = Object();
  const auto* params = criterion.Find("sampling_params");
  if (params)
    for (const auto& [key, value] : params->object)
      Set(&options,
          key == "max_completions_tokens" ? "max_completion_tokens" : key,
          value);
  if (label) {
    const auto* labels = criterion.GetArray("labels");
    const auto* passing = criterion.GetArray("passing_labels");
    if (!labels || labels->array.empty() || !passing)
      return BadInput("testing_criteria.labels", "Expected at least one label");
    for (const auto& value : passing->array)
      if (std::none_of(labels->array.begin(), labels->array.end(),
                       [&](const Json& v) { return v.str == value.str; }))
        return BadInput("testing_criteria.passing_labels",
                        "Passing label is outside the declared labels");
    schema = RecordSchema(Object({{"label", Object({{"type", String("string")},
                                                    {"enum", *labels}})}}),
                          Array({String("label")}));
  } else {
    auto score = Object({{"type", String("number")}});
    const auto* range = criterion.GetArray("range");
    if (range) {
      if (range->array.size() != 2 ||
          range->array[0].number > range->array[1].number)
        return BadInput("testing_criteria.range",
                        "Expected increasing score endpoints");
      Set(&score, "minimum", range->array[0]);
      Set(&score, "maximum", range->array[1]);
    }
    auto text = Object({{"type", String("string")}});
    auto step =
        RecordSchema(Object({{"description", text}, {"conclusion", text}}),
                     Array({String("description"), String("conclusion")}));
    schema =
        RecordSchema(Object({{"result", std::move(score)},
                             {"steps", Object({{"type", String("array")},
                                               {"items", std::move(step)}})}}),
                     Array({String("result"), String("steps")}));
  }
  auto format = Object({{"type", String("json_schema")},
                        {"name", String(label ? "eval_label" : "eval_score")},
                        {"schema", std::move(schema)},
                        {"strict", Boolean(true)}});
  Set(&options, "text", Object({{"format", std::move(format)}}));
  const char* instruction =
      label ? "Classify the sample according to the provided grading "
              "instructions. Return a JSON object containing the chosen label."
            : "Grade the sample according to the provided grading "
              "instructions. Return a JSON object with a numeric result and a "
              "list of grading steps.";
  messages.array.insert(messages.array.begin(),
                        Object({{"role", String("developer")},
                                {"content", String(instruction)}}));
  *source =
      Object({{"type", String("responses")},
              {"model", String(criterion.GetString("model"))},
              {"input_messages", Object({{"type", String("template")},
                                         {"template", std::move(messages)}})},
              {"sampling_params", std::move(options)}});
  return {};
}

ObjectReply ReadGrade(const Json& criterion, const Json& sample, Json* out) {
  const auto* value = sample.Find("output_json");
  if (!value || !value->IsObject() ||
      sample.GetString("finish_reason") != "stop")
    return BadInput(
        "sample", "Grading model did not produce a complete structured result");
  const auto type = criterion.GetString("type");
  Json score;
  bool passed = false;
  if (type == "label_model") {
    const auto* chosen = value->Find("label");
    if (!chosen || !chosen->IsString())
      return BadInput("sample", "Missing grader label");
    auto contains = [&](const char* key) {
      const auto* labels = criterion.GetArray(key);
      return labels &&
             std::any_of(labels->array.begin(), labels->array.end(),
                         [&](const Json& v) { return v.str == chosen->str; });
    };
    if (!contains("labels"))
      return BadInput("sample", "Grading model returned an unknown label");
    passed = contains("passing_labels");
    score = Number(passed ? 1 : 0);
  } else {
    const auto* actual = value->Find("result");
    const auto* range = criterion.GetArray("range");
    if (!actual || !actual->IsNumber() || !std::isfinite(actual->number) ||
        !value->GetArray("steps") ||
        (range &&
         (range->array.size() != 2 || actual->number < range->array[0].number ||
          actual->number > range->array[1].number)))
      return BadInput("sample", "Invalid model grader score");
    score = *actual;
    passed = score.number >= criterion.GetNumber("pass_threshold", 0.5);
  }
  *out = Object({{"type", String(type)},
                 {"name", String(criterion.GetString("name"))},
                 {"score", std::move(score)},
                 {"passed", Boolean(passed)},
                 {"sample", *value}});
  return {};
}
}  // namespace

bool ModelCriterion(const Json& criterion) {
  const auto type = criterion.GetString("type");
  return type == "label_model" || type == "score_model";
}

void RecordModelUsage(Json* resource, const Json& sample) {
  auto& usage = Field(*resource, "per_model_usage");
  if (usage.IsNull()) usage = Array();
  const auto name = sample.GetString("model");
  auto used = std::find_if(
      usage.array.begin(), usage.array.end(),
      [&](const Json& v) { return v.GetString("model_name") == name; });
  if (used == usage.array.end()) {
    usage.array.push_back(Object({{"model_name", String(name)}}));
    used = std::prev(usage.array.end());
  }
  Set(&*used, "invocation_count", Number(used->GetInt("invocation_count") + 1));
  const auto& actual = *sample.Find("usage");
  for (const auto* key :
       {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"})
    Set(&*used, key, Number(used->GetInt(key) + actual.GetInt(key)));
}

void ClearModelGrades(Json& data) {
  std::erase_if(data.object, [](const auto& f) {
    return f.first == "grading_row" || f.first == "model_results";
  });
}

ObjectReply AdvanceModelGrade(ObjectStore& objects, const StateAccess& access,
                              SamplingOwner* owner, ObjectRequest& write,
                              Json& resource, const Json& row,
                              ObjectRequest* consumed) {
  const auto* criteria = write.data.GetArray("criteria");
  if (!criteria) return InvalidRecord();
  auto* saved = write.data.Find("model_results");
  if (!saved) {
    if (std::none_of(criteria->array.begin(), criteria->array.end(),
                     ModelCriterion))
      return {};
    Set(&write.data, "grading_row", row);
    Set(&write.data, "model_results", Object());
  } else {
    if (!saved->IsObject()) return InvalidRecord();
    size_t index = 0;
    while (index < criteria->array.size() &&
           (!ModelCriterion(criteria->array[index]) ||
            saved->Has(std::to_string(index))))
      ++index;
    if (index == criteria->array.size()) return {};
    if (!owner)
      return Error(503, "eval_sample_unavailable",
                   "Model grader owner is unavailable");
    const auto criterion = criteria->array[index];
    Json source, jury = row, grade;
    auto result = GraderSource(criterion, &source);
    if (result.ok())
      result =
          SampleRow(objects, access, *owner, write, source, &jury, consumed);
    if (!result.ok() &&
        (result.commit_unknown || result.http_status == 409 ||
         result.http_status == 429 || result.http_status == 499 ||
         result.http_status >= 500))
      return result;
    if (result.ok() && result.http_status == 202) return result;
    if (result.ok()) {
      const auto& sample = *jury.Find("sample");
      RecordModelUsage(&resource, sample);
      result = ReadGrade(criterion, sample, &grade);
    }
    if (!result.ok()) {
      grade =
          Object({{"name", String(criterion.GetString("name"))},
                  {"type", String(criterion.GetString("type"))},
                  {"score", Number(0)},
                  {"passed", Boolean(false)},
                  {"error", Object({{"code", String(result.code)},
                                    {"message", String(result.message)}})}});
    }
    Set(&Field(write.data, "model_results"), std::to_string(index),
        std::move(grade));
  }
  Set(&write.data, "resource", resource);
  // The row checkpoint consumes only this exact private child. Retrying an
  // uncertain receipt always reads the parent before requesting inference.
  ClearSampling(write.data);
  std::vector<ObjectRequest> changes{write};
  if (!consumed->id.empty()) changes.push_back(*consumed);
  auto checkpoint =
      objects.ExecuteBatch(changes, access.deadline, access.cancelled);
  if (checkpoint.ok()) checkpoint.http_status = 202;
  return checkpoint;
}
}  // namespace q4t::server::eval_detail
