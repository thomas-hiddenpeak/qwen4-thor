#include "q4t/server/responses_output.h"

#include <algorithm>
#include <cmath>
#include <limits>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_input.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/request_json.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr size_t kNoCall = std::numeric_limits<size_t>::max();
Status Probabilities(std::string_view json, bool refusal, io::Json* out) {
  *out = Array();
  if (json.empty() || json == "null") return {};
  io::Json parsed;
  auto status =
      io::ParseJson(std::string(json), &parsed, true, kRequestJsonLimits);
  const auto* content = parsed.GetArray(refusal ? "refusal" : "content");
  if (!status || !content)
    return Status::Fail("invalid Responses token probability source");
  const auto row_value = [](const io::Json& row, io::Json* result) {
    const auto* token = row.Find("token");
    const auto* score = row.Find("logprob");
    const auto* bytes = row.GetArray("bytes");
    if (!token || !token->IsString() || !score || !score->IsNumber() ||
        !std::isfinite(score->number) || score->number > 0 || !bytes)
      return Status::Fail("invalid Responses token probability row");
    for (const auto& byte : bytes->array)
      if (!IsInputInteger(byte) || byte.number < 0 || byte.number > 255)
        return Status::Fail("invalid Responses probability token bytes");
    *result =
        Object({{"token", *token}, {"bytes", *bytes}, {"logprob", *score}});
    return Status();
  };
  for (const auto& row : content->array) {
    io::Json result;
    status = row_value(row, &result);
    if (!status) return status;
    Set(&result, "top_logprobs", Array());
    if (row.Has("top_logprobs") && !row.GetArray("top_logprobs"))
      return Status::Fail("invalid Responses top probability list");
    if (const auto* top = row.GetArray("top_logprobs")) {
      io::Json candidates = Array();
      for (const auto& candidate : top->array) {
        io::Json value;
        status = row_value(candidate, &value);
        if (!status) return status;
        candidates.array.push_back(std::move(value));
      }
      Set(&result, "top_logprobs", std::move(candidates));
    }
    out->array.push_back(std::move(result));
  }
  return {};
}
// The SDK's delta/done probability shape differs from persisted output_text
// content: only the latter carries original token bytes. Never reconstruct
// bytes from normalized text (partial UTF-8 token pieces may differ).
io::Json EventProbabilities(io::Json probabilities) {
  for (auto& row : probabilities.array) {
    std::erase_if(row.object,
                  [](const auto& field) { return field.first == "bytes"; });
    for (auto& top : Field(row, "top_logprobs").array)
      std::erase_if(top.object,
                    [](const auto& field) { return field.first == "bytes"; });
  }
  return probabilities;
}
// ResponseErrorCode from the pinned Responses contract. Keep recognized
// provider codes, including image failures, intact when replaying records.
std::string_view PublicFailureCode(std::string_view code) {
  constexpr std::string_view known[] = {"server_error",
                                        "rate_limit_exceeded",
                                        "invalid_prompt",
                                        "data_residency_mismatch",
                                        "bio_policy",
                                        "misalignment_policy_violation",
                                        "vector_store_timeout",
                                        "invalid_image",
                                        "invalid_image_format",
                                        "invalid_base64_image",
                                        "invalid_image_url",
                                        "image_too_large",
                                        "image_too_small",
                                        "image_parse_error",
                                        "image_content_policy_violation",
                                        "invalid_image_mode",
                                        "image_file_too_large",
                                        "unsupported_image_media_type",
                                        "empty_image_file",
                                        "failed_to_download_image",
                                        "image_file_not_found"};
  if (std::ranges::find(known, code) != std::end(known)) return code;
  if (code == "client_rpm_exhausted" || code == "client_tpm_exhausted")
    return "rate_limit_exceeded";
  if (code == "context_length_exceeded" ||
      code == "client_prompt_limit_exceeded")
    return "invalid_prompt";
  return "server_error";
}
void ProjectFailure(io::Json& error) {
  const auto code = error.GetString("code");
  const auto public_code = PublicFailureCode(code);
  if (public_code == code) return;
  const auto* type = error.Find("type");
  if (!code.empty() && (!type || type->IsNull()))
    Set(&error, "type", String(code));
  Set(&error, "code", String(std::string(public_code)));
}

void ProjectValue(io::Json& value, bool wire) {
  const auto type = value.GetString("type");
  if (type == "custom_tool_call")
    std::erase_if(value.object, [](const auto& field) {
      return field.first == "status" || field.first == "created_by";
    });
  // Hosted action results are generated by the executor. With no token
  // scores the HTTP output type requires an empty array, including on replay.
  if (wire && type == "multi_agent_call_output" && value.GetArray("output"))
    for (auto& part : Field(value, "output").array)
      if (part.GetString("type") == "output_text" && !Nonnull(part, "logprobs"))
        Set(&part, "logprobs", Array());
  const auto object = value.GetString("object");
  if (object == "response" || object == "response.compaction") {
    if (wire && object == "response")
      SetResponseCacheRetentionSpelling(
          value, detail::HttpResponseContext::LegacyCacheRetention());
    // The API expresses a required local shell using an allowed-tools list.
    // Apply the projection on replay as well as newly generated responses.
    const auto* choice = value.Find("tool_choice");
    if (wire && object == "response" && choice &&
        choice->GetString("type") == "local_shell") {
      auto tools = Array();
      tools.array.push_back(*choice);
      Set(&value, "tool_choice",
          Object({{"type", String("allowed_tools")},
                  {"mode", String("required")}, {"tools", std::move(tools)}}));
    }
    if (value.GetArray("output"))
      for (auto& item : Field(value, "output").array) ProjectValue(item, wire);
    if (wire && object == "response" && value.GetString("status") == "failed" &&
        value.Has("error") && Field(value, "error").IsObject())
      ProjectFailure(Field(value, "error"));
  }
  if (type.starts_with("response.")) {
    if (value.Has("response")) ProjectValue(Field(value, "response"), wire);
    if (value.Has("item")) ProjectValue(Field(value, "item"), wire);
  }
}
}  // namespace

