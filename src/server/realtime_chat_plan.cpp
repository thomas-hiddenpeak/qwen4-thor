#include "q4t/server/realtime_chat_plan.h"

#include <algorithm>
#include <cmath>

#include "q4t/server/application_query.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/file_expansion.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_response_output.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/request_limits.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message,
               std::string code = "invalid_request", int http_status = 400) {
  *error = {std::move(code), std::move(path), http_status};
  return Status::Fail(std::move(message));
}
Status Processor(ChatPlanError* error, std::string path) {
  return Invalid(error, std::move(path),
                 "The required native Realtime processor is not connected",
                 "realtime_processor_unavailable", 503);
}
const Json* Option(const Json& settings, const Json& response,
                   const char* name) {
  if (const auto* value = response.Find(name)) return value;
  return settings.Find(name);
}
Status Overflow(ChatPlanError* error) {
  return Invalid(error, "input", "Realtime input exceeds its context budget",
                 "context_length_exceeded");
}
Status Storage(const RealtimeChatPlan& candidate, ChatPlanError* error) {
  size_t retained = 0, bytes = 0;
  const std::vector<const Json*> stored{
      &candidate.envelope.request,
      &candidate.envelope.options,
      &candidate.generation.input.items,
      &candidate.generation.input.messages,
      &candidate.generation.generation_request,
      &candidate.generation.effective_tools,
      &candidate.generation.active_tools,
      candidate.generation.chat.normalized_request
          ? &*candidate.generation.chat.normalized_request
          : nullptr,
      candidate.generation.chat.HeaderTools()};
  for (const auto* value : stored) {
    if (!value) continue;
    if (!MeasureRealtimeStorage(*value, RealtimeConversation::kMaximumBytes,
                                &bytes) ||
        bytes > (64ULL << 20) - retained)
      return Invalid(error, "input", "Native request storage limit exceeded",
                     "realtime_input_capacity", 413);
    retained += bytes;
  }
  return {};
}
}  // namespace

Json DefaultRealtimeChatSettings(const ChatPlanConfig& config,
                                 std::string model) {
  return Object({{"type", String("realtime")},
                 {"model", String(model.empty() ? config.model : model)},
                 {"instructions", String("You are a helpful assistant.")},
                 {"max_output_tokens", String("inf")},
                 {"output_modalities", Array({String("text")})},
                 {"parallel_tool_calls", Boolean(true)},
                 {"tools", Array()},
                 {"tool_choice", String("auto")},
                 {"truncation", String("auto")}});
}

Status ParseRealtimeChatTarget(std::string_view target,
                               const ChatPlanConfig& config, Json* settings,
                               ChatPlanError* error) {
  const auto split = target.find('?');
  if (target.substr(0, split) != "/v1/realtime")
    return Invalid(error, "path", "Invalid Realtime target");
  auto query = ParseApplicationQuery(
      split == target.npos ? std::string_view{} : target.substr(split + 1));
  if (!query.ok()) {
    *error = {query.code, query.value.GetString("param"), query.http_status};
    return Status::Fail(query.message);
  }
  auto status =
      InputFields(query.value, {"model", "intent"}, &error->param, "query");
  if (!status) return status;
  if (query.value.Has("intent") &&
      query.value.GetString("intent") != "realtime")
    return Invalid(error, "intent", "Invalid Realtime conversation intent");
  if (query.value.Has("model") && query.value.GetString("model").empty())
    return Invalid(error, "model", "Empty model identifier");
  *settings =
      DefaultRealtimeChatSettings(config, query.value.GetString("model"));
  return {};
}

