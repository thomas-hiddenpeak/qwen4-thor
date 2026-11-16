#include "q4t/server/chat_moderation.h"

#include <limits>
#include <utility>

#include "q4t/server/chat_choices.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Json PublicReport(const Json& report) {
  if (report.GetString("type") == "error") return report;
  return Object({{"type", String("moderation_results")},
                 {"model", String(report.GetString("model"))},
                 {"results", Array({report})}});
}
}  // namespace

ChatModeration::ChatModeration(ModerationOptions options)
    : options_(std::move(options)),
      input_(ModerationError("moderation_not_executed",
                             "Input classification has not executed")),
      output_(ModerationError("moderation_not_executed",
                              "Output classification has not executed")) {}

Status ChatModeration::Record(bool input, const io::Json& report,
                              ModerationUsage usage) {
  auto& done = input ? input_done_ : output_done_;
  if (!Enabled() || done || (!input && !input_done_) ||
      !ValidModerationReport(report) || usage.input_tokens < 0 ||
      usage.output_tokens < 0 || usage.model_steps < 0 ||
      usage.model_steps > 1 || usage.output_tokens > kMaxModerationStepTokens ||
      usage.input_tokens > INT64_MAX - usage_.input_tokens ||
      (report.GetString("type") != "error" &&
       (report.GetString("model") != options_.model || usage.model_steps != 1 ||
        !usage.input_tokens || !usage.output_tokens)))
    return Status::Fail("Invalid Chat classification receipt or usage");
  (input ? input_ : output_) = report;
  usage_.input_tokens += usage.input_tokens;
  usage_.output_tokens += usage.output_tokens;
  usage_.model_steps += usage.model_steps;
  done = true;
  if (Decision(input) != ModerationDecision::kAllow) Discard();
  return {};
}

ModerationDecision ChatModeration::Decision(bool input) const {
  return DecideModeration(input ? options_.input : options_.output,
                          input ? input_ : output_);
}
bool ChatModeration::Blocked() const {
  return (input_done_ && Decision(true) == ModerationDecision::kBlocked) ||
         (output_done_ && Decision(false) == ModerationDecision::kBlocked);
}
bool ChatModeration::Ready() const {
  return !failed_ && input_done_ &&
         (Blocked() ||
          (output_done_ && Decision(false) == ModerationDecision::kAllow)) &&
         Decision(true) != ModerationDecision::kUnavailable;
}
Status ChatModeration::Capture(std::string_view wire) {
  if (failed_ || !Holding() || !input_done_ ||
      Decision(true) != ModerationDecision::kAllow ||
      wire.size() > kWireLimit - wire_.size()) {
    failed_ = true;
    Discard();
    return Status::Fail("Chat classification output hold unavailable");
  }
  wire_.append(wire);
  return {};
}
Status ChatModeration::Release(std::string* wire) {
  wire->clear();
  if (!Ready() || Blocked() || released_) {
    Discard();
    return Status::Fail("Chat classification has not approved output");
  }
  *wire = std::move(wire_);
  released_ = true;
  wire_.clear();
  return {};
}
void ChatModeration::Discard() { wire_.clear(); }

io::Json ChatModeration::Reports() const {
  return Object(
      {{"input", PublicReport(input_)}, {"output", PublicReport(output_)}});
}
io::Json ChatModeration::Usage() const {
  return Object({{"input_tokens", Number(usage_.input_tokens)},
                 {"output_tokens", Number(usage_.output_tokens)},
                 {"model_steps", Number(usage_.model_steps)}});
}
Status ChatModeration::Project(std::string_view original,
                               std::string* output) const {
  output->clear();
  io::Json response;
  auto status = io::ParseJson(std::string(original), &response, true,
                              kApplicationJsonLimits);
  if (!status || !Ready() ||
      response.GetString("object") != "chat.completion" ||
      !response.GetArray("choices"))
    return Status::Fail("Chat classification response is not publishable");
  if (Blocked()) {
    for (auto& choice : Field(response, "choices").array) {
      Set(&choice, "message",
          Object({{"role", String("assistant")}, {"content", Json()}}));
      Set(&choice, "finish_reason", String("content_filter"));
      Set(&choice, "logprobs", Json());
    }
  }
  Set(&response, "moderation", Reports());
  Set(&response, "q4t_moderation_usage", Usage());
  *output = OutputJson(response);
  if (output->size() > ChatChoices::kResponseLimit) {
    output->clear();
    return Status::Fail("Chat classification response exceeds storage");
  }
  return {};
}
std::string ChatModeration::StreamMetadata(std::string_view id,
                                           std::string_view model,
                                           int64_t created,
                                           bool include_usage) const {
  if (!Ready()) return {};
  auto chunk = Object({{"id", String(std::string(id))},
                       {"object", String("chat.completion.chunk")},
                       {"created", Number(created)},
                       {"model", String(std::string(model))},
                       {"choices", Array()},
                       {"moderation", Reports()},
                       {"q4t_moderation_usage", Usage()}});
  if (include_usage) Set(&chunk, "usage", Json());
  return "data: " + OutputJson(chunk) + "\r\n\r\n";
}
Status ChatModeration::CollectOutput(std::string_view completion,
                                     ModerationContent* content,
                                     ChatPlanError* error) {
  io::Json value;
  auto status = io::ParseJson(std::string(completion), &value, true,
                              kApplicationJsonLimits);
  const auto* choices = value.GetArray("choices");
  if (!status || !choices || choices->array.empty())
    return Status::Fail("Chat choices unavailable for classification");
  auto messages = Array();
  for (const auto& choice : choices->array) {
    const auto* message = choice.Find("message");
    if (!message || !message->IsObject())
      return Status::Fail("Chat message unavailable for classification");
    messages.array.push_back(*message);
  }
  return CollectModerationInput(messages, content, error);
}
Status ChatModeration::EmptyCompletion(int choices, std::string_view id,
                                       std::string_view model, int64_t created,
                                       std::string* output) {
  ChatChoices result(choices, false);
  for (int i = 0; i < choices; ++i) {
    auto status =
        result.Add(i, R"({"role":"assistant","content":null})",
                   "content_filter", "null", 0, 0, 0, false, -1, 0, 0);
    if (!status) return status;
  }
  return result.Json(id, model, created, output);
}
}  // namespace q4t::server