void SetResponseCacheRetentionSpelling(io::Json& response, bool legacy) {
  if (response.GetString("object") != "response") return;
  if (response.GetString("prompt_cache_retention") ==
      (legacy ? "in_memory" : "in-memory"))
    responses_detail::Field(response, "prompt_cache_retention").str =
        legacy ? "in-memory" : "in_memory";
}

io::Json ResponsesPublicValue(io::Json value) {
  ProjectValue(value, false);
  return value;
}

void ProjectMcpErrorStrings(io::Json& value) {
  if (value.GetString("type") == "mcp_call") {
    auto* error = value.Find("error");
    if (error && error->IsObject())
      Set(&value, "error", String(OutputJson(*error)));
    return;
  }
  const auto object = value.GetString("object");
  if (object == "response" || object == "response.compaction" ||
      object == "list") {
    const auto key = object == "list" ? "data" : "output";
    if (value.GetArray(key))
      for (auto& item : Field(value, key).array) ProjectMcpErrorStrings(item);
  } else if (value.GetString("type").starts_with("response.")) {
    if (value.Has("item")) ProjectMcpErrorStrings(Field(value, "item"));
    if (value.Has("response")) ProjectMcpErrorStrings(Field(value, "response"));
  }
}

bool ProjectFunctionArgumentName(io::Json* event, const io::Json& response) {
  if (event->GetString("type") != "response.function_call_arguments.done" ||
      !detail::HttpResponseContext::LegacyFunctionArgumentName())
    return true;
  const auto* index = event->Find("output_index");
  const auto* items = response.GetArray("output");
  if (!index || !IsInputInteger(*index) || index->AsInt() < 0 || !items ||
      static_cast<size_t>(index->AsInt()) >= items->array.size())
    return false;
  const auto& item = items->array[static_cast<size_t>(index->AsInt())];
  if (item.GetString("type") != "function_call" ||
      item.GetString("id").empty() ||
      item.GetString("id") != event->GetString("item_id") ||
      item.GetString("name").empty())
    return false;
  Set(event, "name", *item.Find("name"));
  return true;
}

io::Json ResponsesWireValue(io::Json value) {
  ProjectValue(value, true);
  if (detail::HttpResponseContext::LegacyMcpError())
    ProjectMcpErrorStrings(value);
  return value;
}

Status ResponsesOutput::Init(const io::Json& options, std::string id,
                             int64_t created, bool obfuscation, size_t limit) {
  if (initialized_ || !options.IsObject() || id.empty() || created < 0 ||
      !limit || limit > kStorageLimit)
    return Status::Fail("invalid Responses output initialization");
  limit_ = limit;
  obfuscation_ = obfuscation;
  ChatPlanError error;
  auto identity =
      PlanResponsesToolIdentities(options, &tool_identities_, &error);
  if (!identity) return identity;
  response_ =
      Object({{"id", String(std::move(id))},
              {"object", String("response")},
              {"created_at", Number(created)},
              {"completed_at", {}},
              {"status", String(options.GetBool("background") ? "queued"
                                                              : "in_progress")},
              {"error", {}},
              {"incomplete_details", {}},
              {"output", Array()},
              {"usage", {}}});
  for (const char* key : {"model",
                          "instructions",
                          "metadata",
                          "temperature",
                          "top_p",
                          "parallel_tool_calls",
                          "personality",
                          "image",
                          "tools",
                          "tool_choice",
                          "text",
                          "reasoning",
                          "store",
                          "background",
                          "max_output_tokens",
                          "max_tool_calls",
                          "previous_response_id",
                          "conversation",
                          "prompt",
                          "prompt_cache_key",
                          "prompt_cache_options",
                          "prompt_cache_retention",
                          "user",
                          "safety_identifier",
                          "service_tier",
                          "truncation",
                          "top_logprobs",
                          "access_programs",
                          "q4t_access_program"}) {
    if (const auto* value = options.Find(key)) Set(&response_, key, *value);
  }
  auto status = Charge(OutputJson(response_).size());
  initialized_ = status.ok();
  return status;
}

Status ResponsesOutput::Charge(size_t bytes) {
  if (bytes > limit_ || bytes_ > limit_ - bytes)
    return Status::Fail("Responses output storage exhausted");
  bytes_ += bytes;
  return {};
}