Status PlanRealtimeChat(const Json& settings, const Json& response,
                        const Json& items, const ChatPlanConfig& config,
                        RealtimeChatPlan* plan, ChatPlanError* error,
                        const PromptSnapshot* prompt) {
  if (!plan || !error) return Status::Fail("Missing Realtime plan owner");
  *error = {};
  auto status = ValidateRealtimeWireShape(
      Object({{"type", String("session.update")}, {"session", settings}}),
      RealtimeShape::kClientEvent, error);
  if (status)
    status = ValidateRealtimeWireShape(
        Object({{"type", String("response.create")}, {"response", response}}),
        RealtimeShape::kClientEvent, error);
  if (!status) return status;
  size_t bytes = 0;
  if (settings.GetString("type") != "realtime" || !items.IsArray() ||
      items.array.size() > RealtimeConversation::kMaximumItems ||
      !MeasureRealtimeStorage(items, RealtimeConversation::kMaximumBytes,
                              &bytes) ||
      !MeasureRealtimeStorage(settings, RealtimeConversation::kMaximumBytes,
                              &bytes) ||
      !MeasureRealtimeStorage(response, RealtimeConversation::kMaximumBytes,
                              &bytes))
    return Invalid(error, "input", "Invalid bounded Realtime snapshot");
  if (const auto* metadata = response.Find("metadata");
      metadata && !RealtimeMetadataBounded(*metadata))
    return Invalid(error, "response.metadata", "Invalid response metadata");
  // These processors are connected separately. Never acknowledge a setting
  // here and silently drop its actual audio, trace or remote tool work.
  for (const auto* field : {"audio"}) {
    if (const auto* value = Option(settings, response, field);
        value && !value->IsNull() &&
        (!value->IsObject() || !value->object.empty()))
      return Processor(error, field);
  }
  const auto* reference = Option(settings, response, "prompt");
  if (reference && !reference->IsNull()) {
    if (!prompt) return Processor(error, "prompt");
    if (prompt->api != PromptApi::kRealtime || prompt->scope.empty() ||
        !prompt->request.IsObject() ||
        prompt->reference.GetString("version").empty() ||
        OutputJson(*reference) != OutputJson(prompt->reference))
      return Invalid(error, "prompt", "Prompt snapshot identity differs",
                     "prompt_snapshot_mismatch", 500);
  } else if (prompt) {
    return Invalid(error, "prompt", "Unexpected prompt snapshot",
                   "prompt_snapshot_mismatch", 500);
  }
  if (const auto* trace = settings.Find("tracing"); trace && !trace->IsNull())
    return Processor(error, "tracing");
  if (const auto* include = settings.GetArray("include");
      include && !include->array.empty())
    return Processor(error, "include");
  if (const auto* modes = Option(settings, response, "output_modalities");
      modes && (modes->array.size() != 1 || modes->array[0].str != "text"))
    return Processor(error, "output_modalities");

  auto request =
      Object({{"model", String(settings.GetString("model", config.model))},
              {"store", Boolean(false)},
              {"stream", Boolean(false)}});
  for (const auto* key : {"instructions", "parallel_tool_calls", "tools",
                          "tool_choice", "reasoning"})
    if (const auto* value = Option(settings, response, key))
      Set(&request, key, *value);
  if (!request.Has("instructions"))
    Set(&request, "instructions", String("You are a helpful assistant."));
  // Ordinary Realtime models do not expose a reasoning stream. An explicit
  // effort still uses the same checkpoint adaptation as native Responses.
  if (!request.Has("reasoning"))
    Set(&request, "reasoning", Object({{"effort", String("none")}}));

  if (auto* tools = request.Has("tools") ? &Field(request, "tools") : nullptr)
    for (size_t i = 0; i < tools->array.size(); ++i) {
      auto& tool = tools->array[i];
      if (tool.GetString("type") == "mcp")
        return Processor(error, "tools[" + std::to_string(i) + ']');
      // Realtime's function type is optional and has no Responses strict
      // default. Keep an open schema open instead of silently tightening it.
      Set(&tool, "type", String("function"));
      Set(&tool, "strict", Boolean(false));
      if (!tool.Has("parameters"))
        Set(&tool, "parameters", Object({{"type", String("object")}}));
    }
  const auto* limit = Option(settings, response, "max_output_tokens");
  int output = std::min(4096, config.max_len - 1);
  if (config.client_limits)
    output = static_cast<int>(std::min<uint64_t>(
        std::max(0, output), config.client_limits->max_output_tokens));
  if (limit && !(limit->IsString() && limit->str == "inf")) {
    if (!IsInputInteger(*limit) || limit->number < 1 || limit->number > 4096)
      return Invalid(error, "max_output_tokens", "Expected 1..4096 or inf");
    output = static_cast<int>(limit->number);
  }
  if (output < 1)
    return Invalid(error, "max_output_tokens", "No output capacity available");
  Set(&request, "max_output_tokens", Number(output));

  RealtimeChatPlan candidate;
  candidate.truncation = settings.Has("truncation")
                             ? *settings.Find("truncation")
                             : String("auto");
  if (candidate.truncation.IsObject()) {
    const auto* ratio = candidate.truncation.Find("retention_ratio");
    if (!ratio || !std::isfinite(ratio->number) || ratio->number < 0 ||
        ratio->number > 1)
      return Invalid(error, "truncation.retention_ratio", "Expected 0..1");
    if (const auto* limits = candidate.truncation.Find("token_limits"))
      if (const auto* post = limits->Find("post_instructions");
          post && (!IsInputInteger(*post) || post->number < 1 ||
                   post->number > std::max(0, config.max_len - output)))
        return Invalid(error, "truncation.token_limits.post_instructions",
                       "Invalid post-instruction token budget");
  }
  Set(&request, "truncation",
      String(candidate.truncation.IsString() ? candidate.truncation.str
                                             : "auto"));

  auto mapped = Array();
  if (prompt) {
    const auto* prefix = prompt->request.Find("input");
    if (prefix && !prefix->IsNull()) {
      if (prefix->IsString())
        mapped.array.push_back(
            Object({{"role", String("user")}, {"content", *prefix}}));
      else if (prefix->IsArray())
        mapped = *prefix;
      else
        return Invalid(error, "prompt", "Invalid prompt input snapshot",
                       "prompt_snapshot_mismatch", 500);
    }
  }
  candidate.prompt_items = mapped.array.size();
  for (size_t i = 0; i < items.array.size(); ++i) {
    const auto& original = items.array[i];
    const auto path = "input[" + std::to_string(i) + ']';
    status = ValidateRealtimeShape(original, RealtimeShape::kItem, error, path);
    if (!status) return status;
    auto item = original;
    std::erase_if(item.object,
                  [](const auto& field) { return field.first == "object"; });
    if (item.GetString("type").starts_with("mcp_"))
      return Processor(error, path);
    if (auto* content = item.Has("content") ? &Field(item, "content") : nullptr)
      for (size_t j = 0; j < content->array.size(); ++j) {
        auto& part = content->array[j];
        const auto type = part.GetString("type");
        if (type == "input_audio" || type == "output_audio")
          return Processor(error, path + ".content[" + std::to_string(j) + ']');
        if (type.empty() && part.Has("text") && part.object.size() == 1)
          Set(&part, "type",
              String(item.GetString("role") == "assistant" ? "output_text"
                                                           : "input_text"));
      }
    mapped.array.push_back(std::move(item));
  }
  Set(&request, "input", std::move(mapped));
  status = ParseResponsesEnvelope(request, config, &candidate.envelope, error);
  if (!status) return status;
  if (HasFileInputs(*request.Find("input"))) {
    Json tools;
    status = ValidateResponsesPreparation(
        candidate.envelope, *request.Find("input"), config, &tools, error);
    if (status) status = Storage(candidate, error);
  } else {
    status = CompleteRealtimeChatInput(config, *request.Find("input"), false,
                                       &candidate, error);
  }
  if (!status) return status;
  *plan = std::move(candidate);
  return {};
}

