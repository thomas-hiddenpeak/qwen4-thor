#include "q4t/server/responses_plan.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/model_catalog.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message) {
  *error = {"invalid_request", std::move(path)};
  return Status::Fail(std::move(message));
}
size_t Characters(std::string_view value) {
  return std::count_if(value.begin(), value.end(), [](unsigned char byte) {
    return (byte & 0xc0) != 0x80;
  });
}
bool OneOf(std::string_view value,
           std::initializer_list<std::string_view> choices) {
  return std::find(choices.begin(), choices.end(), value) != choices.end();
}
Status StringOption(const Json& object, const char* key, Json* options,
                    std::string_view parent, ChatPlanError* error,
                    size_t maximum = 4096) {
  const auto* value = object.Find(key);
  if (!value || value->IsNull()) return {};
  if (!value->IsString() || Characters(value->str) > maximum)
    return Invalid(error, InputParamPath(parent, key), "invalid string option");
  Set(options, key, *value);
  return {};
}
Status ChoiceOption(const Json& object, const char* key,
                    std::initializer_list<std::string_view> choices,
                    std::string* value, std::string_view parent,
                    ChatPlanError* error) {
  const auto* field = object.Find(key);
  if (!field || field->IsNull()) return {};
  if (!field->IsString() || !OneOf(field->str, choices))
    return Invalid(error, InputParamPath(parent, key), "invalid option value");
  *value = field->str;
  return {};
}
Status IntegerOption(const Json& request, const char* key, int64_t low,
                     int64_t high, Json* options, ChatPlanError* error) {
  const auto* value = request.Find(key);
  if (!value || value->IsNull()) return {};
  if (!IsInputInteger(*value) || value->number < low || value->number > high)
    return Invalid(error, key, "integer option is outside the accepted range");
  Set(options, key, *value);
  return {};
}
Status Metadata(const Json& request, Json* options, ChatPlanError* error) {
  Set(options, "metadata", Object());
  const auto* value = request.Find("metadata");
  if (!value || value->IsNull()) return {};
  if (!value->IsObject() || value->object.size() > 16)
    return Invalid(error, "metadata",
                   "metadata must contain at most 16 entries");
  for (const auto& [key, entry] : value->object)
    if (Characters(key) > 64 || !entry.IsString() ||
        Characters(entry.str) > 512)
      return Invalid(error, InputParamPath("metadata", key),
                     "metadata keys/values require at most 64/512 characters");
  Set(options, "metadata", *value);
  return {};
}

Status Reasoning(const Json& request, ResponsesEnvelope* result,
                 ChatPlanError* error) {
  if (const auto* object = request.Find("reasoning");
      object && !object->IsNull()) {
    auto status = InputFields(
        *object, {"effort", "summary", "generate_summary", "context", "mode"},
        &error->param, "reasoning");
    if (!status) return status;
    status = ChoiceOption(
        *object, "effort",
        {"none", "minimal", "low", "medium", "high", "xhigh", "max"},
        &result->reasoning_effort, "reasoning", error);
    if (status)
      status = ChoiceOption(*object, "context",
                            {"auto", "current_turn", "all_turns"},
                            &result->reasoning_context, "reasoning", error);
    if (status)
      status = ChoiceOption(*object, "summary", {"auto", "concise", "detailed"},
                            &result->summary, "reasoning", error);
    std::string legacy;
    if (status)
      status = ChoiceOption(*object, "generate_summary",
                            {"auto", "concise", "detailed"}, &legacy,
                            "reasoning", error);
    if (!status) return status;
    if (!legacy.empty() && !result->summary.empty() &&
        legacy != result->summary)
      return Invalid(error, "reasoning.summary", "conflicting summary options");
    if (result->summary.empty()) result->summary = legacy;
    if (Nonnull(*object, "mode")) {
      const auto* mode = object->Find("mode");
      if (!mode->IsString() || !OneOf(mode->str, {"standard", "pro"}))
        return Invalid(error, "reasoning.mode", "invalid reasoning mode");
      result->reasoning_mode = mode->str;
    }
  }
  // This is Qwen's effective history policy; auto is not an opaque promise.
  if (result->reasoning_context == "auto")
    result->reasoning_context = "all_turns";
  Set(&result->options, "reasoning",
      Object({{"effort", String(result->reasoning_effort)},
              {"context", String(result->reasoning_context)},
              {"mode", String(result->reasoning_mode)},
              {"summary",
               result->summary.empty() ? Json() : String(result->summary)}}));
  return {};
}