void ApplyDeltaObfuscation(io::Json* event, bool enabled) {
  std::erase_if(event->object,
                [](const auto& field) { return field.first == "obfuscation"; });
  if (!enabled || !event->GetString("type").ends_with(".delta")) return;
  const auto random = detail::NewRequestId("");
  const size_t size = 128 - ((OutputJson(*event).size() + 18) % 128) +
                      ((random[0] & 1) ? 128 : 0);
  std::string padding;
  while (padding.size() < size) padding += detail::NewRequestId("");
  padding.resize(size);
  Set(event, "obfuscation", String(std::move(padding)));
}

void ResponsesOutput::Event(std::string_view type, io::Json fields,
                            std::string* wire) {
  if (const auto* index = fields.Find("output_index"); index && index->IsNumber()) {
    const auto numbered = static_cast<size_t>(index->AsInt());
    if (numbered < Items().array.size())
      if (const auto* owner = Items().array[numbered].Find("agent"))
        Set(&fields, "agent", *owner);
  }
  Set(&fields, "type", String(std::string(type)));
  Set(&fields, "sequence_number", Number(sequence_++));
  // Capture consumes this wire before PublicWire renders it for the reader.
  ProjectValue(fields, true);
  ApplyDeltaObfuscation(&fields, obfuscation_);
  *wire += "event: " + std::string(type) + "\r\ndata: " + OutputJson(fields) +
           "\r\n\r\n";
}

io::Json& ResponsesOutput::Items() { return Field(response_, "output"); }
void ResponsesOutput::CacheDiagnostics(io::Json value) noexcept {
  try {
    if (initialized_ && !terminal_ && value.IsObject() &&
        OutputJson(value).size() <= 512)
      Set(&response_, "prompt_cache_diagnostics", std::move(value));
  } catch (...) {
    // Optional diagnostics must not turn a valid response into a failure.
  }
}
io::Json ResponsesOutput::Location(size_t index, bool content) const {
  const auto& item = response_.GetArray("output")->array.at(index);
  auto result =
      Object({{"item_id", *item.Find("id")}, {"output_index", Number(index)}});
  if (content) Set(&result, "content_index", Number(0));
  return result;
}

Status ResponsesOutput::Start(std::string* wire) {
  wire->clear();
  if (!initialized_ || started_ || terminal_)
    return Status::Fail("Responses stream already started or uninitialized");
  started_ = true;
  Event("response.created", Object({{"response", response_}}), wire);
  Event(response_.GetString("status") == "queued" ? "response.queued"
                                                  : "response.in_progress",
        Object({{"response", response_}}), wire);
  return {};
}

Status ResponsesOutput::StartRunning(std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || response_.GetString("status") != "queued")
    return Status::Fail("Response is not queued");
  Set(&response_, "status", String("in_progress"));
  Event("response.in_progress", Object({{"response", response_}}), wire);
  return {};
}

Status ResponsesOutput::RestoreQueued(const io::Json& snapshot,
                                      int64_t next_sequence) {
  if (!initialized_ || started_ || terminal_ || next_sequence != 2 ||
      snapshot.GetString("status") != "queued" ||
      !snapshot.GetBool("background") ||
      snapshot.GetString("id") != response_.GetString("id") ||
      !snapshot.GetArray("output") ||
      !snapshot.GetArray("output")->array.empty())
    return Status::Fail("Invalid queued response snapshot");
  bytes_ = 0;
  auto status = Charge(OutputJson(snapshot).size());
  if (!status) return status;
  response_ = snapshot;
  sequence_ = next_sequence;
  started_ = true;
  return {};
}

Status ResponsesOutput::Cancel() {
  if (!started_ || terminal_ || !response_.GetBool("background"))
    return Status::Fail("Cancellation requires an active background response");
  Set(&response_, "status", String("cancelled"));
  Set(&response_, "error", io::Json());
  for (size_t i = 0; i < Items().array.size(); ++i)
    if (!closed_items_.contains(i)) {
      const auto type = Items().array[i].GetString("type");
      if (type == "file_search_call" || type == "mcp_call" ||
          type == "code_interpreter_call" || type == "web_search_call" ||
          type == "shell_call")
        Set(&Items().array[i], "status", String("incomplete"));
      else if (type == "mcp_list_tools")
        Set(&Items().array[i], "error", String("MCP listing cancelled"));
    }
  terminal_ = true;
  return {};
}

Status ResponsesOutput::ChargeItem(const io::Json& item) {
  const size_t bytes = OutputJson(item).size() + 8 * sizeof(io::Json);
  if (item.GetString("type") != "shell_call") return Charge(bytes);
  // Each command has three independently framed events, including optional
  // delta padding. Bound fan-out even for an array of empty commands.
  const auto count = item.Find("action")->GetArray("commands")->array.size();
  constexpr size_t kCommandEventBytes = 1024;
  if (bytes > limit_ || count > (limit_ - bytes) / kCommandEventBytes)
    return Status::Fail("Shell command events exceed response capacity");
  return Charge(bytes + count * kCommandEventBytes);
}

