#include "q4t/server/chat_contract.h"

#include <cmath>
#include <initializer_list>
#include <string_view>

#include "q4t/server/chat_tool_history.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/output_constraints.h"

namespace q4t::server {
namespace {

bool Known(std::string_view name,
           std::initializer_list<std::string_view> keys) {
  for (const auto key : keys)
    if (key == name) return true;
  return false;
}

Status Fields(const io::Json& object,
              std::initializer_list<std::string_view> keys,
              std::string* error_param = nullptr) {
  if (!object.IsObject()) {
    if (error_param) *error_param = "body";
    return Status::Fail("expected JSON object");
  }
  for (const auto& [key, value] : object.object) {
    (void)value;
    if (!Known(key, keys)) {
      if (error_param) *error_param = key;
      return Status::Fail("unsupported field: " + key);
    }
  }
  return Status();
}

bool Number(const io::Json& value, double expected) {
  return IsInputInteger(value) && value.number == expected;
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

Status ValidateChatContract(const io::Json& req, const std::string& model_name,
                            bool allow_media, std::string* error_param) {
  std::string local_param;
  if (!error_param) error_param = &local_param;
  error_param->clear();
  const auto invalid = [&](std::string_view param, std::string message) {
    *error_param = param;
    return Status::Fail(std::move(message));
  };
  auto status = Fields(req,
                       {"model",
                        "prompt",
                        "messages",
                        "max_tokens",
                        "max_completion_tokens",
                        "stream",
                        "stream_options",
                        "request_id",
                        "cancel_token",
                        "request_timeout_ms",
                        "tools",
                        "chat_template_kwargs",
                        "enable_thinking",
                        "preserve_thinking",
                        "reasoning_effort",
                        "add_vision_id",
                        "temperature",
                        "top_p",
                        "n",
                        "frequency_penalty",
                        "presence_penalty",
                        "repetition_penalty",
                        "seed",
                        "stop",
                        "logprobs",
                        "top_logprobs",
                        "logit_bias",
                        "response_format",
                        "user",
                        "session_id",
                        "prefix_cache_scope",
                        "prefix_cache_write_tokens",
                        "parse_response",
                        "tool_choice",
                        "parallel_tool_calls"},
                       error_param);
  if (!status) return status;
  status = ValidateRequestCredentials(req);
  if (!status) {
    const auto* id = req.Find("request_id");
    *error_param = !id || !id->IsString() || !ValidRequestId(id->str)
                       ? "request_id"
                       : "cancel_token";
    return status;
  }
  if (const auto* value = req.Find("request_timeout_ms"); value &&
      (!IsInputInteger(*value) || value->number < 1 ||
       value->number > 10800000))
    return invalid("request_timeout_ms",
                   "request_timeout_ms must be an integer in [1,10800000]");
  for (const auto* field : {"session_id", "prefix_cache_scope"}) {
    const auto* value = req.Find(field);
    if (!value) continue;
    if (!value->IsString() || value->str.size() != 64)
      return invalid(field,
                     std::string(field) + " must be a 64-hex capability");
    for (const char c : value->str)
      if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
        return invalid(field, std::string(field) + " must be lowercase hex");
  }
  if (const auto* value = req.Find("prefix_cache_write_tokens")) {
    const double n = value->AsDouble(-1);
    if (!IsInputInteger(*value) ||
        (n != 0 && (n < 1024 || n > 131072)))
      return invalid(
          "prefix_cache_write_tokens",
          "prefix_cache_write_tokens must be 0 or an integer in [1024,131072]");
    if (n > 0 && !req.Has("prefix_cache_scope"))
      return invalid("prefix_cache_scope",
                     "prefix_cache_write_tokens requires prefix_cache_scope");
  }
  if (const auto* value = req.Find("stream");
      value && !value->IsNull() && !value->IsBool())
    return invalid("stream", "stream must be boolean or null");
  const auto* legacy_limit = req.Find("max_tokens");
  const auto* limit = req.Find("max_completion_tokens");
  if (legacy_limit && !legacy_limit->IsNull() && limit && !limit->IsNull())
    return invalid("max_completion_tokens",
                   "provide only one of max_tokens or "
                   "max_completion_tokens");
  for (const char* key : {"max_tokens", "max_completion_tokens"}) {
    const auto* value = req.Find(key);
    if (!value || value->IsNull()) continue;
    const double n = value->AsDouble(-1);
    if (!IsInputInteger(*value) || n < 1 || n > 2147483647.0)
      return invalid(key, std::string(key) +
                              " must be an integer in [1,2147483647] or null");
  }
  const auto* prompt = req.Find("prompt");
  const auto* messages = req.Find("messages");
  if ((prompt != nullptr) == (messages != nullptr))
    return invalid("messages", "provide exactly one of prompt or messages");
  if (const auto* parsed = req.Find("parse_response"); parsed &&
      (!parsed->IsBool() || (parsed->boolean && !messages)))
    return invalid("parse_response", "parse_response=true requires messages");
  OutputConstraints constraints;
  status = PlanOutputConstraints(req, &constraints);
  if (!status) return status;
  if (prompt && (!prompt->IsString() || prompt->str.empty()))
    return invalid("prompt", "prompt must be a nonempty string");
  if (const auto* model = req.Find("model");
      model && (!model->IsString() || model->str != model_name))
    return invalid("model", "model must match the served model");
  if (const auto* user = req.Find("user");
      user && !user->IsNull() && !user->IsString())
    return invalid("user", "user must be a string or null (metadata only)");
  for (const auto& [key, neutral] : {
           std::pair{"temperature", 0.0}, {"top_p", 1.0}, {"n", 1.0},
           {"frequency_penalty", 0.0}, {"presence_penalty", 0.0},
           {"repetition_penalty", 1.0}}) {
    if (const auto* value = req.Find(key); value &&
        !(value->IsNull() && std::string_view(key) != "repetition_penalty") &&
        !Number(*value, neutral))
      return invalid(key,
                     std::string(key) + " unsupported value in greedy mode");
  }
  if (const auto* seed = req.Find("seed"); seed && !seed->IsNull()) {
    if (!IsInputInteger(*seed) ||
        std::abs(seed->number) > 9007199254740991.0)
      return invalid("seed", "seed must be an exact binary64 integer or null");
  }
  for (const char* key : {"logprobs", "top_logprobs", "stop", "logit_bias"}) {
    const auto* value = req.Find(key);
    if (!value || value->IsNull()) continue;
    const std::string_view name(key);
    const bool neutral =
        (name == "logprobs" && value->IsBool() && !value->boolean) ||
        (name == "top_logprobs" && Number(*value, 0)) ||
        (name == "stop" && value->IsArray() && value->array.empty()) ||
        (name == "logit_bias" && value->IsObject() && value->object.empty());
    if (!neutral) return invalid(key, std::string(key) + " is not supported");
  }
  if (const auto* options = req.Find("stream_options");
      options && !options->IsNull()) {
    status = Fields(*options, {"include_usage"});
    if (!status) return invalid("stream_options", status.message());
    if (const auto* usage = options->Find("include_usage");
        usage && !usage->IsBool())
      return invalid("stream_options.include_usage",
                     "include_usage must be boolean");
  }
  for (const char* key : {"enable_thinking", "preserve_thinking",
                          "reasoning_effort", "add_vision_id"}) {
    if (const auto* value = req.Find(key)) {
      if (!messages) return invalid(key, "template options require messages");
      status = TemplateOption(key, *value);
      if (!status) return invalid(key, status.message());
    }
  }
  if (const auto* options = req.Find("chat_template_kwargs")) {
    if (!messages)
      return invalid("chat_template_kwargs",
                     "template options require messages");
    status = TemplateOptions(*options);
    if (!status) return invalid("chat_template_kwargs", status.message());
  }
  if (const auto* tools = req.Find("tools")) {
    if (!messages || (!tools->IsNull() && !tools->IsArray()))
      return invalid("tools", "tools requires messages and an array or null");
  }
  if (!messages) return Status();
  if (!messages->IsArray() || messages->array.empty())
    return invalid("messages", "messages must be a nonempty array");
  status = ValidateChatMessageProfile(*messages, error_param);
  if (!status) return status;
  for (size_t index = 0; index < messages->array.size(); ++index) {
    const auto& message = messages->array[index];
    *error_param = "messages[" + std::to_string(index) + "]";
    if (!message.IsObject()) return Status::Fail("message must be an object");
    status = Fields(message, {"role", "content", "reasoning_content",
                              "tool_calls", "tool_call_id", "name", "refusal",
                              "audio", "annotations", "function_call"});
    if (!status) return status;
    for (const char* key : {"refusal", "audio", "annotations",
                            "function_call"}) {
      const auto* value = message.Find(key);
      if (value && !value->IsNull())
        return Status::Fail(std::string("message field not supported: ") + key);
    }
    const auto role = message.GetString("role");
    if (const auto* reasoning = message.Find("reasoning_content"); reasoning &&
        (!reasoning->IsString() || role != "assistant"))
      return Status::Fail("reasoning_content requires an assistant string");
    if (message.Has("tool_calls") && role != "assistant")
      return Status::Fail("tool_calls requires an assistant message");
    const auto* content = message.Find("content");
    if (!content || content->IsNull() || content->IsString()) continue;
    if (!content->IsArray()) return Status::Fail("invalid message content");
    for (const auto& part : content->array) {
      if (!part.IsObject())
        return Status::Fail("content part must be an object");
      const std::string type = part.GetString("type");
      if (part.Has("text") && !part.Has("image_url") &&
          !part.Has("video_frames")) {
        status = Fields(part, {"type", "text"});
        if (!status) return status;
        const auto* kind = part.Find("type");
        if ((kind && (!kind->IsString() || kind->str != "text")) ||
            !part.Find("text")->IsString())
          return Status::Fail("text content part requires type=text and text");
      }
      if (part.Has("image_url") || part.Has("video_frames") ||
          type == "image_url" || type == "video_frames" ||
          type == "video_url") {
        if (constraints.active)
          return Status::Fail("constrained output requires text-only messages");
        if (!allow_media)
          return Status::Fail(
              "media disabled; experimental --allow-media required");
      }
    }
  }
  *error_param = "messages";
  status = PlanToolResultOrder(*messages);
  if (status) error_param->clear();
  return status;
}

}  // namespace q4t::server