Status TextOptions(const Json& request, ResponsesEnvelope* result,
                   ChatPlanError* error) {
  auto format = Object({{"type", String("text")}});
  if (const auto* object = request.Find("text")) {
    auto status =
        InputFields(*object, {"format", "verbosity"}, &error->param, "text");
    if (!status) return status;
    status = ChoiceOption(*object, "verbosity", {"low", "medium", "high"},
                          &result->verbosity, "text", error);
    if (!status) return status;
    if (const auto* value = object->Find("format")) {
      const auto type = value->GetString("type");
      status =
          type == "json_schema"
              ? InputFields(*value,
                            {"type", "name", "schema", "description", "strict"},
                            &error->param, "text.format")
              : InputFields(*value, {"type"}, &error->param, "text.format");
      if (!status) return status;
      if (!OneOf(type, {"text", "json_object", "json_schema"}))
        return Invalid(error, "text.format.type", "invalid response format");
      if (type == "json_schema") {
        if (!ValidInputName(value->GetString("name")))
          return Invalid(error, "text.format.name", "invalid schema name");
        const auto* schema = value->Find("schema");
        if (!schema || !schema->IsObject())
          return Invalid(error, "text.format.schema",
                         "schema must be an object");
        if (const auto* strict = value->Find("strict");
            strict && !strict->IsNull() && !strict->IsBool())
          return Invalid(error, "text.format.strict",
                         "strict must be boolean or null");
        if (const auto* description = value->Find("description");
            description && !description->IsString())
          return Invalid(error, "text.format.description",
                         "description must be text");
      }
      format = *value;
    }
  }
  Set(&result->options, "text",
      Object({{"format", std::move(format)},
              {"verbosity", String(result->verbosity)}}));
  return {};
}

Status StateOptions(const Json& request, ResponsesEnvelope* result,
                    ChatPlanError* error) {
  for (const auto* key : {"previous_response_id", "prompt_cache_key",
                          "safety_identifier", "user"}) {
    auto status = StringOption(request, key, &result->options, "", error);
    if (!status) return status;
  }
  result->previous_response_id =
      result->options.GetString("previous_response_id");
  if (Nonnull(request, "previous_response_id") &&
      result->previous_response_id.empty())
    return Invalid(error, "previous_response_id", "empty response reference");
  if (const auto* conversation = request.Find("conversation");
      conversation && !conversation->IsNull()) {
    if (conversation->IsString()) {
      result->conversation_id = conversation->str;
    } else {
      auto status =
          InputFields(*conversation, {"id"}, &error->param, "conversation");
      if (!status) return status;
      result->conversation_id = conversation->GetString("id");
    }
    if (result->conversation_id.empty() || result->conversation_id.size() > 256)
      return Invalid(error, "conversation", "invalid conversation reference");
    Set(&result->options, "conversation",
        Object({{"id", String(result->conversation_id)}}));
  }
  if (!result->previous_response_id.empty() && !result->conversation_id.empty())
    return Invalid(error, "previous_response_id",
                   "conversation and previous_response_id are exclusive");
  if (const auto* prompt = request.Find("prompt");
      prompt && !prompt->IsNull()) {
    auto status = InputFields(*prompt, {"id", "version", "variables"},
                              &error->param, "prompt");
    if (!status) return status;
    if (prompt->GetString("id").empty())
      return Invalid(error, "prompt.id", "prompt requires an identifier");
    if (Nonnull(*prompt, "version") && !prompt->Find("version")->IsString())
      return Invalid(error, "prompt.version", "prompt version must be text");
    if (Nonnull(*prompt, "variables") && !prompt->Find("variables")->IsObject())
      return Invalid(error, "prompt.variables",
                     "prompt variables must be an object");
    Set(&result->options, "prompt", *prompt);
  }
  if (const auto* context = request.Find("context_management");
      context && !context->IsNull()) {
    if (!context->IsArray() || context->array.size() > 1)
      return Invalid(error, "context_management",
                     "expected at most one compaction policy");
    for (const auto& policy : context->array) {
      auto status = InputFields(policy, {"type", "compact_threshold"},
                                &error->param, "context_management[0]");
      if (!status) return status;
      if (policy.GetString("type") != "compaction")
        return Invalid(error, "context_management[0].type",
                       "invalid context policy");
      const auto* threshold = policy.Find("compact_threshold");
      if (threshold && !threshold->IsNull() &&
          (!IsInputInteger(*threshold) || threshold->number < 1 ||
           threshold->number > INT32_MAX))
        return Invalid(error, "context_management[0].compact_threshold",
                       "invalid token threshold");
    }
  }
  return {};
}
}  // namespace