void ResponsesOutput::EmitItemAdded(size_t index, std::string* wire) {
  const auto& item = Items().array[index];
  auto added = item;
  const bool shell = item.GetString("type") == "shell_call";
  if (shell) {
    Set(&added, "status", String("in_progress"));
    Set(&Field(added, "action"), "commands", Array());
  }
  Event("response.output_item.added",
        Object({{"output_index", Number(index)}, {"item", std::move(added)}}),
        wire);
  if (!shell) return;
  const auto& commands = item.Find("action")->GetArray("commands")->array;
  for (size_t i = 0; i < commands.size(); ++i) {
    Event("response.shell_call_command.added",
          Object({{"output_index", Number(index)},
                  {"command_index", Number(i)},
                  {"command", String("")}}),
          wire);
    Event("response.shell_call_command.delta",
          Object({{"output_index", Number(index)},
                  {"command_index", Number(i)},
                  {"delta", commands[i]}}),
          wire);
  }
}

Status ResponsesOutput::AddItem(ResponseDelta::Kind kind,
                                const ResponseToolCall* call, size_t* index,
                                std::string* wire) {
  if (Items().array.size() >= kMaxItems)
    return Status::Fail("Responses output item capacity exhausted");
  io::Json item;
  const bool refusal = kind == ResponseDelta::Kind::kRefusal;
  const bool message = kind == ResponseDelta::Kind::kContent || refusal;
  if (kind == ResponseDelta::Kind::kTool) {
    const ResponsesToolIdentity* identity = nullptr;
    for (const auto& candidate : tool_identities_)
      if (candidate.model_name == call->name &&
          (candidate.type == "custom") == call->custom) {
        identity = &candidate;
        break;
      }
    if (!identity && (call->name.starts_with("q4t_ns_") ||
                      call->name.starts_with("q4t_builtin_")))
      return Status::Fail("unknown Responses internal tool identity");
    if (identity && !identity->direct)
      return Status::Fail(
          "Program-only tool cannot be emitted by a direct caller");
    if (identity && identity->type == "shell") {
      io::Json action;
      auto parsed = ParseResponsesShellAction(call->arguments, &action);
      if (!parsed) return parsed;
      item = Object({{"id", String(detail::NewRequestId("sh_"))},
                     {"type", String("shell_call")},
                     {"call_id", String(call->id)},
                     {"environment", Object({{"type", String("local")}})},
                     {"caller", Object({{"type", String("direct")}})},
                     {"action", std::move(action)}});
    } else if (identity && identity->type == "apply_patch") {
      io::Json operation;
      if (!io::ParseJson(call->arguments, &operation, true, kSchemaJsonLimits))
        return Status::Fail("Invalid generated patch operation");
      item = Object({{"id", String(detail::NewRequestId("apc_"))},
                     {"type", String("apply_patch_call")},
                     {"call_id", String(call->id)},
                     {"status", String("in_progress")},
                     {"caller", Object({{"type", String("direct")}})},
                     {"operation", std::move(operation)}});
      ChatPlanError error;
      auto valid = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                              &error, "item");
      if (!valid) return valid;
    } else if (identity && identity->type == "local_shell") {
      item = Object({{"id", String(detail::NewRequestId("lsh_"))},
                     {"type", String("local_shell_call")},
                     {"call_id", String(call->id)},
                     {"action", Object({{"type", String("exec")},
                                        {"command", Array()},
                                        {"env", Object()}})}});
    } else {
      item = Object(
          {{"id", String(detail::NewRequestId(call->custom ? "ctc_" : "fc_"))},
           {"type",
            String(call->custom ? "custom_tool_call" : "function_call")},
           {"call_id", String(call->id)},
           {"name", String(identity ? identity->name : call->name)},
           {call->custom ? "input" : "arguments", String("")}});
      if (identity && !identity->name_space.empty())
        Set(&item, "namespace", String(identity->name_space));
      if (identity && identity->async) Set(&item, "async", Boolean(true));
      Set(&item, "caller", Object({{"type", String("direct")}}));
    }
  } else {
    item =
        Object({{"id", String(detail::NewRequestId(message ? "msg_" : "rs_"))},
                {"type", String(message ? "message" : "reasoning")},
                {"content", Array()}});
    if (message)
      Set(&item, "role", String("assistant"));
    else
      Set(&item, "summary", Array());
  }
  Set(&item, "status", String("in_progress"));
  auto status = ChargeItem(item);
  if (!status) return status;
  *index = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(call ? call->index : kNoCall);
  EmitItemAdded(*index, wire);
  if (kind != ResponseDelta::Kind::kTool) {
    auto part = Object({{"type", String(refusal   ? "refusal"
                                        : message ? "output_text"
                                                  : "reasoning_text")},
                        {refusal ? "refusal" : "text", String("")}});
    if (kind == ResponseDelta::Kind::kContent) {
      Set(&part, "annotations", Array());
      Set(&part, "logprobs", Array());
    }
    Field(Items().array.back(), "content").array.push_back(part);
    auto fields = Location(*index, true);
    Set(&fields, "part", std::move(part));
    Event("response.content_part.added", std::move(fields), wire);
  }
  return {};
}

