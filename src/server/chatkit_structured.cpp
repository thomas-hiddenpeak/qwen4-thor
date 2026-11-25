#include "chatkit_structured.h"

#include <algorithm>
#include <unordered_set>

#include "object_protocol.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string field) {
  auto error = Error(400, "invalid_request", "Invalid structured input");
  error.value = Object({{"param", String(std::move(field))}});
  return error;
}

bool StringField(const Json& value, const char* key) {
  const auto* field = value.Find(key);
  return field && field->IsString();
}

ObjectReply ValidateAnswers(const Json& answers) {
  if (!answers.IsObject()) return Invalid("input.answers");
  for (const auto& [id, answer] : answers.object) {
    const auto field = "input.answers." + id;
    if (!answer.IsObject()) return Invalid(field);
    if (const auto* skip = answer.Find("skipped"); skip && !skip->IsBool())
      return Invalid(field + ".skipped");
    const auto* values = answer.Find("values");
    if (values && (!values->IsArray() ||
                   !std::ranges::all_of(values->array, [](const Json& v) {
                     return v.IsString();
                   })))
      return Invalid(field + ".values");
  }
  return {};
}
}  // namespace

io::Json PendingStructuredInput(const Json& arguments) {
  const auto* inputs = arguments.GetArray("inputs");
  if (!inputs || inputs->array.empty()) return {};
  std::unordered_set<std::string> ids;
  Json questions = Array();
  for (const auto& input : inputs->array) {
    const auto id = input.GetString("id");
    if (id.empty() || !ids.insert(id).second || !StringField(input, "question"))
      return {};
    auto row = Object({{"id", String(id)},
                       {"question", *input.Find("question")},
                       {"answer", {}}});
    const auto type = input.GetString("type");
    Set(&row, "type", String(type));
    if (type == "freeform") {
      const auto* desc = input.Find("description");
      if (desc && !desc->IsNull() && !desc->IsString()) return {};
      Set(&row, "description", desc ? *desc : Json{});
    } else if (type == "multiple_choice") {
      const auto* options = input.GetArray("options");
      const auto* many = input.Find("multiple");
      if (!options || (many && !many->IsBool())) return {};
      Json choices = Array();
      for (const auto& choice : options->array) {
        if (!StringField(choice, "value")) return {};
        choices.array.push_back(Object({{"value", *choice.Find("value")}}));
      }
      Set(&row, "options", std::move(choices));
      Set(&row, "multiple", Boolean(input.GetBool("multiple")));
    } else {
      return {};
    }
    questions.array.push_back(std::move(row));
  }
  return Object(
      {{"status", String("pending")}, {"inputs", std::move(questions)}});
}

ObjectReply SubmitStructuredInput(const Json& submission, Json* form) {
  if (form->GetString("status") != "pending")
    return Error(409, "chatkit_input_not_pending",
                 "This structured input has already been answered or skipped");
  if (!submission.IsObject()) return Invalid("input");
  const auto status = submission.GetString("status", "answered");
  if ((submission.Has("status") && !StringField(submission, "status")) ||
      (status != "answered" && status != "skipped"))
    return Invalid("input.status");
  const auto* answers = submission.Find("answers");
  if (answers) {
    auto valid = ValidateAnswers(*answers);
    if (!valid.ok()) return valid;
  }
  // Validate the entire submission before mutating any question. Unknown
  // answers are validated above but have no corresponding question to update.
  if (!form->GetArray("inputs"))
    return Error(503, "chatkit_thread_invalid", "Missing structured questions");
  for (auto& question : Field(*form, "inputs").array) {
    const auto* answer =
        answers ? answers->Find(question.GetString("id")) : nullptr;
    const auto* values = answer ? answer->GetArray("values") : nullptr;
    const bool skip = status == "skipped" || !values || values->array.empty() ||
                      answer->GetBool("skipped");
    Json selected = Array();
    if (!skip) {
      if (question.GetString("type") == "multiple_choice" &&
          !question.GetBool("multiple"))
        selected.array.push_back(values->array.front());
      else
        selected = *values;
    }
    Set(&question, "answer",
        Object({{"values", std::move(selected)}, {"skipped", Boolean(skip)}}));
  }
  Set(form, "status", String(status));
  return {};
}
}  // namespace q4t::server::chatkit_detail