Status NormalizeResponsesCompactRequest(const io::Json& request,
                                        const ChatPlanConfig& config,
                                        io::Json* normalized,
                                        ChatPlanError* error) {
  *error = {};
  auto status =
      InputFields(request,
                  {"model", "input", "instructions", "previous_response_id",
                   "prompt_cache_key", "prompt_cache_options",
                   "prompt_cache_retention", "service_tier"},
                  &error->param, "");
  if (!status) return status;
  if (!request.Has("model"))
    return Invalid(error, "model", "compact requires a model field");
  auto result = request;
  if (request.Find("model")->IsNull())
    Set(&result, "model", String(config.model));
  Set(&result, "store", Boolean(false));
  Set(&result, "reasoning", Object({{"effort", String("none")}}));
  Set(&result, "temperature", Number(0));
  *normalized = std::move(result);
  return {};
}

Status ParseResponsesEnvelope(const io::Json& request,
                              const ChatPlanConfig& config,
                              ResponsesEnvelope* envelope,
                              ChatPlanError* error) {
  *envelope = {};
  *error = {};
  auto status = InputFields(request,
                            {"access_programs",
                             "background",
                             "context_management",
                             "conversation",
                             "include",
                             "input",
                             "instructions",
                             "max_output_tokens",
                             "max_tool_calls",
                             "metadata",
                             "model",
                             "moderation",
                             "parallel_tool_calls",
                             "previous_response_id",
                             "prompt",
                             "prompt_cache_key",
                             "prompt_cache_options",
                             "prompt_cache_retention",
                             "reasoning",
                             "safety_identifier",
                             "service_tier",
                             "store",
                             "stream",
                             "stream_options",
                             "temperature",
                             "text",
                             "tool_choice",
                             "tools",
                             "top_logprobs",
                             "top_p",
                             "truncation",
                             "user",
                             "request_id",
                             "cancel_token",
                             "request_timeout_ms",
                             "seed"},
                            &error->param, "");
  if (!status) return status;
  ResponsesEnvelope result;
  result.request = request;
  result.options = Object({{"instructions", {}},
                           {"previous_response_id", {}},
                           {"max_tool_calls", {}},
                           {"access_programs", {}},
                           {"top_logprobs", Number(0)}});
  std::string model = config.model;
  if (const auto* value = request.Find("model")) {
    if (!value->IsString() || !ValidModelIdentifier(value->str))
      return Invalid(error, "model", "invalid served model identifier");
    model = value->str;
  }
  if (!IsServedModel(model, config.model, config.model_aliases)) {
    *error = {"model_not_found", "model", 404};
    return Status::Fail("model is not registered by this server");
  }
  Set(&result.options, "model", String(model));
  for (const auto& [key, fallback] : {std::pair{"store", true},
                                      {"background", false},
                                      {"stream", false},
                                      {"parallel_tool_calls", true}}) {
    const auto* value = request.Find(key);
    if (value && !value->IsNull() && !value->IsBool())
      return Invalid(error, key, "expected boolean or null");
    Set(&result.options, key,
        Boolean(value && value->IsBool() ? value->boolean : fallback));
  }
  result.stream = result.options.GetBool("stream");
  result.store = result.options.GetBool("store");
  result.background = result.options.GetBool("background");
  status = StringOption(request, "instructions", &result.options, "", error,
                        16 * 1024 * 1024);
  if (status) status = Metadata(request, &result.options, error);
  if (!status) return status;
  for (const auto* key : {"temperature", "top_p"}) {
    const auto* value = request.Find(key);
    const bool temperature = std::string_view(key) == "temperature";
    if (value && !value->IsNull() &&
        (!value->IsNumber() || !std::isfinite(value->number) ||
         value->number < 0 || value->number > (temperature ? 2 : 1)))
      return Invalid(error, key, "invalid sampling option");
    Set(&result.options, key, value && value->IsNumber() ? *value : Number(1));
  }
  Set(&result.options, "max_output_tokens",
      Number(config.client_limits
                 ? std::min<int64_t>(config.default_max_tokens,
                                     config.client_limits->max_output_tokens)
                 : config.default_max_tokens));
  status = IntegerOption(request, "max_output_tokens", 1, INT32_MAX,
                         &result.options, error);
  if (status)
    status = IntegerOption(request, "max_tool_calls", 1, INT32_MAX,
                           &result.options, error);
  if (status)
    status =
        IntegerOption(request, "top_logprobs", 0, 20, &result.options, error);
  if (status) status = StateOptions(request, &result, error);
  if (status)
    status = ParsePromptCacheOptions(request, &result.cache, &error->param);
  if (status) status = Reasoning(request, &result, error);
  if (status) status = TextOptions(request, &result, error);
  if (!status) return status;
  if (config.client_limits && result.options.GetInt("max_output_tokens") >
                                  config.client_limits->max_output_tokens) {
    *error = {"client_output_limit_exceeded", "max_output_tokens", 400};
    return Status::Fail("requested output exceeds client token limit");
  }
  std::string truncation = "disabled", tier = "default";
  status = ChoiceOption(request, "truncation", {"auto", "disabled"},
                        &truncation, "", error);
  if (status)
    status = ChoiceOption(request, "service_tier",
                          {"auto", "default", "flex", "priority", "scale"},
                          &tier, "", error);
  if (!status) return status;
  result.truncate = truncation == "auto";
  Set(&result.options, "truncation", String(truncation));
  Set(&result.options, "service_tier",
      String(tier == "auto" ? "default" : tier));
  Set(&result.options, "prompt_cache_retention", String(result.cache.retention));
  Set(&result.options, "prompt_cache_options",
      PublicPromptCacheOptions(result.cache));
  if (const auto* include = request.Find("include");
      include && !include->IsNull()) {
    if (!include->IsArray() || include->array.size() > 8)
      return Invalid(error, "include", "invalid include list");
    std::unordered_set<std::string> seen;
    for (size_t i = 0; i < include->array.size(); ++i) {
      const auto& item = include->array[i];
      if (!item.IsString() ||
          !OneOf(
              item.str,
              {"file_search_call.results", "web_search_call.results",
               "web_search_call.action.sources",
               "message.input_image.image_url",
               "computer_call_output.output.image_url",
               "code_interpreter_call.outputs", "reasoning.encrypted_content",
               "message.output_text.logprobs"}) ||
          !seen.insert(item.str).second)
        return Invalid(error, "include[" + std::to_string(i) + ']',
                       "invalid or duplicate include option");
      result.logprobs |= item.str == "message.output_text.logprobs";
      result.encrypted_reasoning |= item.str == "reasoning.encrypted_content";
    }
  }
  result.logprobs |= result.options.GetInt("top_logprobs") > 0;
  auto sampling_request = Object();
  for (const auto* key : {"temperature", "top_p", "top_logprobs"})
    if (std::string_view(key) != "top_logprobs" || result.logprobs)
      Set(&sampling_request, key, *result.options.Find(key));
  Set(&sampling_request, "logprobs", Boolean(result.logprobs));
  if (const auto* seed = request.Find("seed"))
    Set(&sampling_request, "seed", *seed);
  SamplingOptions sampling;
  status = ParseSamplingOptions(sampling_request, &sampling, &error->param);
  if (!status) return status;
  if (const auto* stream_options = request.Find("stream_options");
      stream_options && !stream_options->IsNull()) {
    if (!result.stream)
      return Invalid(error, "stream_options",
                     "stream options require stream=true");
    status = InputFields(*stream_options, {"include_obfuscation"},
                         &error->param, "stream_options");
    if (!status) return status;
    if (const auto* value = stream_options->Find("include_obfuscation")) {
      if (!value->IsBool())
        return Invalid(error, "stream_options.include_obfuscation",
                       "expected boolean");
      result.obfuscation = value->boolean;
    }
  }
  // These are retained service requirements. Validation of their model/tool
  // policy and application happens in the corresponding service stage.
  for (const auto* key : {"access_programs", "moderation"})
    if (const auto* value = request.Find(key); value && !value->IsNull()) {
      if (!value->IsObject())
        return Invalid(error, key, "expected options object");
      Set(&result.options, key, *value);
    }
  status = ValidateRequestCredentials(request);
  if (!status) {
    error->param = "request_id";
    return status;
  }
  if (status)
    status = IntegerOption(request, "request_timeout_ms", 1, 10800000,
                           &result.options, error);
  if (!status) return status;
  *envelope = std::move(result);
  return {};
}

