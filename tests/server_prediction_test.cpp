#include "q4t/server/prediction.h"

#include "q4t/model/sequence.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_plan.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::Status;
using q4t::model::ModelSequence;
using Stage = ModelSequence::Stage;

bool Read(const std::string& body, PredictionOptions* options,
          std::string* param = nullptr) {
  q4t::io::Json request;
  SamplingOptions sampling;
  return q4t::io::ParseJson(body, &request, true).ok() &&
         ParseSamplingOptions(request, &sampling, param).ok() &&
         ParsePredictionOptions(request, sampling, options, param).ok();
}

Status Complete(ModelSequence& seq, bool success = true) {
  ModelSequence* pointer = &seq;
  return q4t::model::CompleteSequenceWork({&pointer, 1}, Status(), [&] {
    return success ? Status() : Status::Fail("injected completion failure");
  });
}
}  // namespace

Q4T_TEST(prediction_content_contract_and_parts_order) {
  PredictionOptions options;
  Q4T_CHECK(Read("{}", &options) && !options.enabled);
  Q4T_CHECK(Read(R"({"prediction":null,"n":2,"logprobs":true})", &options));
  Q4T_CHECK(!options.enabled);
  Q4T_CHECK(
      Read(R"({"prediction":{"type":"content","content":""}})", &options) &&
      options.enabled && options.content.empty());
  Q4T_CHECK(Read(R"({"prediction":{"type":"content","content":[
    {"type":"text","text":"甲"},{"type":"text","text":"\n乙"}]}})",
                 &options));
  Q4T_CHECK(options.content == "甲\n乙");
  std::string param;
  Q4T_CHECK(!Read(R"({"prediction":{"type":"content","content":[
    {"type":"text","text":3}]}})",
                  &options, &param));
  Q4T_CHECK(param == "prediction.content[0].text");
  Q4T_CHECK(!Read(R"({"prediction":{"type":"content","content":[]}})", &options,
                  &param) &&
            param == "prediction.content");
  Q4T_CHECK(!Read(R"({"prediction":{"type":"content","content":"a","x":1}})",
                  &options, &param) &&
            param == "prediction.x");
  return true;
}

Q4T_TEST(prediction_official_parameter_combinations) {
  for (const auto& field :
       {"\"n\":2", "\"logprobs\":true", "\"presence_penalty\":0.1",
        "\"frequency_penalty\":2", "\"tools\":[{}]",
        "\"max_completion_tokens\":8"}) {
    PredictionOptions options;
    std::string param;
    Q4T_CHECK(
        !Read(std::string(
                  "{\"prediction\":{\"type\":\"content\",\"content\":\"a\"},") +
                  field + '}',
              &options, &param));
    Q4T_CHECK(!param.empty());
  }
  PredictionOptions options;
  Q4T_CHECK(Read(R"({"prediction":{"type":"content","content":"x"},
    "n":null,"logprobs":false,"frequency_penalty":-1,"presence_penalty":-2,
    "max_tokens":10,"max_completion_tokens":null,"temperature":0.8,
    "top_p":0.7,"seed":9223372036854775807,"tools":[]})",
                 &options));
  return true;
}

Q4T_TEST(prediction_proposal_does_not_count_until_target_consumes) {
  const int32_t ids[] = {4, 5, 6, 4, 7};
  PredictionCursor prediction(ids);
  Q4T_CHECK(prediction.Next() == 4 && prediction.Accepted() == 0);
  Q4T_CHECK(prediction.Next() == 4 && prediction.Rejected() == 5);
  prediction.Consume(4, false);  // Hidden reasoning cannot consume content.
  Q4T_CHECK(prediction.Next() == 4 && prediction.Accepted() == 0);
  prediction.Consume(9, true);  // Inserted target text leaves proposal live.
  Q4T_CHECK(prediction.Next() == 4 && prediction.Completion(1) == 6);
  prediction.Consume(4, true);
  Q4T_CHECK(prediction.Next() == 5 && prediction.Accepted() == 1);
  prediction.Consume(6, true);  // Removed prediction token 5 remains rejected.
  Q4T_CHECK(prediction.Next() == 4 && prediction.Accepted() == 2);
  prediction.Consume(4, true);
  prediction.Consume(7, true);
  prediction.Consume(4, true);  // No reuse of already consumed occurrences.
  Q4T_CHECK(prediction.Next() == -1 && !prediction.Matches(4));
  Q4T_CHECK(prediction.Accepted() == 4 && prediction.Rejected() == 1);
  Q4T_CHECK(prediction.Completion(6) == 7);
  return true;
}

