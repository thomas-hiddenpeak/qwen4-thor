#include "q4t/server/reasoning_options.h"

#include "q4t/server/input_contract.h"

namespace q4t::server {

std::string_view ReasoningFieldName(ReasoningField field) {
  return field == ReasoningField::kReasoning ? "reasoning"
                                             : "reasoning_content";
}

Status ParseReasoningOptions(const io::Json& request, ReasoningOptions* options,
                             std::string* error_param) {
  *options = {};
  const auto invalid = [&](const char* key, const char* message) {
    if (error_param) *error_param = key;
    return Status::Fail(message);
  };
  const bool parsed =
      request.Has("messages") && request.GetBool("parse_response", true);
  if (const auto* field = request.Find("reasoning_field")) {
    if (!field->IsString() ||
        (field->str != "reasoning" && field->str != "reasoning_content"))
      return invalid("reasoning_field",
                     "reasoning_field must be reasoning or reasoning_content");
    if (!parsed)
      return invalid("reasoning_field",
                     "reasoning_field requires parsed messages");
    if (field->str == "reasoning") options->field = ReasoningField::kReasoning;
  }
  if (const auto* include = request.Find("include_reasoning")) {
    if (!include->IsBool())
      return invalid("include_reasoning", "include_reasoning must be boolean");
    if (!parsed)
      return invalid("include_reasoning",
                     "include_reasoning requires parsed messages");
    options->include = include->boolean;
  }
  if (const auto* budget = request.Find("thinking_token_budget");
      budget && !budget->IsNull()) {
    if (!IsInputInteger(*budget) || budget->number < 0 ||
        budget->number > 2147483647.0)
      return invalid("thinking_token_budget",
                     "thinking_token_budget must be an integer in "
                     "[0,2147483647] or null");
    if (!parsed)
      return invalid("thinking_token_budget",
                     "thinking_token_budget requires parsed messages");
    options->token_budget = budget->AsInt();
  }
  return Status();
}

Status ResolveMessageReasoning(const io::Json& message, size_t index,
                               std::string_view* reasoning,
                               std::string* error_param) {
  *reasoning = {};
  const auto invalid = [&](const char* key, const char* text) {
    if (error_param)
      *error_param = "messages[" + std::to_string(index) + "]." + key;
    return Status::Fail(text);
  };
  const auto* canonical = message.Find("reasoning");
  const auto* legacy = message.Find("reasoning_content");
  for (const auto* key : {"reasoning", "reasoning_content"}) {
    const auto* value = message.Find(key);
    if (!value) continue;
    if (message.GetString("role") != "assistant")
      return invalid(key, "reasoning history requires an assistant message");
    if (!value->IsNull() && !value->IsString())
      return invalid(key, "reasoning history must be a string or null");
  }
  if (canonical && canonical->IsString()) {
    if (legacy && legacy->IsString() && canonical->str != legacy->str)
      return invalid("reasoning", "conflicting reasoning history aliases");
    *reasoning = canonical->str;
  } else if (legacy && legacy->IsString()) {
    *reasoning = legacy->str;
  }
  return Status();
}

}  // namespace q4t::server