Status CompleteRealtimeChatInput(const ChatPlanConfig& config,
                                 const Json& resolved, bool prepared_file_media,
                                 RealtimeChatPlan* plan, ChatPlanError* error) {
  if (!plan || !error) return Status::Fail("Missing Realtime input owner");
  const auto* original = plan->envelope.request.GetArray("input");
  if (!original || !resolved.IsArray() ||
      original->array.size() != resolved.array.size())
    return Invalid(error, "input", "Prepared input ownership changed",
                   "realtime_input_owner_invalid", 500);
  auto candidate = *plan;
  candidate.envelope.prepared_file_media = prepared_file_media;
  auto status = PrepareResponsesGeneration(candidate.envelope, resolved, config,
                                           &candidate.generation, error);
  if (!status) return status;
  if (candidate.generation.input.items.array.empty()) {
    // response.create itself asks for a new answer even without a user item.
    // This text checkpoint expects the task in a user turn. Mirror the actual
    // caller instructions without a meta-instruction to follow adapter rules.
    // Keep this transport cue ephemeral and the instruction role unchanged.
    // The normal template/tokenizer counts its actual input and quota cost.
    auto& generation = candidate.generation;
    auto& chat = generation.generation_request;
    auto task = candidate.envelope.request.GetString("instructions");
    if (task.empty()) task = "Generate the next assistant response.";
    Field(chat, "messages")
        .array.push_back(
            Object({{"role", String("user")}, {"content", String(task)}}));
    auto planning = config;
    planning.async_tool_call_ids = generation.chat.async_tool_call_ids;
    planning.responses_tool_media = generation.chat.responses_tool_media;
    planning.header_tools = generation.chat.header_tools;
    planning.tool_call_formats = generation.chat.tool_call_formats;
    status = PlanChatRequest(chat, planning, &generation.chat, error);
    if (!status) return status;
  }
  status = Storage(candidate, error);
  if (!status) return status;
  candidate.input_ready = true;
  *plan = std::move(candidate);
  return {};
}

