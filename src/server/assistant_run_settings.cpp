#include "assistant_runs_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/model_catalog.h"

namespace q4t::server::assistant_detail {
namespace {
using namespace object_detail;

Json Setting(const Json& request, const Json& assistant, const char* key,
             Json fallback = {}) {
  if (Nonnull(request, key)) return *request.Find(key);
  if (Nonnull(assistant, key)) return *assistant.Find(key);
  return fallback;
}

Json ResourceIds(const Json& assistant, const Json& thread, const char* tool,
                 const char* key) {
  auto ids = Array();
  for (const auto* config : {&assistant, &thread}) {
    const auto* resources = config->Find("tool_resources");
    const auto* bound = resources ? resources->Find(tool) : nullptr;
    if (const auto* files = bound ? bound->GetArray(key) : nullptr) {
      for (const auto& id : files->array) {
        if (std::ranges::none_of(
                ids.array, [&](const auto& old) { return old.str == id.str; }))
          ids.array.push_back(id);
      }
    }
  }
  return ids;
}

Json NativeTools(const Json& settings, const Json& assistant,
                 const Json& thread) {
  auto converted = Array();
  for (const auto& tool : settings.GetArray("tools")->array) {
    const auto type = tool.GetString("type");
    Json definition;
    if (type == "function") {
      definition = *tool.Find("function");
      // Assistants functions are advisory unless the caller opts into
      // strictness.
      if (!Nonnull(definition, "strict"))
        Set(&definition, "strict", Boolean(false));
      Set(&definition, "type", String(type));
    } else if (type == "file_search") {
      definition =
          tool.Has("file_search") ? *tool.Find("file_search") : Object();
      Set(&definition, "type", String(type));
      if (const auto* ranking = definition.Find("ranking_options");
          ranking && ranking->GetString("ranker") == "default_2024_08_21") {
        // Both API families select the fixed native vector ranking policy.
        Set(&Field(definition, "ranking_options"), "ranker",
            String("default-2024-11-15"));
      }
      Set(&definition, "vector_store_ids",
          ResourceIds(assistant, thread, "file_search", "vector_store_ids"));
    } else {
      definition = Object(
          {{"type", String(type)},
           {"container",
            Object({{"type", String("auto")},
                    {"file_ids", ResourceIds(assistant, thread, type.c_str(),
                                             "file_ids")}})}});
    }
    converted.array.push_back(std::move(definition));
  }
  return converted;
}

Json NativeHistory(const Json& rows, const Json& truncation) {
  auto input = Array();
  size_t start = 0;
  if (truncation.GetString("type") == "last_messages") {
    const auto keep = static_cast<size_t>(truncation.GetInt("last_messages"));
    if (keep < rows.array.size()) start = rows.array.size() - keep;
  }
  for (size_t i = start; i < rows.array.size(); ++i) {
    const auto& row = rows.array[i];
    auto content = Array();
    for (const auto& part : row.Find("content")->array) {
      const auto type = part.GetString("type");
      if (type == "text") {
        content.array.push_back(
            Object({{"type", String("input_text")},
                    {"text", *part.Find("text")->Find("value")}}));
      } else if (type == "image_file" || type == "image_url") {
        const auto& image = *part.Find(type);
        auto block =
            Object({{"type", String("input_image")},
                    {"detail", String(image.GetString("detail", "auto"))}});
        if (type == "image_file")
          Set(&block, "file_id", *image.Find("file_id"));
        else
          Set(&block, "image_url", *image.Find("url"));
        content.array.push_back(std::move(block));
      } else if (type == "refusal") {
        content.array.push_back(Object(
            {{"type", String("input_text")}, {"text", *part.Find("refusal")}}));
      }
    }
    input.array.push_back(
        Object({{"role", *row.Find("role")}, {"content", std::move(content)}}));
  }
  return input;
}
}  // namespace

ObjectReply PrepareRunSettings(const Json& assistant_record,
                               const Json& thread_data, const Json& request,
                               const ChatPlanConfig& config, std::string run_id,
                               int64_t created, Json* run, Json* native) {
  const auto* saved = assistant_record.Find("data");
  const auto* assistant = saved ? saved->Find("assistant") : nullptr;
  const auto* thread = thread_data.Find("thread");
  const auto* messages = thread_data.GetArray("messages");
  ChatPlanError error;
  if (!assistant || !thread || !messages ||
      !ValidateAssistant(*assistant, AssistantShape::kResource, &error))
    return Error(503, "assistant_record_invalid", "Invalid run configuration");
  auto model = Setting(request, *assistant, "model");
  if (!IsServedModel(model.str, config.model, config.model_aliases))
    return Error(404, "model_not_found", "Unknown run model");
  auto instructions = Setting(request, *assistant, "instructions", String(""));
  if (Nonnull(request, "additional_instructions")) {
    if (!instructions.str.empty()) instructions.str += "\n\n";
    instructions.str += request.GetString("additional_instructions");
  }
  auto truncation =
      Setting(request, {}, "truncation_strategy",
              Object({{"type", String("auto")}, {"last_messages", {}}}));
  if (truncation.GetString("type") == "last_messages" &&
      truncation.GetInt("last_messages") < 1)
    return Invalid("Expected positive last_messages",
                   "truncation_strategy.last_messages");
  *run = Object(
      {{"id", String(std::move(run_id))},
       {"object", String("thread.run")},
       {"thread_id", *thread->Find("id")},
       {"assistant_id", *assistant->Find("id")},
       {"created_at", Number(created)},
       {"started_at", {}},
       {"cancelled_at", {}},
       {"completed_at", {}},
       {"failed_at", {}},
       {"expires_at", Number(created + 600)},
       {"status", String("queued")},
       {"required_action", {}},
       {"last_error", {}},
       {"incomplete_details", {}},
       {"usage", {}},
       {"model", std::move(model)},
       {"instructions", std::move(instructions)},
       {"tools", Setting(request, *assistant, "tools", Array())},
       {"metadata", Setting(request, {}, "metadata", Object())},
       {"parallel_tool_calls",
        Setting(request, {}, "parallel_tool_calls", Boolean(true))},
       {"response_format",
        Setting(request, *assistant, "response_format", String("auto"))},
       {"tool_choice", Setting(request, {}, "tool_choice", String("auto"))},
       {"truncation_strategy", truncation},
       {"max_prompt_tokens", Setting(request, {}, "max_prompt_tokens")},
       {"max_completion_tokens", Setting(request, {}, "max_completion_tokens")},
       {"temperature", Setting(request, *assistant, "temperature", Number(1))},
       {"top_p", Setting(request, *assistant, "top_p", Number(1))}});
  auto valid = ValidateAssistant(*run, AssistantShape::kRunResource, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  auto choice = *run->Find("tool_choice");
  if (choice.GetString("type") == "function")
    choice = Object({{"type", String("function")},
                     {"name", *choice.Find("function")->Find("name")}});
  *native = Object({{"background", Boolean(true)},
                    {"store", Boolean(true)},
                    {"input", NativeHistory(*messages, truncation)},
                    {"tools", NativeTools(*run, *assistant, *thread)},
                    {"tool_choice", std::move(choice)},
                    {"truncation", String("auto")}});
  for (const auto* key :
       {"model", "instructions", "parallel_tool_calls", "temperature", "top_p"})
    Set(native, key, *run->Find(key));
  if (Nonnull(*run, "max_completion_tokens"))
    Set(native, "max_output_tokens", *run->Find("max_completion_tokens"));
  const auto effort = Setting(request, *saved, "reasoning_effort");
  if (!effort.IsNull()) Set(native, "reasoning", Object({{"effort", effort}}));
  const auto* format = run->Find("response_format");
  if (format->IsObject()) {
    auto flat = *format;
    if (flat.GetString("type") == "json_schema") {
      flat = *format->Find("json_schema");
      Set(&flat, "type", String("json_schema"));
    }
    Set(native, "text", Object({{"format", std::move(flat)}}));
  }
  return {};
}
}  // namespace q4t::server::assistant_detail
