#include "q4t/server/completion_plan.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/token_logprobs.h"

#include <cmath>
#include <limits>

#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::Status;
using namespace q4t::server;

ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "base";
  config.model_aliases = {"org/alias", "中文"};
  config.token_vocab = 512;
  config.max_len = 64;
  return config;
}
bool Read(std::string_view fields, CompletionRequestPlan* plan,
           ChatPlanError* error) {
  Json req;
  return q4t::io::ParseJson("{\"model\":\"base\"," + std::string(fields) + '}',
                             &req, true, kCompletionJsonLimits) &&
         PlanCompletionRequest(req, Config(), plan, error);
}
Status Encode(std::string_view text, std::vector<uint32_t>* out) {
  out->clear();
  if (text == "ab") { *out = {256}; return Status(); }
  const std::string_view specials[] = {"<|endoftext|>", "<|fim_prefix|>",
                                      "<|fim_suffix|>", "<|fim_middle|>"};
  for (size_t i = 0; i < 4; ++i)
    if (text == specials[i]) {
      *out = {static_cast<uint32_t>(300 + i)};
      return Status();
    }
  for (unsigned char c : text) out->push_back(c);
  return Status();
}
Status Decode(const std::vector<uint32_t>& ids, std::string* text) {
  text->clear();
  const std::string_view specials[] = {"<|endoftext|>", "<|fim_prefix|>",
                                      "<|fim_suffix|>", "<|fim_middle|>"};
  for (auto id : ids) {
    if (id < 256) *text += static_cast<char>(id);
    else if (id == 256) *text += "ab";
    else if (id >= 300 && id <= 303) *text += specials[id - 300];
    else return Status::Fail("unknown token");
  }
  return Status();
}
bool Parse(const std::string& text, Json* value) {
  return q4t::io::ParseJson(text, value, true).ok();
}
}  // namespace

Q4T_TEST(completion_models_stable_list_aliases_and_exact_url_decode) {
  ModelCatalog catalog;
  Q4T_CHECK(catalog.Configure("base", {"org/alias", "中文", "literal%2Fid"}, 42));
  const auto first = catalog.ListJson();
  Q4T_CHECK(catalog.ListJson() == first);
  Json list, item;
  Q4T_CHECK(Parse(first, &list));
  Q4T_CHECK(list.GetArray("data")->array.size() == 4);
  Q4T_CHECK(Parse(catalog.RetrieveJson("org/alias"), &item));
  Q4T_CHECK(item.GetInt("created") == 42 && item.GetString("owned_by") == "q4t");
  Q4T_CHECK(catalog.RetrieveJson("missing").empty());
  std::string id;
  Q4T_CHECK(DecodeModelPath("/v1/models/org%2Falias", &id) && id == "org/alias");
  Q4T_CHECK(DecodeModelPath("/v1/models/literal%252Fid", &id) &&
            id == "literal%2Fid");
  Q4T_CHECK(DecodeModelPath("/v1/models/%E4%B8%AD%E6%96%87", &id) && id == "中文");
  for (const auto* path : {"/v1/models/", "/v1/models/a?b", "/v1/models/%",
                            "/v1/models/%00", "/v1/models/%FF",
                            "/v1/models/%C0%80", "/v1/models/%ED%A0%80"})
    Q4T_CHECK(!DecodeModelPath(path, &id) && id.empty());
  Q4T_CHECK(!catalog.Configure("base", {"same", "same"}, 4));
  Q4T_CHECK(catalog.ListJson() == first);
  ServerOptions options;
  options.model_dir = "/read-only-model";
  const std::string_view args[] = {"--served-model-name", "new",
                                  "--model-alias", "base",
                                  "--model-alias", "org/alias"};
  Q4T_CHECK(ParseServerOptions(args, &options));
  Q4T_CHECK(options.model_aliases == std::vector<std::string>({"base", "org/alias"}));
  const std::string_view bad[] = {"--model-alias", "base"};
  Q4T_CHECK(!ParseServerOptions(bad, &options));
  Q4T_CHECK(options.model_aliases.size() == 2);
  return true;
}