std::vector<size_t> RealtimeRetainedHistory(const RealtimeChatPlan& plan) {
  std::vector<size_t> retained;
  for (const auto index : plan.generation.input_source_indices)
    if (index >= plan.prompt_items)
      retained.push_back(index - plan.prompt_items);
  return retained;
}

Status FitRealtimeChatContext(const ChatPlanConfig& config,
                              const RealtimeContextMeasure& measure,
                              RealtimeChatPlan* plan, ChatPlanError* error) {
  if (!measure || !plan || !error)
    return Status::Fail("Missing Realtime context owner");
  if (!plan->input_ready)
    return Invalid(error, "input", "Owned file preparation is pending",
                   "realtime_input_preparation_pending", 503);
  const auto maximum = config.max_len;
  const auto output = plan->envelope.options.GetInt("max_output_tokens");
  if (maximum <= output || output < 1) return Overflow(error);
  const size_t input_limit = static_cast<size_t>(maximum - output);
  size_t tokens = 0;
  auto status = measure(plan->generation, &tokens, error);
  if (!status) return status;
  size_t threshold = input_limit, target = input_limit;
  if (plan->truncation.IsObject()) {
    ResponsesGenerationPlan instructions;
    status = PrepareResponsesGeneration(plan->envelope, Array(), config,
                                        &instructions, error);
    if (!status) return status;
    size_t prefix = 0;
    status = measure(instructions, &prefix, error);
    if (!status) return status;
    if (prefix >= input_limit) return Overflow(error);
    size_t post_limit = input_limit - prefix;
    if (const auto* limits = plan->truncation.Find("token_limits"))
      if (const auto* post = limits->Find("post_instructions")) {
        if (post->number > post_limit)
          return Invalid(error, "truncation.token_limits.post_instructions",
                         "Post-instruction budget exceeds actual context");
        post_limit = static_cast<size_t>(post->number);
      }
    threshold = prefix + post_limit;
    target =
        prefix +
        static_cast<size_t>(std::floor(
            post_limit * plan->truncation.Find("retention_ratio")->number));
  }
  if (tokens <= threshold) return {};
  if (!plan->envelope.truncate) return Overflow(error);
  // Reuse the existing group-safe cut selection. Authentication, quotas and
  // arbitrary validation failures never trigger dropping historical input.
  return FitResponsesContext(
      plan->envelope, config,
      [&](const ResponsesGenerationPlan& candidate, ChatPlanError* failure) {
        size_t measured = 0;
        auto checked = measure(candidate, &measured, failure);
        if (!checked) return checked;
        return measured <= target ? Status{} : Overflow(failure);
      },
      &plan->generation, error);
}
}  // namespace q4t::server