Q4T_TEST(prediction_partial_sequence_commit_waits_and_excludes_suffix) {
  ModelSequence seq;
  const int32_t prompt[] = {1, 2}, proposal[] = {3, 99};
  Q4T_CHECK(seq.Begin(0) && seq.Submit(prompt, Stage::kDecode, 8) &&
            Complete(seq));
  Q4T_CHECK(!seq.RetainSubmittedPrefix(1));
  Q4T_CHECK(seq.Submit(proposal, Stage::kDecode, 8));
  Q4T_CHECK(!seq.RetainSubmittedPrefix(0));
  Q4T_CHECK(!seq.RetainSubmittedPrefix(3));
  Q4T_CHECK(seq.RetainSubmittedPrefix(1));
  Q4T_CHECK(seq.HasPending() && seq.position == 2 &&
            seq.SubmittedPosition() == 3);
  Q4T_CHECK(seq.history == std::vector<int32_t>({1, 2}));
  Q4T_CHECK(Complete(seq));
  Q4T_CHECK(seq.history == std::vector<int32_t>({1, 2, 3}));
  Q4T_CHECK(seq.position == 3 && !seq.HasPending());
  return true;
}

Q4T_TEST(prediction_rollback_completion_failure_never_commits_prefix) {
  ModelSequence seq;
  const int32_t proposal[] = {3, 4};
  Q4T_CHECK(seq.Begin(0) && seq.Submit(proposal, Stage::kDecode, 8));
  Q4T_CHECK(seq.RetainSubmittedPrefix(1));
  Q4T_CHECK(!Complete(seq, false));
  Q4T_CHECK(seq.position == 0 && seq.history.empty() && !seq.HasPending());
  Q4T_CHECK(seq.stage == Stage::kFailed && !seq.RetainSubmittedPrefix(1));
  Q4T_CHECK(seq.End());
  return true;
}

Q4T_TEST(prediction_tokenization_shares_output_and_context_budget) {
  q4t::io::Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"prompt":"x","max_tokens":4,
    "prediction":{"type":"content","content":"abcd"}})",
                               &request));
  ChatPlanConfig config;
  config.max_len = 8;
  config.token_vocab = 256;
  ChatPromptBudget budget;
  ChatPlanError error;
  std::vector<uint32_t> tokens;
  const auto encode = [](std::string_view text, std::vector<uint32_t>* ids) {
    ids->assign(text.begin(), text.end());
    return Status();
  };
  Q4T_CHECK(
      EncodeChatPrompt(request, config, "x", encode, &tokens, &budget, &error));
  Q4T_CHECK(tokens.size() == 1 && budget.prediction_tokens.size() == 4);
  Q4T_CHECK(budget.total_output == 4 && budget.available_output == 4);
  Q4T_CHECK(!EncodeChatPrompt(request, config, "xxxxx", encode, &tokens,
                              &budget, &error));
  Q4T_CHECK(error.param == "prediction.content");
  return true;
}

Q4T_TEST(prediction_usage_includes_rejected_not_double_accepted) {
  ChatChoices choices(1, false);
  Q4T_CHECK(choices.Add(0, R"({"role":"assistant","content":"ok"})", "stop",
                        "null", 5, 7, 0, false, -1, 0, 0, true, 4, 2));
  q4t::io::Json usage;
  Q4T_CHECK(q4t::io::ParseJson(choices.UsageJson(), &usage));
  Q4T_CHECK(usage.GetInt("completion_tokens") == 7 &&
            usage.GetInt("total_tokens") == 12);
  const auto* details = usage.Find("completion_tokens_details");
  Q4T_CHECK(details && details->GetInt("accepted_prediction_tokens") == 4 &&
            details->GetInt("rejected_prediction_tokens") == 2);
  ChatChoices bad(1, false);
  Q4T_CHECK(
      !bad.Add(0, "{}", "stop", "null", 5, 7, 0, false, -1, 0, 0, true, 6, 2));
  Q4T_CHECK(
      !bad.Add(0, "{}", "stop", "null", 5, 7, 0, false, -1, 0, 0, false, 1, 0));
  return true;
}
