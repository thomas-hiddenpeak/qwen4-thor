#include "chat_server_internal.h"

#include <algorithm>

#include "chat_model_step.h"
#include "object_protocol.h"
#include "q4t/server/decision_scoring.h"

namespace q4t::server {
namespace {
using namespace object_detail;
size_t OptionCount(const Json& question) {
  const auto type = question.GetString("type");
  if (type == "predicate") return 2;
  return question.Find(type == "choice" ? "choices" : "levels")->array.size();
}
}  // namespace

ObjectReply ChatServer::RunDecisionRequest(
    int fd, const Json& request, const ChatPlanConfig& config,
    ClientRateLease& rate, const std::shared_ptr<RequestControl>& control,
    const std::function<bool()>& cancelled) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !control || !http->Client().Allowed())
    return Error(503, "decision_owner_unavailable",
                 "Authenticated Decision owner unavailable");
  const auto& questions = request.Find("questions")->array;
  size_t count = 0;
  bool needs_predicate = false;
  for (const auto& q : questions) {
    if (q.GetString("type") == "predicate")
      needs_predicate = true;
    else
      count = std::max(count, OptionCount(q));
  }
  std::vector<DecisionLabel> codes, predicates;
  {
    auto lock = LockUnlessCancelled(tok_mu_, cancelled);
    if (!lock.owns_lock() || !tok_)
      return Error(503, "decision_tokenizer_unavailable",
                   "Tokenizer unavailable");
    const auto encode = [&](std::string_view text, std::vector<uint32_t>* ids) {
      return tok_->Encode(
          text, ids,
          {.cancelled = cancelled, .recognize_special_tokens = false});
    };
    Status status;
    if (count)
      status =
          MakeDecisionLabels(DecisionLabels::kCodes, count, encode, &codes);
    if (status && needs_predicate)
      status = MakeDecisionLabels(DecisionLabels::kPredicate, 2, encode,
                                  &predicates);
    if (!status)
      return Error(503, "decision_codebook_unavailable", status.message());
  }
  Json shared;
  auto status = DecisionSharedMessages(request, &shared);
  if (!status) return Error(400, "invalid_request", status.message());
  ModelPreparation accounting(clients_, http->Client(), rate, 0, 0,
                              questions.size());
  auto answers = Array();
  for (const auto& question : questions) {
    if (cancelled())
      return Error(409, control->Reason(), "Decision request was cancelled");
    const auto& alphabet =
        question.GetString("type") == "predicate" ? predicates : codes;
    std::vector<DecisionLabel> labels(alphabet.begin(),
                                      alphabet.begin() + OptionCount(question));
    labels.push_back(alphabet.back());
    Json body;
    status = DecisionStepRequest(request, shared, question, labels, &body);
    if (!status) return Error(500, "decision_plan_invalid", status.message());
    DecisionScore score{question, labels, {}};
    InternalModelStep step{
        body, config, accounting, [] {return ObjectReply{}; },
        {},   false,  {},         {},
        0,    0,      nullptr,    &score};
    auto reply = RunInternalModelStep(fd, &step, control);
    if (!reply.ok()) return reply;
    if (score.token < 0 || score.answer.IsNull() || step.output_tokens != 1)
      return Error(502, "decision_scores_missing",
                   "Native model did not complete its Decision");
    answers.array.push_back(std::move(score.answer));
  }
  http->RefreshUsage();
  const auto usage = http->Usage().Total();
  if (!usage)
    return Error(503, "decision_usage_unavailable",
                 "Actual model usage unavailable");
  ObjectReply reply;
  status =
      BuildDecisionResult(request, std::move(answers), *usage, &reply.value);
  return status ? reply
                : Error(502, "decision_output_invalid", status.message());
}
}  // namespace q4t::server
