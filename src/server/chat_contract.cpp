#include "q4t/server/chat_contract.h"

#include <cmath>
#include <initializer_list>
#include <string_view>

namespace q4t::server {
namespace {

bool Known(std::string_view name,
           std::initializer_list<std::string_view> keys) {
  for (const auto key : keys)
    if (key == name) return true;
  return false;
}

Status Fields(const io::Json& object,
              std::initializer_list<std::string_view> keys) {
  if (!object.IsObject()) return Status::Fail("expected JSON object");
  for (const auto& [key, value] : object.object) {
    (void)value;
    if (!Known(key, keys)) return Status::Fail("unsupported field: " + key);
  }
  return Status();
}

bool Number(const io::Json& value, double expected) {
  return value.IsNumber() && value.number == expected;
}

Status TemplateOption(const std::string& key, const io::Json& value) {
  if (key == "reasoning_effort") {
    if (!value.IsString() || !Known(value.str, {"low", "medium", "xhigh"}))
      return Status::Fail("reasoning_effort must be low, medium or xhigh");
  } else if (!value.IsBool()) {
    return Status::Fail(key + " must be boolean");
  }
  return Status();
}

Status TemplateOptions(const io::Json& options) {
  auto status = Fields(options, {"enable_thinking", "preserve_thinking",
                                 "reasoning_effort", "add_vision_id"});
  if (!status) return status;
  for (const auto& [key, value] : options.object) {
    status = TemplateOption(key, value);
    if (!status) return status;
  }
  return Status();
}

}  // namespace

Status ValidateChatContract(const io::Json& req,
                            const std::string& model_name, bool allow_media) {
  auto status = Fields(req, {
      "model", "prompt", "messages", "max_tokens", "stream", "stream_options",
      "request_id", "cancel_token", "request_timeout_ms", "tools",
      "chat_template_kwargs", "enable_thinking", "preserve_thinking",
      "reasoning_effort", "add_vision_id", "temperature", "top_p", "n",
      "frequency_penalty", "presence_penalty", "repetition_penalty", "seed",
      "stop", "logprobs", "top_logprobs", "logit_bias", "response_format",
      "user"});
  if (!status) return status;
  if (const auto* value = req.Find("stream"); value && !value->IsBool())
    return Status::Fail("stream must be boolean");
  if (const auto* value = req.Find("max_tokens")) {
    const double n = value->AsDouble(-1);
    if (!value->IsNumber() || !std::isfinite(n) || n < 1 ||
        n > 2147483647.0 || std::trunc(n) != n)
      return Status::Fail("max_tokens must be an integer in [1,2147483647]");
  }
  const auto* prompt = req.Find("prompt");
  const auto* messages = req.Find("messages");
  if ((prompt != nullptr) == (messages != nullptr))
    return Status::Fail("provide exactly one of prompt or messages");
  if (prompt && (!prompt->IsString() || prompt->str.empty()))
    return Status::Fail("prompt must be a nonempty string");
  if (const auto* model = req.Find("model");
      model && (!model->IsString() || model->str != model_name))
    return Status::Fail("model must match the served model");
  if (const auto* user = req.Find("user"); user && !user->IsString())
    return Status::Fail("user must be a string (metadata only)");
  for (const auto& [key, neutral] : {
           std::pair{"temperature", 0.0}, {"top_p", 1.0}, {"n", 1.0},
           {"frequency_penalty", 0.0}, {"presence_penalty", 0.0},
           {"repetition_penalty", 1.0}}) {
    if (const auto* value = req.Find(key); value && !Number(*value, neutral))
      return Status::Fail(std::string(key) +
                          " unsupported value in greedy mode");
  }
  if (const auto* seed = req.Find("seed"); seed && !seed->IsNull()) {
    if (!seed->IsNumber() || !std::isfinite(seed->number) ||
        std::abs(seed->number) > 9007199254740991.0 ||
        std::trunc(seed->number) != seed->number)
      return Status::Fail("seed must be an exact binary64 integer or null");
  }
  for (const char* key : {"logprobs", "top_logprobs", "stop", "logit_bias",
                          "response_format"}) {
    const auto* value = req.Find(key);
    if (!value || value->IsNull()) continue;
    const std::string_view name(key);
    const bool neutral =
        (name == "logprobs" && value->IsBool() && !value->boolean) ||
        (name == "top_logprobs" && Number(*value, 0)) ||
        (name == "stop" && value->IsArray() && value->array.empty()) ||
        (name == "logit_bias" && value->IsObject() && value->object.empty()) ||
        (name == "response_format" && value->IsObject() &&
         value->object.size() == 1 && value->GetString("type") == "text");
    if (!neutral) return Status::Fail(std::string(key) + " is not supported");
  }
  if (const auto* options = req.Find("stream_options")) {
    status = Fields(*options, {"include_usage"});
    if (!status) return status;
    if (const auto* usage = options->Find("include_usage");
        usage && !usage->IsBool())
      return Status::Fail("include_usage must be boolean");
  }
  for (const char* key : {"enable_thinking", "preserve_thinking",
                          "reasoning_effort", "add_vision_id"}) {
    if (const auto* value = req.Find(key)) {
      if (!messages) return Status::Fail("template options require messages");
      status = TemplateOption(key, *value);
      if (!status) return status;
    }
  }
  if (const auto* options = req.Find("chat_template_kwargs")) {
    if (!messages) return Status::Fail("template options require messages");
    status = TemplateOptions(*options);
    if (!status) return status;
  }
  if (const auto* tools = req.Find("tools")) {
    if (!messages || (!tools->IsNull() && !tools->IsArray()))
      return Status::Fail("tools requires messages and an array or null");
  }
  if (!messages) return Status();
  if (!messages->IsArray() || messages->array.empty())
    return Status::Fail("messages must be a nonempty array");
  for (const auto& message : messages->array) {
    if (!message.IsObject()) return Status::Fail("message must be an object");
    const auto* content = message.Find("content");
    if (!content || content->IsNull() || content->IsString()) continue;
    if (!content->IsArray()) return Status::Fail("invalid message content");
    for (const auto& part : content->array) {
      if (!part.IsObject())
        return Status::Fail("content part must be an object");
      const std::string type = part.GetString("type");
      if (part.Has("image_url") || part.Has("video_frames") ||
          type == "image_url" || type == "video_frames" ||
          type == "video_url") {
        if (!allow_media)
          return Status::Fail(
              "media disabled; experimental --allow-media required");
      }
    }
  }
  return Status();
}

}  // namespace q4t::server