Status ResponsesOutput::Append(const ResponseDelta& delta,
                               std::string_view chat_logprobs,
                               std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || response_.GetString("status") != "in_progress")
    return Status::Fail("Responses output is not in progress");
  const bool tool = delta.kind == ResponseDelta::Kind::kTool;
  const bool refusal = delta.kind == ResponseDelta::Kind::kRefusal;
  if (!tool && delta.text.empty()) return {};
  io::Json probabilities;
  auto status = Probabilities(chat_logprobs, refusal, &probabilities);
  if (!status) return status;
  const auto& added = tool ? delta.call.arguments : delta.text;
  if (added.size() > limit_ / 6)
    return Status::Fail("Responses output fragment exceeds storage limit");
  status = Charge(added.size() * 6 + OutputJson(probabilities).size() +
                  probabilities.array.size() * sizeof(io::Json) * 8);
  if (!status) return status;
  size_t index = 0;
  if (tool) {
    const auto it = calls_.find(delta.call.index);
    if (delta.tool_start) {
      if (it != calls_.end() || delta.call.id.empty() ||
          delta.call.name.empty())
        return Status::Fail(
            "invalid Responses tool identity or duplicate start");
      for (const auto& item : Items().array)
        if (item.GetString("call_id") == delta.call.id)
          return Status::Fail("duplicate Responses call_id");
      status = AddItem(delta.kind, &delta.call, &index, wire);
      if (!status) return status;
      calls_.emplace(delta.call.index, index);
      model_names_.emplace(delta.call.index, delta.call.name);
    } else {
      if (it == calls_.end())
        return Status::Fail("Responses tool fragment without a start");
      index = it->second;
      if (closed_items_.contains(index))
        return Status::Fail("Responses tool fragment after completion");
      const auto& item = Items().array[index];
      if ((!delta.call.id.empty() &&
           delta.call.id != item.GetString("call_id")) ||
          (!delta.call.name.empty() &&
           delta.call.name != model_names_.at(delta.call.index)) ||
          delta.call.custom != (item.GetString("type") == "custom_tool_call"))
        return Status::Fail("Responses tool identity changed within stream");
    }
  } else {
    const auto type =
        delta.kind == ResponseDelta::Kind::kReasoning ? "reasoning" : "message";
    const auto part_type = refusal ? "refusal"
                           : delta.kind == ResponseDelta::Kind::kContent
                               ? "output_text"
                               : "reasoning_text";
    const auto* parts = Items().array.empty()
                            ? nullptr
                            : Items().array.back().GetArray("content");
    if (Items().array.empty() ||
        Items().array.back().GetString("type") != type || !parts ||
        parts->array.empty() ||
        parts->array[0].GetString("type") != part_type) {
      status = AddItem(delta.kind, nullptr, &index, wire);
      if (!status) return status;
    } else {
      index = Items().array.size() - 1;
    }
  }
  auto& item = Items().array[index];
  if (tool) {
    if (item.GetString("type") == "apply_patch_call" ||
        item.GetString("type") == "shell_call") {
      if (!delta.tool_start)
        return Status::Fail("Unexpected fragment after validated client call");
      return {};
    }
    if (item.GetString("type") == "local_shell_call") {
      // The protocol has no event for partial argv. A completed call exposes
      // the parsed action only after the model has closed the tool payload.
      local_shell_arguments_[index] += added;
      return {};
    }
    const bool custom = item.GetString("type") == "custom_tool_call";
    Field(item, custom ? "input" : "arguments").str += added;
    if (added.empty()) return {};
    auto fields = Location(index);
    Set(&fields, "delta", String(added));
    Event(custom ? "response.custom_tool_call_input.delta"
                 : "response.function_call_arguments.delta",
          std::move(fields), wire);
  } else {
    auto& part = Field(item, "content").array[0];
    Field(part, refusal ? "refusal" : "text").str += added;
    auto fields = Location(index, true);
    Set(&fields, "delta", String(added));
    if (delta.kind == ResponseDelta::Kind::kContent) {
      auto& logs = Field(part, "logprobs").array;
      logs.insert(logs.end(), probabilities.array.begin(),
                  probabilities.array.end());
      Set(&fields, "logprobs", EventProbabilities(std::move(probabilities)));
    }
    Event(refusal ? "response.refusal.delta"
          : delta.kind == ResponseDelta::Kind::kContent
              ? "response.output_text.delta"
              : "response.reasoning_text.delta",
          std::move(fields), wire);
  }
  return {};
}

Status ResponsesOutput::AppendSummary(std::string_view text,
                                      std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || response_.GetString("status") != "in_progress")
    return Status::Fail("Responses summary is not in progress");
  if (text.empty()) return {};
  if (text.size() > limit_ / 6)
    return Status::Fail("Responses summary fragment exceeds storage limit");
  auto status = Charge(text.size() * 6 + sizeof(io::Json) * 8);
  if (!status) return status;
  if (summary_item_ == kMaxItems) {
    if (Items().array.size() >= kMaxItems)
      return Status::Fail("Responses output item capacity exhausted");
    auto item = Object({{"id", String(detail::NewRequestId("rs_"))},
                        {"type", String("reasoning")},
                        {"status", String("in_progress")},
                        {"summary", Array()}});
    status = Charge(OutputJson(item).size() + sizeof(io::Json) * 8);
    if (!status) return status;
    summary_item_ = Items().array.size();
    Items().array.push_back(std::move(item));
    call_indices_.push_back(kNoCall);
    Event("response.output_item.added",
          Object({{"output_index", Number(summary_item_)},
                  {"item", Items().array.back()}}),
          wire);
    auto part =
        Object({{"type", String("summary_text")}, {"text", String("")}});
    Field(Items().array.back(), "summary").array.push_back(part);
    auto fields = Location(summary_item_);
    Set(&fields, "summary_index", Number(0));
    Set(&fields, "part", std::move(part));
    Event("response.reasoning_summary_part.added", std::move(fields), wire);
  }
  auto& part = Field(Items().array[summary_item_], "summary").array[0];
  Field(part, "text").str.append(text);
  auto fields = Location(summary_item_);
  Set(&fields, "summary_index", Number(0));
  Set(&fields, "delta", String(std::string(text)));
  Event("response.reasoning_summary_text.delta", std::move(fields), wire);
  return {};
}