Q4T_TEST(completion_standard_defaults_null_shapes_and_model_errors) {
  CompletionRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(Read(R"("prompt":null)", &plan, &error));
  Q4T_CHECK(plan.max_tokens == 16 && plan.n == 1 && plan.best_of == 1 &&
            plan.logprobs == -1 && !plan.echo && !plan.stream &&
            plan.sampling.temperature == 1);
  Q4T_CHECK(Read(R"("prompt":"", "n":3,"best_of":null,"max_tokens":0,
                    "echo":true,"logprobs":0,"seed":9007199254740993)",
                  &plan, &error));
  Q4T_CHECK(plan.best_of == 3 && plan.CandidateCount() == 3 &&
            plan.max_tokens == 0 && plan.sampling.seed == 9007199254740993LL &&
            plan.ScoresPrompt());
  for (const auto* prompt : {"\"raw <think>\"", "[\"a\",\"b\"]", "[1,2]",
                              "[[1],[2,3]]", "[]", "[[]]"}) {
    Q4T_CHECK(Read(std::string("\"prompt\":") + prompt, &plan, &error));
  }
  Json request;
  Q4T_CHECK(Parse(R"({"model":"org/alias","prompt":"q"})", &request));
  Q4T_CHECK(PlanCompletionRequest(request, Config(), &plan, &error));
  Q4T_CHECK(plan.response_model == "org/alias");
  Q4T_CHECK(Parse(R"({"model":"missing","prompt":"q"})", &request));
  Q4T_CHECK(!PlanCompletionRequest(request, Config(), &plan, &error));
  Q4T_CHECK(error.http_status == 404 && error.code == "model_not_found");
  ChatRequestPlan chat;
  Q4T_CHECK(Parse(R"({"model":"org/alias","prompt":"q"})", &request));
  Q4T_CHECK(PlanChatRequest(request, Config(), &chat, &error));
  return true;
}

Q4T_TEST(completion_invalid_values_and_combinations_are_transactional) {
  CompletionRequestPlan plan;
  ChatPlanError error;
  const std::pair<const char*, const char*> cases[] = {
      {R"("prompt":[1.000000000000001])", "prompt[0]"},
      {R"("prompt":[-1])", "prompt[0]"},
      {R"("prompt":[512])", "prompt[0]"},
      {R"("prompt":["a",[2]])", "prompt[1]"},
      {R"("prompt":[[2],null])", "prompt[1]"},
      {R"("prompt":false)", "prompt"},
      {R"("prompt":"a","logprobs":true)", "logprobs"},
      {R"("prompt":"a","logprobs":6)", "logprobs"},
      {R"("prompt":"a","max_tokens":-1)", "max_tokens"},
      {R"("prompt":"a","n":3,"best_of":2)", "best_of"},
      {R"("prompt":"a","best_of":2,"stream":true)", "best_of"},
      {R"("prompt":"a","stream_options":{})", "stream_options"},
      {R"("prompt":"a","stream":true,"stream_options":{"include_usage":null})",
       "stream_options.include_usage"},
      {R"("prompt":"a","logit_bias":{"512":1})", "logit_bias[\"512\"]"},
      {R"("prompt":"a","suffix":3)", "suffix"}};
  for (const auto& [body, param] : cases) {
    Q4T_CHECK(!Read(body, &plan, &error));
    Q4T_CHECK(error.param == param && plan.prompts.empty());
  }
  Q4T_CHECK(!Read(R"("prompt":["a","b"],"best_of":128)", &plan, &error));
  Q4T_CHECK(error.http_status == 503 && error.code == "completion_resource_exhausted");
  return true;
}

Q4T_TEST(completion_prompt_ids_fim_and_empty_document_preserve_actual_input) {
  CompletionRequestPlan plan;
  ChatPlanError error;
  std::vector<PreparedCompletionPrompt> prompts;
  uint64_t inputs = 0, outputs = 0;
  Q4T_CHECK(Read(R"("prompt":[[97,98],[256]],"suffix":"x","n":2,"max_tokens":3)",
                  &plan, &error));
  Q4T_CHECK(PrepareCompletionPrompts(plan, Config(), Encode, Decode, &prompts,
                                    &inputs, &outputs, &error));
  Q4T_CHECK(prompts[0].tokens == std::vector<uint32_t>({301,97,98,302,120,303}));
  Q4T_CHECK(prompts[1].tokens == std::vector<uint32_t>({301,256,302,120,303}));
  Q4T_CHECK(prompts[0].echo_text == "ab" && prompts[1].echo_text == "ab");
  Q4T_CHECK(prompts[0].echo_begin == 1 && prompts[0].echo_end == 3);
  Q4T_CHECK(inputs == 11 && outputs == 12);
  Q4T_CHECK(Read(R"("prompt":null,"max_tokens":0)", &plan, &error));
  Q4T_CHECK(PrepareCompletionPrompts(plan, Config(), Encode, Decode, &prompts,
                                    &inputs, &outputs, &error));
  Q4T_CHECK(prompts[0].tokens == std::vector<uint32_t>{300} && inputs == 1 &&
            outputs == 0 && prompts[0].echo_begin == prompts[0].echo_end);
  return true;
}

