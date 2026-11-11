#include "q4t/server/embeddings_plan.h"

#include <algorithm>
#include <bit>
#include <charconv>
#include <cmath>

#include "q4t/server/input_contract.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/output_schema.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string param, std::string message,
               std::string code = "invalid_request", int status = 400) {
  *error = {std::move(code), std::move(param), status};
  return Status::Fail(std::move(message));
}
Status Tokens(const Json& value, int vocab, const std::string& param,
              std::vector<uint32_t>* tokens, ChatPlanError* error) {
  if (!value.IsArray() || value.array.empty() ||
      value.array.size() > kEmbeddingInputTokens)
    return Invalid(error, param, "Expected 1..8192 token IDs");
  for (size_t i = 0; i < value.array.size(); ++i) {
    const auto& token = value.array[i];
    if (!IsInputInteger(token) || token.number < 0 || token.number >= vocab)
      return Invalid(error, param + "[" + std::to_string(i) + "]",
                     "Token ID must be an exact integer in the served vocab");
    tokens->push_back(static_cast<uint32_t>(token.number));
  }
  return {};
}
std::string Base64(std::span<const float> vector) {
  constexpr char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string result;
  result.reserve((vector.size() * 4 + 2) / 3 * 4);
  uint32_t bits = 0;
  unsigned count = 0;
  for (const float value : vector) {
    const auto word = std::bit_cast<uint32_t>(value);
    for (unsigned shift = 0; shift < 32; shift += 8) {
      bits = (bits << 8) | ((word >> shift) & 255);
      count += 8;
      while (count >= 6) {
        count -= 6;
        result += alphabet[(bits >> count) & 63];
      }
    }
  }
  if (count) result += alphabet[(bits << (6 - count)) & 63];
  while (result.size() % 4) result += '=';
  return result;
}
}  // namespace

Status PlanEmbeddingsRequest(const Json& request, const ChatPlanConfig& config,
                             EmbeddingsRequestPlan* plan,
                             ChatPlanError* error) {
  *plan = {};
  *error = {};
  auto status = InputFields(
      request, {"input", "model", "dimensions", "encoding_format", "user"},
      &error->param);
  if (!status) return status;
  const auto* model = request.Find("model");
  if (!model || !model->IsString() || model->str.empty())
    return Invalid(error, "model", "Expected a served model name");
  if (!IsServedModel(model->str, config.model, config.model_aliases))
    return Invalid(error, "model", "Embedding model is not served",
                   "model_not_found", 404);
  if (config.embedding_dimensions < 1 ||
      config.embedding_dimensions > static_cast<int>(kEmbeddingDimensions) ||
      config.token_vocab < 1 || config.max_len < 1)
    return Invalid(error, "model", "Invalid native embedding configuration",
                   "embedding_configuration_invalid", 503);
  EmbeddingsRequestPlan candidate;
  candidate.model = config.model;
  candidate.hidden_size = config.embedding_dimensions;
  candidate.dimensions = candidate.hidden_size;
  if (const auto* dimensions = request.Find("dimensions")) {
    if (!IsInputInteger(*dimensions) || dimensions->number < 1 ||
        dimensions->number > config.embedding_dimensions)
      return Invalid(error, "dimensions",
                     "Dimensions must fit the served final hidden state");
    candidate.dimensions = dimensions->AsInt();
  }
  if (const auto* format = request.Find("encoding_format")) {
    if (!format->IsString() ||
        (format->str != "float" && format->str != "base64"))
      return Invalid(error, "encoding_format", "Expected float or base64");
    candidate.base64 = format->str == "base64";
  }
  if (const auto* user = request.Find("user");
      user && (!user->IsString() || user->str.size() > 4096))
    return Invalid(error, "user", "Expected an end-user string <=4096 bytes");
  const auto* input = request.Find("input");
  if (!input || (!input->IsString() && !input->IsArray()) ||
      (input->IsArray() && input->array.empty()))
    return Invalid(error, "input", "Expected nonempty text or input array");
  size_t token_count = 0;
  const auto add = [&](const Json& value, const std::string& param,
                       bool token_input) {
    EmbeddingInput item;
    item.token_input = token_input;
    Status result;
    if (token_input) {
      result = Tokens(value, config.token_vocab, param, &item.tokens, error);
    } else if (!value.IsString() || value.str.empty()) {
      return Invalid(error, param, "Expected nonempty text; types cannot mix");
    } else {
      item.text = value.str;
    }
    if (!result) return result;
    if (item.tokens.size() > kEmbeddingTotalTokens - token_count)
      return Invalid(error, "input", "Combined input exceeds 300000 tokens",
                     "context_length_exceeded");
    token_count += item.tokens.size();
    candidate.inputs.push_back(std::move(item));
    return Status{};
  };
  if (input->IsString()) {
    status = add(*input, "input", false);
  } else if (input->array[0].IsNumber()) {
    status = add(*input, "input", true);
  } else {
    if (input->array.size() > kEmbeddingInputs)
      return Invalid(error, "input", "Expected at most 2048 embedding inputs");
    const bool tokens = input->array[0].IsArray();
    for (size_t i = 0; i < input->array.size(); ++i) {
      status = add(input->array[i], "input[" + std::to_string(i) + "]", tokens);
      if (!status) break;
    }
  }
  if (!status) return status;
  *plan = std::move(candidate);
  return {};
}