Status ResponsesOutput::StartCompaction(std::string id, std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || compaction_item_ != kMaxItems || id.empty() ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems)
    return Status::Fail("Invalid compaction start");
  auto item = Object({{"id", String(std::move(id))},
                      {"type", String("compaction")},
                      {"encrypted_content", String("")}});
  auto status = Charge(OutputJson(item).size() + sizeof(io::Json) * 8);
  if (!status) return status;
  compaction_item_ = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(kNoCall);
  Event("response.output_item.added",
        Object({{"output_index", Number(compaction_item_)},
                {"item", Items().array.back()}}),
        wire);
  Event("response.compaction.compacting", Location(compaction_item_), wire);
  return {};
}

Status ResponsesOutput::CompleteCompaction(const io::Json& item,
                                           std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || compaction_done_ ||
      compaction_item_ >= Items().array.size() ||
      item.GetString("type") != "compaction" || item.Has("status") ||
      item.GetString("encrypted_content").empty() ||
      item.GetString("id") != Items().array[compaction_item_].GetString("id"))
    return Status::Fail("Invalid compaction completion");
  auto status = Charge(OutputJson(item).size());
  if (!status) return status;
  Items().array[compaction_item_] = item;
  compaction_done_ = true;
  Event("response.output_item.done",
        Object({{"output_index", Number(compaction_item_)}, {"item", item}}),
        wire);
  return {};
}

void ResponsesOutput::CloseItem(size_t index, bool incomplete,
                                std::string* wire) {
  if (closed_items_.contains(index)) return;
  if (CloseInterruptedMcp(index, wire)) return;
  auto& item = Items().array[index];
  // Compaction has no status/arguments/content fields in the SDK contract.
  // Its done event was published when the authenticated window was ready.
  if (item.GetString("type") == "compaction") return;
  const auto type = item.GetString("type");
  if (type != "code_interpreter_call" || item.GetString("status") != "failed")
    Set(&item, "status", String(incomplete ? "incomplete" : "completed"));
  if (type == "message" || (type == "reasoning" && item.GetArray("content") &&
                            !item.GetArray("content")->array.empty())) {
    const auto& part = Field(item, "content").array[0];
    auto fields = Location(index, true);
    const bool refusal = part.GetString("type") == "refusal";
    Set(&fields, refusal ? "refusal" : "text",
        *part.Find(refusal ? "refusal" : "text"));
    if (type == "message" && !refusal)
      Set(&fields, "logprobs", EventProbabilities(*part.Find("logprobs")));
    Event(refusal             ? "response.refusal.done"
          : type == "message" ? "response.output_text.done"
                              : "response.reasoning_text.done",
          std::move(fields), wire);
    fields = Location(index, true);
    Set(&fields, "part", part);
    Event("response.content_part.done", std::move(fields), wire);
  } else if (type == "shell_call" && !incomplete) {
    const auto& commands = item.Find("action")->GetArray("commands")->array;
    for (size_t i = 0; i < commands.size(); ++i)
      Event("response.shell_call_command.done",
            Object({{"output_index", Number(index)},
                    {"command_index", Number(i)},
                    {"command", commands[i]}}),
            wire);
  } else if ((type == "function_call" || type == "custom_tool_call") &&
             !incomplete) {
    const bool custom = type == "custom_tool_call";
    auto fields = Location(index);
    Set(&fields, custom ? "input" : "arguments",
        *item.Find(custom ? "input" : "arguments"));
    Event(custom ? "response.custom_tool_call_input.done"
                 : "response.function_call_arguments.done",
          std::move(fields), wire);
  }
  if (type == "reasoning") {
    const auto& summary = Field(item, "summary").array;
    for (size_t i = 0; i < summary.size(); ++i) {
      auto fields = Location(index);
      Set(&fields, "summary_index", Number(i));
      Set(&fields, "text", *summary[i].Find("text"));
      Event("response.reasoning_summary_text.done", std::move(fields), wire);
      fields = Location(index);
      Set(&fields, "summary_index", Number(i));
      Set(&fields, "part", summary[i]);
      if (incomplete) Set(&fields, "status", String("incomplete"));
      Event("response.reasoning_summary_part.done", std::move(fields), wire);
    }
  }
  Event("response.output_item.done",
        Object({{"output_index", Number(index)}, {"item", item}}), wire);
  closed_items_.insert(index);
}