Status PrepareResponsesGeneration(const ResponsesEnvelope& envelope,
                                  const io::Json& resolved_input,
                                  const ChatPlanConfig& config,
                                  ResponsesGenerationPlan* plan,
                                  ChatPlanError* error) {
  *plan = {};
  *error = {};
  ResponsesGenerationPlan result;
  auto request = envelope.request;
  Set(&request, "input", resolved_input);
  auto status = NormalizeResponsesInput(request, &result.input, error);
  if (!status) return status;
  result.generation_request = Object({{"messages", result.input.messages}});
  auto& chat = result.generation_request;
  for (const auto* key :
       {"model", "temperature", "top_p", "parallel_tool_calls", "stream"})
    Set(&chat, key, *envelope.options.Find(key));
  Set(&chat, "max_completion_tokens",
      *envelope.options.Find("max_output_tokens"));
  Set(&chat, "logprobs", Boolean(envelope.logprobs));
  if (envelope.logprobs)
    Set(&chat, "top_logprobs", *envelope.options.Find("top_logprobs"));
  for (const auto* key :
       {"request_id", "cancel_token", "request_timeout_ms", "seed"})
    if (const auto* value = request.Find(key)) Set(&chat, key, *value);
  Json tools, choice;
  status = NormalizeResponsesTools(request, &tools, &choice, error,
                                   &result.effective_tools);
  if (!status) return status;
  Set(&chat, "tools", std::move(tools));
  Set(&chat, "tool_choice", std::move(choice));
  const auto& format = *envelope.options.Find("text")->Find("format");
  if (format.GetString("type") == "json_schema") {
    Json spec = Object();
    for (const auto* key : {"name", "schema", "description", "strict"})
      if (const auto* value = format.Find(key)) Set(&spec, key, *value);
    Set(&chat, "response_format",
        Object({{"type", String("json_schema")},
                {"json_schema", std::move(spec)}}));
  } else {
    Set(&chat, "response_format", format);
  }
  Set(&chat, "enable_thinking", Boolean(envelope.reasoning_effort != "none"));
  std::string guidance;
  if (OneOf(envelope.reasoning_effort, {"low", "medium", "xhigh"})) {
    Set(&chat, "reasoning_effort", String(envelope.reasoning_effort));
  } else if (envelope.reasoning_effort != "none") {
    // The checkpoint has three named effort prompts. Other API levels use
    // explicit natural-language adaptation, not a false numeric token promise.
    Set(&chat, "reasoning_effort", String("medium"));
    if (envelope.reasoning_effort == "minimal")
      guidance =
          "Use minimal reasoning: only the essential steps needed for an "
          "accurate answer.\n";
    else if (envelope.reasoning_effort == "high")
      guidance =
          "Use high reasoning effort: check assumptions and verify the "
          "important steps before answering.\n";
    else
      guidance =
          "Use maximum reasoning effort within the output budget: examine "
          "alternatives and verify the result carefully.\n";
  }
  if (envelope.verbosity == "low")
    guidance += "Keep the final answer concise and directly relevant.\n";
  else if (envelope.verbosity == "high")
    guidance +=
        "Give a detailed final answer with the relevant explanation and "
        "evidence.\n";
  if (!guidance.empty()) {
    Field(chat, "messages")
        .array.insert(Field(chat, "messages").array.begin(),
                      Object({{"role", String("system")},
                              {"content", String(guidance)}}));
    for (auto& boundary : result.input.cache_boundaries)
      ++boundary.message_index;
  }
  if (envelope.reasoning_context == "current_turn") {
    auto& messages = Field(chat, "messages").array;
    size_t last_user = 0;
    for (size_t i = 0; i < messages.size(); ++i)
      if (messages[i].GetString("role") == "user") last_user = i;
    for (size_t i = 0; i < last_user; ++i)
      std::erase_if(messages[i].object, [](const auto& field) {
        return field.first == "reasoning" || field.first == "reasoning_content";
      });
  }
  auto generation_config = config;
  generation_config.prewarm_only = envelope.cache.prewarm;
  status = PlanChatRequest(chat, generation_config, &result.chat, error);
  if (!status) {
    if (!guidance.empty() && error->param.starts_with("messages[")) {
      // Model-adaptation instructions are not caller-controlled input.
      error->param = "input";
    } else {
      ResponsesInputErrorParam(result.input, &error->param);
    }
    if (error->param == "max_completion_tokens")
      error->param = "max_output_tokens";
    if (error->param.starts_with("tool_choice.allowed_tools"))
      error->param.erase(std::string_view("tool_choice").size(),
                         std::string_view(".allowed_tools").size());
    if (error->param.starts_with("tools[") ||
        error->param.starts_with("tool_choice")) {
      for (const auto* wrapper : {".function", ".custom"}) {
        const auto offset = error->param.find(wrapper);
        if (offset != std::string::npos)
          error->param.erase(offset, std::char_traits<char>::length(wrapper));
      }
    }
    if (error->param.starts_with("response_format.json_schema"))
      error->param.replace(0, 27, "text.format");
    else if (error->param.starts_with("response_format"))
      error->param.replace(0, 15, "text.format");
    return status;
  }
  *plan = std::move(result);
  return {};
}

