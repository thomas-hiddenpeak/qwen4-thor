#include "q4t/server/realtime_prompt_settings.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string code, std::string message, std::string param) {
  auto reply = Error(400, std::move(code), std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Shape(const Json& value, std::string_view type) {
  ChatPlanError error;
  const auto status = ValidateRealtimeWireShape(
      Object({{"type", String(std::string(type))},
              {type == "session.update" ? "session" : "response", value}}),
      RealtimeShape::kClientEvent, &error);
  return status ? ObjectReply{}
                : Invalid(error.code, status.message(), error.param);
}
void MergeObject(Json* current, const Json& update) {
  if (!current->IsObject() || !update.IsObject() ||
      (update.Has("type") &&
       current->GetString("type") != update.GetString("type"))) {
    *current = update;
    return;
  }
  for (const auto& [key, value] : update.object) {
    auto* old = current->Has(key) ? &Field(*current, key) : nullptr;
    if (old && old->IsObject() && value.IsObject())
      MergeObject(old, value);
    else
      Set(current, key, value);
  }
}
void MergeSettings(Json* current, const Json& update) {
  for (const auto& [key, value] : update.object) {
    if (key == "prompt") continue;
    if ((key == "audio" || key == "reasoning") && current->Has(key))
      MergeObject(&Field(*current, key), value);
    else
      Set(current, key, value);
  }
}
ObjectReply Bounded(const Json& defaults, const Json& overrides,
                    const RealtimePromptSelection& selected) {
  size_t total = 0;
  std::vector<const Json*> values{&defaults, &overrides, &selected.settings};
  if (selected.prompt) {
    values.push_back(&selected.prompt->reference);
    values.push_back(&selected.prompt->request);
    total = selected.prompt->scope.capacity();
  }
  for (const auto* value : values) {
    size_t bytes = 0;
    if (total > RealtimePromptSettings::kMaximumBytes ||
        !MeasureRealtimeStorage(
            *value, RealtimePromptSettings::kMaximumBytes - total, &bytes))
      return Error(413, "realtime_prompt_capacity",
                   "Prompt settings exceed their retained storage limit");
    total += bytes;
  }
  return {};
}
}  // namespace

RealtimePromptSettings::RealtimePromptSettings(Json defaults)
    : defaults_(std::move(defaults)), overrides_(Object()) {
  current_.settings = defaults_;
}

ObjectReply RealtimePromptSettings::Select(
    const Json& update, const RealtimePromptResolver& resolve,
    std::optional<PromptSnapshot>* prompt) const {
  const auto* reference = update.Find("prompt");
  if (!reference) return {};
  if (reference->IsNull()) {
    prompt->reset();
    return {};
  }
  if (!resolve)
    return Error(503, "prompt_registry_unavailable",
                 "Prompt lookup is unavailable");
  PromptSnapshot snapshot;
  auto result = resolve(*reference, kMaximumExpansionBytes, &snapshot);
  if (!Accepted(result)) return result;
  if (snapshot.api != PromptApi::kRealtime)
    return Invalid("prompt_api_mismatch", "Expected a Realtime prompt",
                   "prompt.id");
  if (!snapshot.request.IsObject() || snapshot.scope.empty() ||
      snapshot.reference.GetString("id") != reference->GetString("id") ||
      snapshot.reference.GetString("version").empty() ||
      (reference->Has("version") && !reference->Find("version")->IsNull() &&
       snapshot.reference.GetString("version") !=
           reference->GetString("version")))
    return Error(500, "prompt_snapshot_mismatch",
                 "Resolved prompt identity differs");
  *prompt = std::move(snapshot);
  return {};
}

ObjectReply RealtimePromptSettings::Compose(
    const Json& overrides, std::optional<PromptSnapshot> prompt,
    bool referenced, RealtimePromptSelection* selected) const {
  RealtimePromptSelection next{defaults_, std::move(prompt)};
  if (next.prompt) {
    auto fields = next.prompt->request;
    // The prefix remains private Responses-shaped input, not public session
    // settings or fabricated conversation items. Execution must account it.
    std::erase_if(fields.object,
                  [](const auto& field) { return field.first == "input"; });
    MergeSettings(&next.settings, fields);
  }
  MergeSettings(&next.settings, overrides);
  if (referenced)
    Set(&next.settings, "prompt",
        next.prompt ? next.prompt->reference : Json());
  auto result = Shape(next.settings, "session.update");
  if (Accepted(result)) result = Bounded(defaults_, overrides, next);
  if (!Accepted(result)) return result;
  *selected = std::move(next);
  return {};
}

ObjectReply RealtimePromptSettings::Update(
    const Json& update, const RealtimePromptResolver& resolve,
    const RealtimePromptValidator& validate) {
  auto result = Shape(update, "session.update");
  if (!Accepted(result)) return result;
  auto prompt = current_.prompt;
  result = Select(update, resolve, &prompt);
  if (!Accepted(result)) return result;
  auto overrides = overrides_;
  MergeSettings(&overrides, update);
  RealtimePromptSelection next;
  const bool referenced = referenced_ || update.Has("prompt");
  result = Compose(overrides, std::move(prompt), referenced, &next);
  if (!Accepted(result)) return result;
  if (next.settings.GetString("model") != current_.settings.GetString("model"))
    return Invalid("realtime_model_immutable", "Session model cannot change",
                   "session.model");
  const auto* tracing = current_.settings.Find("tracing");
  if (tracing && !tracing->IsNull() &&
      (!next.settings.Has("tracing") ||
       OutputJson(*tracing) != OutputJson(*next.settings.Find("tracing"))))
    return Invalid("realtime_tracing_immutable",
                   "Enabled tracing cannot change", "session.tracing");
  if (validate) result = validate(next);
  if (!Accepted(result)) return result;
  overrides_ = std::move(overrides);
  referenced_ = referenced;
  current_ = std::move(next);
  return {};
}

ObjectReply RealtimePromptSettings::Response(
    const Json& response, const RealtimePromptResolver& resolve,
    const RealtimePromptValidator& validate,
    RealtimePromptSelection* selected) const {
  if (!selected)
    return Error(500, "prompt_owner_missing", "Prompt response owner missing");
  auto result = Shape(response, "response.create");
  if (!Accepted(result)) return result;
  auto prompt = current_.prompt;
  result = Select(response, resolve, &prompt);
  if (!Accepted(result)) return result;
  RealtimePromptSelection next;
  result = Compose(overrides_, std::move(prompt),
                   referenced_ || response.Has("prompt"), &next);
  if (!Accepted(result)) return result;
  if (next.settings.GetString("model") != current_.settings.GetString("model"))
    return Invalid("realtime_model_immutable", "Session model cannot change",
                   "response.prompt");
  // Direct response options are still passed separately to the execution
  // planner, where they override these effective session defaults.
  if (validate) result = validate(next);
  if (!Accepted(result)) return result;
  *selected = std::move(next);
  return {};
}
}  // namespace q4t::server