Q4T_TEST(completion_batch_budget_precedes_execution_and_charges_all_candidates) {
  CompletionRequestPlan plan;
  ChatPlanError error;
  std::vector<PreparedCompletionPrompt> prompts;
  uint64_t inputs = 0, outputs = 0;
  Q4T_CHECK(Read(R"("prompt":["abc","defg"],"best_of":3,"max_tokens":4)",
                  &plan, &error));
  auto config = Config();
  ClientLimits limits;
  limits.max_prompt_tokens = 6;
  limits.max_output_tokens = 24;
  config.client_limits = &limits;
  Q4T_CHECK(!PrepareCompletionPrompts(plan, config, Encode, Decode, &prompts,
                                     &inputs, &outputs, &error));
  Q4T_CHECK(prompts.empty() && inputs == 0 && outputs == 0 &&
            error.code == "client_prompt_limit_exceeded");
  limits.max_prompt_tokens = 7;
  limits.max_output_tokens = 23;
  Q4T_CHECK(!PrepareCompletionPrompts(plan, config, Encode, Decode, &prompts,
                                     &inputs, &outputs, &error));
  Q4T_CHECK(error.code == "client_output_limit_exceeded");
  limits.max_output_tokens = 24;
  Q4T_CHECK(PrepareCompletionPrompts(plan, config, Encode, Decode, &prompts,
                                    &inputs, &outputs, &error));
  Q4T_CHECK(inputs == 7 && outputs == 24);
  config.client_limits = nullptr;
  config.max_len = 7;
  Q4T_CHECK(!PrepareCompletionPrompts(plan, config, Encode, Decode, &prompts,
                                     &inputs, &outputs, &error));
  Q4T_CHECK(error.param == "prompt[1]" && prompts.empty());
  Q4T_CHECK(Read(R"("prompt":"abcdefg","max_tokens":0,"echo":true)", &plan, &error));
  Q4T_CHECK(PrepareCompletionPrompts(plan, config, Encode, Decode, &prompts,
                                    &inputs, &outputs, &error));
  return true;
}

Q4T_TEST(completion_probability_rows_use_actual_scores_and_null_first_token) {
  const auto decode = [](int32_t id, std::string* out) {
    *out = std::string(1, char('a' + id));
    return Status();
  };
  CompletionProbabilityRow first, selected;
  Q4T_CHECK(CompletionProbability(0, nullptr, decode, &first));
  SampledToken sample;
  sample.token = 2;
  sample.logprob = -9999;
  sample.raw_logprob = -3;
  sample.top = {{0,-1},{1,-2}};
  Q4T_CHECK(CompletionProbability(2, &sample, decode, &selected));
  CompletionProbabilityRows rows;
  Q4T_CHECK(rows.Add(first, 0));
  Q4T_CHECK(rows.Add(selected, 1));
  Json result;
  Q4T_CHECK(Parse(rows.Json(), &result));
  Q4T_CHECK(result.GetArray("token_logprobs")->array[0].IsNull());
  Q4T_CHECK(result.GetArray("token_logprobs")->array[1].number == -3);
  const auto& top = result.GetArray("top_logprobs")->array[1];
  Q4T_CHECK(top.object.size() == 3 && top.GetNumber("c") == -3);
  sample.raw_logprob = std::numeric_limits<double>::quiet_NaN();
  Q4T_CHECK(!CompletionProbability(2, &sample, decode, &selected));
  return true;
}

Q4T_TEST(completion_echo_scalar_offsets_survive_split_utf8_and_repair) {
  const std::string raw = "a中" + std::string("\xff",1) + "🚀";
  const size_t starts[] = {0,1,2,3,4,5,6,7,8};
  std::string text;
  std::vector<size_t> offsets;
  Q4T_CHECK(CompletionEchoOffsets(raw, starts, &text, &offsets));
  Q4T_CHECK(text == "a中�🚀");
  Q4T_CHECK(offsets == std::vector<size_t>({0,1,1,1,2,3,3,3,3}));
  const size_t bad[] = {1,0};
  Q4T_CHECK(!CompletionEchoOffsets(raw, bad, &text, &offsets));
  return true;
}

