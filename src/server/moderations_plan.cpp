#include "q4t/server/moderations_plan.h"

#include "q4t/server/input_contract.h"
#include "q4t/server/model_catalog.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;

Status Invalid(ChatPlanError* error, std::string param, std::string message,
               std::string code = "invalid_request", int status = 400) {
  *error = {std::move(code), std::move(param), status};
  return Status::Fail(std::move(message));
}
}  // namespace

Status PlanModerationsRequest(const io::Json& request,
                              const ChatPlanConfig& config,
                              ModerationsRequestPlan* plan,
                              ChatPlanError* error) {
  *plan = {};
  *error = {};
  auto status = InputFields(request, {"input", "model"}, &error->param);
  if (!status) return status;
  const auto* model = request.Find("model");
  if (model && (!model->IsString() || model->str.empty()))
    return Invalid(error, "model", "Expected a served model name");
  if (model && !IsServedModel(model->str, config.model, config.model_aliases))
    return Invalid(error, "model", "Moderation model is not served",
                   "model_not_found", 404);
  const auto* input = request.Find("input");
  if (!input || (!input->IsString() && !input->IsArray()) ||
      (input->IsArray() && input->array.empty()))
    return Invalid(error, "input", "Expected text or a nonempty input array");
  ModerationsRequestPlan candidate;
  candidate.model = config.model;
  const ModerationOptions options{true, config.model};
  size_t text_bytes = 0;
  const auto text = [&](const Json& value, const std::string& param) {
    if (!value.IsString())
      return Invalid(error, param, "Expected text; input types cannot mix");
    if (value.str.size() > kModerationsTextBytes - text_bytes)
      return Invalid(error, param, "Moderation text exceeds request capacity",
                     "moderation_input_capacity", 413);
    text_bytes += value.str.size();
    return Status{};
  };
  const auto add = [&](const Json& value, const std::string& param) {
    ModerationContent content;
    auto result = CollectModerationInput(
        Array({Object({{"role", String("user")}, {"content", value}})}),
        &content, error);
    ModerationPlan item;
    if (result)
      result = PlanModeration(options, content, config,
                              kMaxModerationStepTokens, &item, error);
    if (!result) {
      error->param = param;
      return result;
    }
    candidate.inputs.push_back(std::move(item));
    return Status{};
  };
  if (input->IsString()) {
    status = text(*input, "input");
    if (status) status = add(*input, "input");
  } else if (input->array[0].IsString()) {
    if (input->array.size() > kModerationsInputs)
      return Invalid(error, "input", "Too many moderation inputs",
                     "moderation_input_capacity", 413);
    for (size_t i = 0; i < input->array.size(); ++i) {
      const auto param = "input[" + std::to_string(i) + "]";
      status = text(input->array[i], param);
      if (status) status = add(input->array[i], param);
      if (!status) break;
    }
  } else {
    if (input->array.size() > kModerationsParts)
      return Invalid(error, "input", "Too many moderation content parts",
                     "moderation_input_capacity", 413);
    for (size_t i = 0; i < input->array.size(); ++i) {
      const auto& part = input->array[i];
      const auto param = "input[" + std::to_string(i) + "]";
      const auto type = part.GetString("type");
      if (type == "text") {
        status = InputFields(part, {"type", "text"}, &error->param, param);
        if (!status) return status;
        const auto* value = part.Find("text");
        if (!value)
          return Invalid(error, param + ".text", "Text content is required");
        status = text(*value, param + ".text");
        if (!status) return status;
      } else if (type == "image_url") {
        status = InputFields(part, {"type", "image_url"}, &error->param, param);
        if (!status) return status;
        const auto* image = part.Find("image_url");
        if (!image)
          return Invalid(error, param + ".image_url", "Image is required");
        status =
            InputFields(*image, {"url"}, &error->param, param + ".image_url");
        if (!status) return status;
        const auto* url = image->Find("url");
        if (!url || !url->IsString() || url->str.empty())
          return Invalid(error, param + ".image_url.url",
                         "A nonempty image URL is required");
      } else {
        return Invalid(error, param + ".type",
                       "Expected a text or image_url content part");
      }
    }
    status = add(*input, "input");
  }
  if (!status) return status;
  *plan = std::move(candidate);
  return {};
}

Status BuildModerationsResponse(std::string_view id,
                                const ModerationsRequestPlan& plan,
                                const std::vector<Json>& reports,
                                Json* response) {
  *response = {};
  if (id.empty() || id.size() > 128 || plan.model.empty() ||
      plan.inputs.empty() || plan.inputs.size() > kModerationsInputs ||
      reports.size() != plan.inputs.size())
    return Status::Fail("Incomplete moderation response");
  auto results = Array();
  for (size_t i = 0; i < reports.size(); ++i) {
    const auto& report = reports[i];
    if (!ValidModerationReport(report) ||
        report.GetString("type") != "moderation_result" ||
        report.GetString("model") != plan.model ||
        plan.inputs[i].model != plan.model)
      return Status::Fail("Missing or mismatched real moderation report");
    auto result = Object();
    for (const auto* key : {"flagged", "categories", "category_scores",
                            "category_applied_input_types"})
      Set(&result, key, *report.Find(key));
    results.array.push_back(std::move(result));
  }
  *response = Object({{"id", String(std::string(id))},
                      {"model", String(plan.model)},
                      {"results", std::move(results)}});
  return {};
}
}  // namespace q4t::server