Status ResponsesOutput::CompleteAsyncCalls(size_t completed_calls,
                                           bool* continue_answer,
                                           std::string* wire) {
  wire->clear();
  *continue_answer = false;
  if (!started_ || terminal_ || response_.GetString("status") != "in_progress")
    return Status::Fail("Responses output is not in progress");
  size_t pending = 0;
  for (const auto& [call, index] : calls_) {
    if (closed_items_.contains(index)) continue;
    if (call >= completed_calls)
      return Status::Fail("cannot continue an unvalidated async tool");
    if (!Items().array[index].GetBool("async")) return {};
    ++pending;
  }
  if (!pending) return {};
  // Public item/event order follows output order, not unordered-map order.
  for (size_t i = 0; i < Items().array.size(); ++i)
    if (call_indices_[i] != kNoCall && !closed_items_.contains(i))
      CloseItem(i, false, wire);
  *continue_answer = true;
  return {};
}

Status ResponsesOutput::Moderation(const io::Json& results,
                                   const ModerationUsage& usage) {
  const auto* input = results.Find("input");
  const auto* output = results.Find("output");
  if (!initialized_ || terminal_ || !results.IsObject() ||
      results.object.size() != 2 || !input || !output ||
      !ValidModerationReport(*input) || !ValidModerationReport(*output) ||
      usage.input_tokens < 0 || usage.output_tokens < 0 ||
      usage.output_tokens > 2 * kMaxModerationStepTokens ||
      usage.input_tokens > INT64_MAX - usage.output_tokens ||
      usage.model_steps < 0 || usage.model_steps > 2)
    return Status::Fail("Invalid moderation metadata or actual usage");
  auto status = Charge(OutputJson(results).size() + 256);
  if (!status) return status;
  Set(&response_, "moderation", results);
  Set(&response_, "q4t_moderation_usage",
      Object(
          {{"input_tokens", Number(usage.input_tokens)},
           {"output_tokens", Number(usage.output_tokens)},
           {"total_tokens", Number(usage.input_tokens + usage.output_tokens)},
           {"model_steps", Number(usage.model_steps)}}));
  return {};
}

Status ResponsesOutput::SetUsage(const ResponsesUsage& usage) {
  if (!initialized_ || terminal_ || usage.input_tokens < 0 ||
      usage.output_tokens < 0 || usage.cached_tokens < 0 ||
      usage.cached_tokens > usage.input_tokens ||
      usage.cache_write_tokens < 0 ||
      usage.cache_write_tokens > usage.input_tokens - usage.cached_tokens ||
      usage.reasoning_tokens < 0 ||
      usage.reasoning_tokens > usage.output_tokens ||
      usage.input_tokens > INT64_MAX - usage.output_tokens)
    return Status::Fail("invalid Responses actual usage");
  Set(&response_, "usage",
      Object(
          {{"input_tokens", Number(usage.input_tokens)},
           {"input_tokens_details",
            Object({{"cached_tokens", Number(usage.cached_tokens)},
                    {"cache_write_tokens", Number(usage.cache_write_tokens)}})},
           {"output_tokens", Number(usage.output_tokens)},
           {"output_tokens_details",
            Object({{"reasoning_tokens", Number(usage.reasoning_tokens)}})},
           {"total_tokens",
            Number(usage.input_tokens + usage.output_tokens)}}));
  return {};
}