Status FitResponsesContext(const ResponsesEnvelope& envelope,
                           const ChatPlanConfig& config,
                           const ResponsesContextCheck& check,
                           ResponsesGenerationPlan* plan,
                           ChatPlanError* error) {
  if (!envelope.truncate) return {};
  if (!check) {
    *error = {"response_executor_unavailable", "truncation", 503};
    return Status::Fail("Context measurement is unavailable");
  }
  auto status = check(*plan, error);
  if (status || error->code != "context_length_exceeded") return status;
  const auto overflow = status;
  const auto overflow_error = *error;
  const auto& items = plan->input.items.array;
  if (items.empty()) return status;

  // Protect the active user turn. With tool-result-only history, retain the
  // last item and every call it depends on. Prefix cuts never split parallel
  // function/custom calls from their outputs or reasoning from its next item.
  size_t protected_from = items.size() - 1;
  for (size_t i = 0; i < items.size(); ++i)
    if (items[i].GetString("role") == "user") protected_from = i;
  std::unordered_map<std::string, size_t> calls;
  std::vector<int> spanning(items.size() + 1, 0);
  for (size_t i = 0; i < items.size(); ++i) {
    const auto type = items[i].GetString("type");
    if (type == "function_call" || type == "custom_tool_call") {
      calls.emplace(items[i].GetString("call_id"), i);
    } else if (type == "function_call_output" ||
               type == "custom_tool_call_output") {
      const auto found = calls.find(items[i].GetString("call_id"));
      if (found != calls.end()) {
        ++spanning[found->second + 1];
        --spanning[i + 1];
      }
    }
  }
  int open_calls = 0;
  for (size_t cut = 1; cut <= protected_from; ++cut) {
    open_calls += spanning[cut];
    if (open_calls || items[cut - 1].GetString("type") == "reasoning") continue;
    auto retained = Array();
    retained.array.assign(items.begin() + cut, items.end());
    ResponsesGenerationPlan candidate;
    status = PrepareResponsesGeneration(envelope, retained, config, &candidate,
                                        error);
    if (!status) return status;
    // Preserve paths into the original resolved input, including failures
    // discovered while measuring a retained candidate.
    const size_t removed = plan->truncated_items + cut;
    for (auto& path : candidate.input.message_paths) {
      if (!path.starts_with("input[")) continue;
      const auto end = path.find(']', 6);
      size_t index = 0;
      if (end == std::string::npos) continue;
      const auto parsed =
          std::from_chars(path.data() + 6, path.data() + end, index);
      if (parsed.ec == std::errc{} && parsed.ptr == path.data() + end)
        path.replace(6, end - 6, std::to_string(index + removed));
    }
    status = check(candidate, error);
    if (status) {
      candidate.truncated_items = removed;
      *plan = std::move(candidate);
      return {};
    }
    if (error->code != "context_length_exceeded") {
      ResponsesInputErrorParam(candidate.input, &error->param);
      return status;
    }
  }
  *error = overflow_error;
  return overflow;
}
}  // namespace q4t::server