Q4T_TEST(completion_generated_probability_offsets_follow_stream_and_stop) {
  TokenLogprobs probabilities(true, true, 2);  // Two echoed Unicode scalars.
  detail::Utf8Stream utf8;
  const std::vector<std::string> pieces = {"\xe4", "\xb8", "\xad", "xSTOP"};
  const auto decode = [&](int32_t id, std::string* out) {
    *out = pieces.at(id);
    return Status();
  };
  for (size_t i = 0; i < 4; ++i) {
    SampledToken sample;
    sample.token = i;
    sample.raw_logprob = -double(i+1);
    Q4T_CHECK(probabilities.Push(sample, pieces[i], decode));
    std::vector<detail::Utf8Span> spans;
    const auto text = utf8.Append(pieces[i], &spans);
    probabilities.Map(spans);
    if (!text.empty()) {
      ResponseDelta delta;
      delta.kind = ResponseDelta::Kind::kContent;
      delta.text = i == 3 ? "x" : "中";
      delta.source_begin = i == 3 ? 3 : 0;
      delta.source_end = i == 3 ? 4 : 3;
      std::string chunk;
      Q4T_CHECK(probabilities.Emit(delta, &chunk));
      probabilities.DiscardBefore(delta.source_end);
    }
  }
  Json result;
  Q4T_CHECK(Parse(probabilities.Json(), &result));
  const auto& offsets = result.GetArray("text_offset")->array;
  Q4T_CHECK(offsets.size() == 4 && offsets[0].number == 2 &&
            offsets[1].number == 2 && offsets[2].number == 2 &&
            offsets[3].number == 3);
  Q4T_CHECK(result.GetArray("tokens")->array[3].str == "xSTOP");
  Q4T_CHECK(result.GetArray("token_logprobs")->array[3].number == -4);
  return true;
}

Q4T_TEST(completion_best_of_ranks_mean_counts_hidden_work_and_preserves_n_order) {
  CompletionChoices choices(2, 1, 3, false);
  const std::string texts[] = {"long-better", "short-worse", "tie-later"};
  for (int p = 0; p < 2; ++p) {
    for (int i = 0; i < 3; ++i) {
      CompletionCandidate candidate;
      candidate.text = texts[i];
      candidate.finish = "stop";
      candidate.tokens = i == 1 ? 1 : 4;
      candidate.score_tokens = candidate.tokens;
      candidate.sum_logprob = i == 1 ? -2 : -4;
      Q4T_CHECK(choices.Add(p*3+i, p+2, std::move(candidate)));
    }
  }
  std::string wire;
  Q4T_CHECK(choices.Json("c", "m", 7, &wire));
  Json json;
  Q4T_CHECK(Parse(wire, &json));
  const auto& results = json.GetArray("choices")->array;
  Q4T_CHECK(results.size() == 2 && results[0].GetString("text") == "long-better" &&
            results[1].GetInt("index") == 1);
  Q4T_CHECK(json.Find("usage")->GetInt("prompt_tokens") == 5 &&
            json.Find("usage")->GetInt("completion_tokens") == 18);
  CompletionChoices all(1, 2, 2, false);
  for (int i = 0; i < 2; ++i) {
    CompletionCandidate candidate;
    candidate.text = std::to_string(i);
    candidate.finish = "length";
    candidate.tokens = candidate.score_tokens = 1;
    candidate.sum_logprob = i ? -1 : -2;
    Q4T_CHECK(all.Add(i, 3, std::move(candidate)));
  }
  Q4T_CHECK(all.Json("c", "m", 7, &wire) && Parse(wire, &json));
  Q4T_CHECK(json.GetArray("choices")->array[0].GetString("text") == "0");
  return true;
}

Q4T_TEST(completion_sse_shape_usage_and_optional_padding) {
  const auto plain = CompletionChunk("c", "m", 7, 2, "中\n", "", "null", true, false);
  Json event;
  Q4T_CHECK(Parse(plain.substr(6, plain.size()-10), &event));
  Q4T_CHECK(event.GetString("object") == "text_completion" && !event.Has("obfuscation"));
  const auto& choice = event.GetArray("choices")->array[0];
  Q4T_CHECK(choice.GetString("text") == "中\n" && choice.GetInt("index") == 2 &&
            choice.Find("finish_reason")->IsNull() && event.Find("usage")->IsNull());
  const auto padded = CompletionChunk("c", "m", 7, 0, "", "length", "null", false, true);
  Q4T_CHECK(Parse(padded.substr(6, padded.size()-10), &event));
  Q4T_CHECK(!event.GetString("obfuscation").empty() && !event.Has("usage"));
  const auto usage = CompletionUsageChunk("c", "m", 7,
      R"({"prompt_tokens":2,"completion_tokens":3,"total_tokens":5})", false);
  Q4T_CHECK(Parse(usage.substr(6, usage.size()-10), &event));
  Q4T_CHECK(event.GetArray("choices")->array.empty() &&
            event.Find("usage")->GetInt("total_tokens") == 5);
  return true;
}
