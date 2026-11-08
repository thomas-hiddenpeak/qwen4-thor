#include "q4t/server/prediction.h"

#include <algorithm>

#include "q4t/server/input_contract.h"

namespace q4t::server {

Status ParsePredictionOptions(const io::Json& request,
                              const SamplingOptions& sampling,
                              PredictionOptions* options,
                              std::string* error_param) {
  *options = {};
  if (error_param) error_param->clear();
  const auto fail = [&](std::string path, std::string message) {
    if (error_param) *error_param = std::move(path);
    return Status::Fail(std::move(message));
  };
  const auto* value = request.Find("prediction");
  if (!value || value->IsNull()) return Status();
  if (!value->IsObject())
    return fail("prediction", "prediction must be an object or null");
  for (const auto& [key, _] : value->object)
    if (key != "type" && key != "content")
      return fail(InputParamPath("prediction", key),
                  "unknown prediction field");
  if (value->GetString("type") != "content")
    return fail("prediction.type", "prediction type must be content");
  const auto* content = value->Find("content");
  if (!content)
    return fail("prediction.content", "prediction content is required");
  if (content->IsString()) {
    options->content = content->str;
  } else if (content->IsArray() && !content->array.empty()) {
    for (size_t i = 0; i < content->array.size(); ++i) {
      const auto& part = content->array[i];
      const std::string path = "prediction.content[" + std::to_string(i) + ']';
      if (!part.IsObject()) return fail(path, "prediction part must be object");
      for (const auto& [key, _] : part.object)
        if (key != "type" && key != "text")
          return fail(InputParamPath(path, key),
                      "unknown prediction text field");
      if (part.GetString("type") != "text")
        return fail(path + ".type", "prediction part must have type text");
      const auto* text = part.Find("text");
      if (!text || !text->IsString())
        return fail(path + ".text", "prediction text must be a string");
      options->content += text->str;
    }
  } else {
    return fail("prediction.content", "prediction needs string or text parts");
  }
  // Conditions from the pinned official Predicted Outputs contract. They do
  // not restrict these options for requests without a prediction.
  if (sampling.choices != 1) return fail("n", "prediction requires n=1");
  if (sampling.logprobs)
    return fail("logprobs", "prediction does not support logprobs");
  if (sampling.frequency_penalty > 0)
    return fail("frequency_penalty", "prediction cannot use positive penalty");
  if (sampling.presence_penalty > 0)
    return fail("presence_penalty", "prediction cannot use positive penalty");
  if (const auto* tools = request.Find("tools");
      tools && tools->IsArray() && !tools->array.empty())
    return fail("tools", "prediction does not support tools");
  if (const auto* limit = request.Find("max_completion_tokens");
      limit && !limit->IsNull())
    return fail("max_completion_tokens", "prediction uses max_tokens");
  options->enabled = true;
  return Status();
}

PredictionCursor::PredictionCursor(std::span<const int32_t> tokens)
    : tokens_(tokens.begin(), tokens.end()) {
  for (size_t i = 0; i < tokens_.size(); ++i)
    positions_[tokens_[i]].push_back(i);
}

size_t PredictionCursor::Match(int32_t token) const {
  const auto found = positions_.find(token);
  if (found == positions_.end()) return tokens_.size();
  const auto it =
      std::lower_bound(found->second.begin(), found->second.end(), cursor_);
  return it == found->second.end() ? tokens_.size() : *it;
}

bool PredictionCursor::Matches(int32_t token) const {
  return Match(token) < tokens_.size();
}

void PredictionCursor::Consume(int32_t token, bool content) {
  if (!content) return;
  const auto found = Match(token);
  if (found == tokens_.size()) return;
  cursor_ = found + 1;
  ++accepted_;
}

int32_t PredictionCursor::Next() const {
  return cursor_ < tokens_.size() ? tokens_[cursor_] : -1;
}

}  // namespace q4t::server