Status PrepareEmbeddingInputs(const EmbeddingsRequestPlan& plan,
                              const ChatPlanConfig& config,
                              const PromptEncoder& encode,
                              std::vector<std::vector<int32_t>>* inputs,
                              uint64_t* total, ChatPlanError* error) {
  inputs->clear();
  *total = 0;
  *error = {};
  if (!encode || plan.inputs.empty() || plan.inputs.size() > kEmbeddingInputs)
    return Invalid(error, "input", "Invalid embedding preparation");
  std::vector<std::vector<int32_t>> candidate;
  uint64_t count = 0;
  for (size_t i = 0; i < plan.inputs.size(); ++i) {
    const auto& input = plan.inputs[i];
    std::vector<uint32_t> tokens = input.tokens;
    const auto param = "input[" + std::to_string(i) + "]";
    if (!input.token_input) {
      const auto status = encode(input.text, &tokens);
      if (!status) return Invalid(error, param, status.message());
    }
    if (tokens.empty() || tokens.size() > kEmbeddingInputTokens ||
        tokens.size() > static_cast<size_t>(config.max_len) ||
        (!config.capacities.empty() &&
         std::none_of(config.capacities.begin(), config.capacities.end(),
                      [&](int capacity) {
                        return capacity > 0 &&
                               tokens.size() <= static_cast<size_t>(capacity);
                      })))
      return Invalid(error, param, "Embedding input exceeds model context",
                     "context_length_exceeded");
    if (tokens.size() > kEmbeddingTotalTokens - count)
      return Invalid(error, "input", "Combined input exceeds 300000 tokens",
                     "context_length_exceeded");
    std::vector<int32_t> ids;
    ids.reserve(tokens.size());
    for (const auto token : tokens) {
      if (token >= static_cast<uint32_t>(config.token_vocab))
        return Invalid(error, param, "Token is outside the served vocab");
      ids.push_back(static_cast<int32_t>(token));
    }
    count += ids.size();
    if (config.client_limits && count > config.client_limits->max_prompt_tokens)
      return Invalid(error, "input", "Cumulative client input limit exceeded",
                     "client_prompt_limit_exceeded");
    candidate.push_back(std::move(ids));
  }
  *inputs = std::move(candidate);
  *total = count;
  return {};
}

Status NormalizeEmbeddingHidden(std::span<const uint16_t> hidden,
                                size_t dimensions, std::vector<float>* out) {
  out->clear();
  if (!dimensions || dimensions > hidden.size() ||
      hidden.size() > kEmbeddingDimensions)
    return Status::Fail("Invalid final hidden dimensions");
  std::vector<float> result;
  result.reserve(dimensions);
  double squared = 0;
  for (size_t i = 0; i < hidden.size(); ++i) {
    const float value =
        std::bit_cast<float>(static_cast<uint32_t>(hidden[i]) << 16);
    if (!std::isfinite(value))
      return Status::Fail("Final hidden state is not finite");
    if (i < dimensions) {
      result.push_back(value);
      squared += static_cast<double>(value) * value;
    }
  }
  if (!std::isfinite(squared) || squared <= 0)
    return Status::Fail("Final hidden prefix has zero or invalid norm");
  const double norm = std::sqrt(squared);
  for (auto& value : result) value = static_cast<float>(value / norm);
  *out = std::move(result);
  return {};
}

Status BuildEmbeddingsResponse(const EmbeddingsRequestPlan& plan,
                               std::span<const std::vector<float>> vectors,
                               uint64_t prompt_tokens, std::string* response) {
  response->clear();
  if (plan.model.empty() || vectors.empty() ||
      vectors.size() != plan.inputs.size() ||
      vectors.size() > kEmbeddingInputs || !plan.dimensions ||
      plan.dimensions > plan.hidden_size ||
      plan.hidden_size > kEmbeddingDimensions || !prompt_tokens ||
      prompt_tokens > kEmbeddingTotalTokens)
    return Status::Fail("Incomplete embedding response");
  std::string result =
      "{\"object\":\"list\",\"model\":" + OutputJson(String(plan.model)) +
      ",\"data\":[";
  for (size_t i = 0; i < vectors.size(); ++i) {
    const auto& vector = vectors[i];
    if (vector.size() != plan.dimensions)
      return Status::Fail("Incomplete embedding vector");
    for (const auto value : vector)
      if (!std::isfinite(value))
        return Status::Fail("Non-finite embedding vector");
    if (i) result += ',';
    result += "{\"object\":\"embedding\",\"index\":" + std::to_string(i) +
              ",\"embedding\":";
    if (plan.base64) {
      result += '"' + Base64(vector) + '"';
    } else {
      result += '[';
      for (size_t j = 0; j < vector.size(); ++j) {
        if (j) result += ',';
        // Integer-oriented JSON readers discard the sign of bare -0.
        if (vector[j] == 0.0f && std::signbit(vector[j])) {
          result += "-0.0";
          continue;
        }
        char buffer[32];
        const auto [end, error] = std::to_chars(buffer, buffer + 32, vector[j]);
        if (error != std::errc{})
          return Status::Fail("Embedding float encoding failed");
        result.append(buffer, end);
      }
      result += ']';
    }
    result += '}';
    if (result.size() > kChatBodyBytes - 256)
      return Status::Fail("Embedding response exceeds finite capacity");
  }
  result += "],\"usage\":{\"prompt_tokens\":" + std::to_string(prompt_tokens) +
            ",\"total_tokens\":" + std::to_string(prompt_tokens) + "}}";
  *response = std::move(result);
  return {};
}
}  // namespace q4t::server
