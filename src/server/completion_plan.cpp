#include "q4t/server/completion_plan.h"

#include <algorithm>
#include <limits>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/text_stop.h"

namespace q4t::server {
namespace {
Status Fail(ChatPlanError* error, std::string param, std::string message,
            std::string code = "invalid_request", int status = 400) {
  *error = {std::move(code), std::move(param), status};
  return Status::Fail(std::move(message));
}
io::Json Number(int value) {
  io::Json result;
  result.type = io::Json::Type::kNumber;
  result.number = value;
  result.str = std::to_string(value);
  return result;
}
io::Json Boolean(bool value) {
  io::Json result;
  result.type = io::Json::Type::kBool;
  result.boolean = value;
  return result;
}
void Put(io::Json* object, const char* key, io::Json value) {
  for (auto& [name, item] : object->object) {
    if (name == key) {
      item = std::move(value);
      return;
    }
  }
  object->object.emplace_back(key, std::move(value));
}
}  // namespace

Status PlanCompletionRequest(const io::Json& req, const ChatPlanConfig& config,
                             CompletionRequestPlan* plan,
                             ChatPlanError* error) {
  *plan = {};
  *error = {};
  CompletionRequestPlan result;
  auto status = InputFields(
      req, {"model", "prompt", "best_of", "echo", "frequency_penalty",
            "logit_bias", "logprobs", "max_tokens", "n", "presence_penalty",
            "seed", "stop", "stream", "stream_options", "suffix",
            "temperature", "top_p", "user", "request_id", "cancel_token",
            "request_timeout_ms"},
      &error->param, "");
  if (!status) return status;
  const auto* model = req.Find("model");
  if (!model || !model->IsString() || !ValidModelIdentifier(model->str))
    return Fail(error, "model", "model must be a nonempty model identifier");
  if (!IsServedModel(model->str, config.model, config.model_aliases))
    return Fail(error, "model", "model is not registered by this server",
                "model_not_found", 404);
  result.response_model = model->str;
  status = ValidateRequestCredentials(req);
  if (!status) {
    const auto* id = req.Find("request_id");
    error->param = !id || !id->IsString() || !ValidRequestId(id->str)
                       ? "request_id"
                       : "cancel_token";
    return status;
  }
  if (const auto* value = req.Find("request_timeout_ms");
      value && (!IsInputInteger(*value) || value->number < 1 ||
                value->number > 10800000))
    return Fail(error, "request_timeout_ms",
                "request_timeout_ms must be an integer in [1,10800000]");
  const auto integer = [&](const char* key, int low, int high, int* out) {
    const auto* value = req.Find(key);
    if (!value || value->IsNull()) return Status();
    if (!IsInputInteger(*value) || value->number < low || value->number > high)
      return Fail(error, key, "invalid integer completion option");
    *out = static_cast<int>(value->number);
    return Status();
  };
  status = integer("n", 1, 128, &result.n);
  result.best_of = result.n;
  if (status) status = integer("best_of", 1, 128, &result.best_of);
  if (status) status = integer("logprobs", 0, 5, &result.logprobs);
  if (status)
    status = integer("max_tokens", 0, std::numeric_limits<int>::max(),
                     &result.max_tokens);
  if (!status) return status;
  if (result.best_of < result.n)
    return Fail(error, "best_of", "best_of must be at least n");
  for (const auto& [key, output] :
       {std::pair{"echo", &result.echo}, std::pair{"stream", &result.stream}}) {
    const auto* value = req.Find(key);
    if (value && !value->IsNull()) {
      if (!value->IsBool())
        return Fail(error, key, "completion flag must be boolean or null");
      *output = value->boolean;
    }
  }
  if (result.stream && result.best_of > result.n)
    return Fail(error, "best_of", "best_of selection cannot be streamed");
  if (const auto* value = req.Find("suffix"); value && !value->IsNull()) {
    if (!value->IsString())
      return Fail(error, "suffix", "suffix must be a string or null");
    result.suffix = value->str;
  }
  if (const auto* value = req.Find("user");
      value && !value->IsNull() && !value->IsString())
    return Fail(error, "user", "user must be a string or null");
  if (const auto* options = req.Find("stream_options");
      options && !options->IsNull()) {
    status = InputFields(*options, {"include_usage", "include_obfuscation"},
                          &error->param, "stream_options");
    if (!status) return status;
    if (!result.stream)
      return Fail(error, "stream_options", "stream_options requires stream");
    for (const auto& [key, target] :
         {std::pair{"include_usage", &result.include_usage},
          std::pair{"include_obfuscation", &result.include_obfuscation}}) {
      if (const auto* value = options->Find(key)) {
        if (!value->IsBool())
          return Fail(error, std::string("stream_options.") + key,
                      "stream option must be boolean");
        *target = value->boolean;
      }
    }
  }

  const auto add_prompt = [&](const io::Json* value, const std::string& path) {
    CompletionPrompt prompt;
    if (!value || value->IsNull()) {
      // Empty document is seeded during tokenization, not a Chat message.
    } else if (value->IsString()) {
      prompt.text = value->str;
    } else if (value->IsArray()) {
      prompt.tokenized = true;
      for (size_t i = 0; i < value->array.size(); ++i) {
        const auto& token = value->array[i];
        if (!IsInputInteger(token) || token.number < 0 ||
            config.token_vocab <= 0 || token.number >= config.token_vocab)
          return Fail(error, path + '[' + std::to_string(i) + ']',
                      "prompt token outside served vocabulary");
        prompt.input_tokens.push_back(static_cast<uint32_t>(token.number));
      }
    } else {
      return Fail(error, path, "invalid completion prompt");
    }
    result.prompts.push_back(std::move(prompt));
    return Status();
  };
  const auto* prompt = req.Find("prompt");
  if (prompt && prompt->IsArray() && !prompt->array.empty() &&
      (prompt->array.front().IsString() || prompt->array.front().IsArray())) {
    if (prompt->array.size() > kCompletionCandidates)
      return Fail(error, "prompt", "completion batch exceeds 128 prompts",
                  "completion_resource_exhausted", 503);
    const bool strings = prompt->array.front().IsString();
    for (size_t i = 0; i < prompt->array.size(); ++i) {
      const auto& item = prompt->array[i];
      const auto path = "prompt[" + std::to_string(i) + ']';
      if ((strings && !item.IsString()) || (!strings && !item.IsArray()))
        return Fail(error, path, "completion prompts must be homogeneous");
      status = add_prompt(&item, path);
      if (!status) return status;
    }
  } else {
    status = add_prompt(prompt, "prompt");
    if (!status) return status;
  }
  if (static_cast<size_t>(result.CandidateCount()) > kCompletionCandidates)
    return Fail(error, "best_of", "completion batch exceeds 128 candidates",
                "completion_resource_exhausted", 503);
  result.generation_request.type = io::Json::Type::kObject;
  for (const auto* key : {"model", "temperature", "top_p", "seed",
                           "frequency_penalty", "presence_penalty",
                           "logit_bias", "user", "request_id", "cancel_token",
                           "request_timeout_ms", "stop", "stream_options"})
    if (const auto* value = req.Find(key))
      Put(&result.generation_request, key, *value);
  Put(&result.generation_request, "stream", Boolean(result.stream));
  Put(&result.generation_request, "max_tokens", Number(result.max_tokens));
  Put(&result.generation_request, "n", Number(result.CandidateCount()));
  const bool scores = result.logprobs >= 0 || result.best_of > result.n;
  Put(&result.generation_request, "logprobs", Boolean(scores));
  if (scores)
    Put(&result.generation_request, "top_logprobs",
        Number(std::max(0, result.logprobs)));
  status = ParseSamplingOptions(result.generation_request, &result.sampling,
                                 &error->param);
  if (!status) return status;
  for (const auto& [token, bias] : result.sampling.logit_bias) {
    (void)bias;
    if (token >= config.token_vocab)
      return Fail(error, "logit_bias[\"" + std::to_string(token) + "\"]",
                  "logit_bias token outside served vocabulary");
  }
  status = ParseTextStop(req, false, &result.stops, &error->param);
  if (!status) return status;
  *plan = std::move(result);
  return Status();
}

Status PrepareCompletionPrompts(
    const CompletionRequestPlan& plan, const ChatPlanConfig& config,
    const PromptEncoder& encode, const CompletionDecoder& decode,
    std::vector<PreparedCompletionPrompt>* prompts, uint64_t* prompt_tokens,
    uint64_t* output_tokens, ChatPlanError* error) {
  prompts->clear();
  *prompt_tokens = *output_tokens = 0;
  *error = {};
  if (plan.prompts.empty() || plan.CandidateCount() < 1 ||
      plan.CandidateCount() > static_cast<int>(kCompletionCandidates) ||
      plan.max_tokens < 0 || plan.best_of < 1 ||
      config.token_vocab <= 0 || config.max_len <= 0 || config.max_seq < 1 ||
      (!config.capacities.empty() &&
       config.capacities.size() != static_cast<size_t>(config.max_seq)))
    return Fail(error, "", "invalid completion planning state",
                "completion_internal_error", 500);
  std::vector<PreparedCompletionPrompt> prepared;
  uint64_t inputs = 0, outputs = 0;
  const auto special = [&](const char* name, uint32_t* token) {
    std::vector<uint32_t> ids;
    std::string decoded;
    auto status = encode(name, &ids);
    if (status) status = decode(ids, &decoded);
    if (!status || ids.size() != 1 || decoded != name ||
        ids[0] >= static_cast<uint32_t>(config.token_vocab))
      return Fail(error, "prompt", "required document/FIM token unavailable",
                  "completion_tokenizer_unavailable", 503);
    *token = ids[0];
    return Status();
  };
  for (size_t i = 0; i < plan.prompts.size(); ++i) {
    const auto& source = plan.prompts[i];
    PreparedCompletionPrompt value;
    const auto path = plan.prompts.size() == 1
                          ? "prompt"
                          : "prompt[" + std::to_string(i) + ']';
    Status status;
    if (source.tokenized) {
      value.tokens = source.input_tokens;
      status = decode(value.tokens, &value.echo_text);
    } else {
      value.echo_text = source.text;
      status = encode(source.text, &value.tokens);
    }
    if (!status) return Fail(error, path, status.message());
    value.echo_end = value.tokens.size();
    if (plan.suffix) {
      uint32_t prefix = 0, suffix = 0, middle = 0;
      status = special("<|fim_prefix|>", &prefix);
      if (status) status = special("<|fim_suffix|>", &suffix);
      if (status) status = special("<|fim_middle|>", &middle);
      if (!status) return status;
      std::vector<uint32_t> suffix_ids;
      status = encode(*plan.suffix, &suffix_ids);
      if (!status) return Fail(error, "suffix", status.message());
      value.tokens.insert(value.tokens.begin(), prefix);
      value.echo_begin = 1;
      ++value.echo_end;
      value.tokens.push_back(suffix);
      value.tokens.insert(value.tokens.end(), suffix_ids.begin(),
                           suffix_ids.end());
      value.tokens.push_back(middle);
    } else if (value.tokens.empty()) {
      uint32_t seed;
      status = special("<|endoftext|>", &seed);
      if (!status) return status;
      value.tokens.push_back(seed);
    }
    for (uint32_t token : value.tokens)
      if (token >= static_cast<uint32_t>(config.token_vocab))
        return Fail(error, path, "encoded token outside served vocabulary");
    const uint64_t required = value.tokens.size() + uint64_t(plan.max_tokens);
    if (required > static_cast<uint64_t>(config.max_len))
      return Fail(error, path, "prompt plus output exceeds context capacity",
                  "context_length_exceeded");
    auto& budget = value.budget;
    budget.requested_output = budget.capped_output = budget.available_output =
        plan.max_tokens;
    budget.total_output = uint64_t(plan.max_tokens) * plan.best_of;
    budget.required_capacity = config.capacities.empty() ? 0 : required;
    budget.output_source = "max_tokens";
    for (int slot = 0; slot < config.max_seq; ++slot) {
      const auto capacity = config.capacities.empty()
                                ? config.max_len
                                : config.capacities[slot];
      if (required <= static_cast<uint64_t>(capacity))
        budget.eligible_slots.push_back(slot);
    }
    if (budget.eligible_slots.empty())
      return Fail(error, path, "prompt plus output exceeds slot capacity",
                  "context_length_exceeded");
    inputs += value.tokens.size();
    outputs += budget.total_output;
    prepared.push_back(std::move(value));
  }
  if (config.client_limits && inputs > config.client_limits->max_prompt_tokens)
    return Fail(error, "prompt", "batch exceeds client prompt token limit",
                "client_prompt_limit_exceeded");
  if (config.client_limits && outputs > config.client_limits->max_output_tokens)
    return Fail(error, "max_tokens", "all candidates exceed client output limit",
                "client_output_limit_exceeded");
  *prompt_tokens = inputs;
  *output_tokens = outputs;
  *prompts = std::move(prepared);
  return Status();
}

}  // namespace q4t::server