Status ResponsesOutput::Finish(bool incomplete, size_t completed_calls,
                               const ResponsesUsage& usage,
                               int64_t completed_at, std::string* wire,
                               const ResponsesReasoningSeal& seal,
                               bool content_filtered,
                               bool assistant_prompt_exhausted) {
  wire->clear();
  if (!started_ || terminal_ || (content_filtered && !incomplete) ||
      (assistant_prompt_exhausted && !incomplete) ||
      (compaction_item_ != kMaxItems && !compaction_done_) ||
      response_.GetString("status") != "in_progress" ||
      usage.input_tokens < 0 || usage.output_tokens < 0 ||
      usage.cached_tokens < 0 || usage.cached_tokens > usage.input_tokens ||
      usage.cache_write_tokens < 0 ||
      usage.cache_write_tokens > usage.input_tokens ||
      usage.cache_write_tokens > usage.input_tokens - usage.cached_tokens ||
      usage.reasoning_tokens < 0 ||
      usage.reasoning_tokens > usage.output_tokens ||
      usage.input_tokens > INT64_MAX - usage.output_tokens || completed_at < 0)
    return Status::Fail("invalid Responses terminal or usage");
  if (!incomplete)
    for (const auto& [call, index] : calls_) {
      if (!closed_items_.contains(index) && call >= completed_calls)
        return Status::Fail("cannot complete an unvalidated Responses tool");
    }
  for (const auto& [index, arguments] : local_shell_arguments_) {
    if (call_indices_[index] >= completed_calls) continue;
    io::Json action;
    if (!io::ParseJson(arguments, &action, true, kSchemaJsonLimits) ||
        !action.IsObject())
      return Status::Fail("Invalid generated local shell action");
    if (!action.Has("type")) Set(&action, "type", String("exec"));
    if (!action.Has("env")) Set(&action, "env", Object());
    const auto* argv = action.GetArray("command");
    if (!argv || argv->array.empty() || !argv->array.front().IsString() ||
        argv->array.front().str.empty())
      return Status::Fail("Generated local shell call requires an executable");
    auto& item = Items().array[index];
    Set(&item, "action", std::move(action));
    ChatPlanError error;
    auto valid = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                            &error, "item");
    if (!valid) return valid;
  }
  if (!incomplete)
    for (size_t i = 0; i < Items().array.size(); ++i)
      if ((Items().array[i].GetString("type") == "file_search_call" ||
           Items().array[i].GetString("type") == "tool_search_call" ||
           Items().array[i].GetString("type") == "code_interpreter_call" ||
           Items().array[i].GetString("type") == "web_search_call" ||
           Items().array[i].GetString("type") == "mcp_call" ||
           Items().array[i].GetString("type") == "mcp_list_tools") &&
          !closed_items_.contains(i))
        return Status::Fail("Cannot finish an unexecuted native tool");
  for (size_t i = 0; i < Items().array.size(); ++i) {
    if (Items().array[i].GetString("type") == "compaction" ||
        Items().array[i].GetString("type") == "mcp_list_tools" ||
        closed_items_.contains(i))
      continue;
    const bool partial =
        incomplete &&
        (Items().array[i].GetString("type") == "file_search_call" ||
         Items().array[i].GetString("type") == "tool_search_call" ||
         Items().array[i].GetString("type") == "code_interpreter_call" ||
         Items().array[i].GetString("type") == "web_search_call" ||
         Items().array[i].GetString("type") == "mcp_call" ||
         (call_indices_[i] != kNoCall ? call_indices_[i] >= completed_calls
                                      : i + 1 == Items().array.size()));
    if (Items().array[i].GetString("type") == "apply_patch_call" &&
        call_indices_[i] >= completed_calls) {
      if (!incomplete) return Status::Fail("Patch call was not confirmed");
      continue;
    }
    Set(&Items().array[i], "status",
        String(partial ? "incomplete" : "completed"));
  }
  // Encryption can fail; no done/terminal events escape before all required
  // state has actually been sealed. The execution owner works on a candidate.
  if (seal)
    for (size_t i = 0; i < Items().array.size(); ++i) {
      if (Items().array[i].GetString("type") != "reasoning") continue;
      std::string opaque;
      auto status = seal(i, &opaque);
      if (status) status = Charge(opaque.size() + sizeof(io::Json));
      if (!status) return status;
      Set(&Items().array[i], "encrypted_content", String(std::move(opaque)));
    }
  for (size_t i = 0; i < Items().array.size(); ++i) {
    if (Items().array[i].GetString("type") == "apply_patch_call" &&
        Items().array[i].GetString("status") == "in_progress")
      continue;
    CloseItem(i, Items().array[i].GetString("status") == "incomplete", wire);
  }
  Set(&response_, "status", String(incomplete ? "incomplete" : "completed"));
  if (assistant_prompt_exhausted)
    Set(&response_, "incomplete_details", Object());
  else if (incomplete)
    Set(&response_, "incomplete_details",
        Object({{"reason", String(content_filtered ? "content_filter"
                                                   : "max_output_tokens")}}));
  else
    Set(&response_, "completed_at", Number(completed_at));
  const auto usage_status = SetUsage(usage);
  if (!usage_status) return usage_status;
  Event(incomplete ? "response.incomplete" : "response.completed",
        Object({{"response", response_}}), wire);
  terminal_ = true;
  return {};
}

Status ResponsesOutput::Fail(std::string_view code, std::string_view message,
                             std::string* wire) {
  wire->clear();
  if (!started_ || terminal_)
    return Status::Fail("Responses failure outside active lifecycle");
  // A patch has only in_progress/completed states. Failure terminates the
  // response without confirming an open client operation.
  for (size_t i = 0; i < Items().array.size(); ++i) {
    auto& item = Items().array[i];
    const auto type = item.GetString("type");
    if (closed_items_.contains(i) || type == "compaction" ||
        type == "apply_patch_call")
      continue;
    if (type == "mcp_list_tools")
      Set(&item, "error", String("MCP listing interrupted"));
    else
      Set(&item, "status", String("incomplete"));
  }
  Set(&response_, "status", String("failed"));
  Set(&response_, "error",
      Object({{"code", String(std::string(code))},
              {"message", String(std::string(message))}}));
  Event("response.failed", Object({{"response", response_}}), wire);
  terminal_ = true;
  return {};
}

std::string ResponsesOutput::Json() const {
  return OutputJson(ResponsesWireValue(response_));
}

std::string ResponsesOutput::RenderStreamError(void* owner, int status,
                                               const std::string& message,
                                               std::string_view code,
                                               std::string_view param) {
  auto& output = *static_cast<ResponsesOutput*>(owner);
  if (output.terminal_) return {};
  if (code.empty()) code = status >= 500 ? "server_error" : "invalid_request";
  std::string wire;
  if (output.started_) {
    const auto result = output.Fail(code, message, &wire);
    if (result) return wire;
  }
  // A fault can occur after HTTP headers but before response.created. This is
  // a protocol error event, not a fabricated complete response snapshot.
  output.Event("error",
               Object({{"code", String(std::string(code))},
                       {"message", String(message)},
                       {"param", param.empty() ? io::Json()
                                               : String(std::string(param))}}),
               &wire);
  output.terminal_ = true;
  return wire;
}
}  // namespace q4t::server
