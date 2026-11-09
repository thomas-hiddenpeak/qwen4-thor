#include "q4t/server/responses_output.h"

#include <cmath>
#include <limits>

#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr size_t kNoCall = std::numeric_limits<size_t>::max();
Status Probabilities(std::string_view json, io::Json* out) {
  *out = Array();
  if (json.empty() || json == "null") return {};
  io::Json parsed;
  auto status =
      io::ParseJson(std::string(json), &parsed, true, kRequestJsonLimits);
  const auto* content = parsed.GetArray("content");
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
}  // namespace

Status ResponsesOutput::Init(const io::Json& options, std::string id,
                             int64_t created, bool obfuscation, size_t limit) {
  if (initialized_ || !options.IsObject() || id.empty() || created < 0 ||
      !limit || limit > kStorageLimit)
    return Status::Fail("invalid Responses output initialization");
  limit_ = limit;
  obfuscation_ = obfuscation;
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
                          "access_programs"}) {
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

void ResponsesOutput::Event(std::string_view type, io::Json fields,
                            std::string* wire) {
  Set(&fields, "type", String(std::string(type)));
  Set(&fields, "sequence_number", Number(sequence_++));
  // The public stream option pads delta events, not lifecycle snapshots.
  if (obfuscation_ && type.ends_with(".delta")) {
    const auto random = detail::NewRequestId("");
    const size_t size = 128 - ((OutputJson(fields).size() + 18) % 128) +
                        ((random[0] & 1) ? 128 : 0);
    std::string padding;
    while (padding.size() < size) padding += detail::NewRequestId("");
    padding.resize(size);
    Set(&fields, "obfuscation", String(std::move(padding)));
  }
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
  terminal_ = true;
  return {};
}

Status ResponsesOutput::AddItem(ResponseDelta::Kind kind,
                                const ResponseToolCall* call, size_t* index,
                                std::string* wire) {
  if (Items().array.size() >= kMaxItems)
    return Status::Fail("Responses output item capacity exhausted");
  io::Json item;
  if (kind == ResponseDelta::Kind::kTool) {
    item = Object(
        {{"id", String(detail::NewRequestId(call->custom ? "ctc_" : "fc_"))},
         {"type", String(call->custom ? "custom_tool_call" : "function_call")},
         {"call_id", String(call->id)},
         {"name", String(call->name)},
         {call->custom ? "input" : "arguments", String("")}});
  } else {
    item = Object(
        {{"id", String(detail::NewRequestId(
                    kind == ResponseDelta::Kind::kContent ? "msg_" : "rs_"))},
         {"type", String(kind == ResponseDelta::Kind::kContent ? "message"
                                                               : "reasoning")},
         {"content", Array()}});
    if (kind == ResponseDelta::Kind::kContent)
      Set(&item, "role", String("assistant"));
    else
      Set(&item, "summary", Array());
  }
  Set(&item, "status", String("in_progress"));
  auto status = Charge(OutputJson(item).size() + sizeof(io::Json) * 8);
  if (!status) return status;
  *index = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(call ? call->index : kNoCall);
  Event("response.output_item.added",
        Object(
            {{"output_index", Number(*index)}, {"item", Items().array.back()}}),
        wire);
  if (kind != ResponseDelta::Kind::kTool) {
    auto part = Object({{"type", String(kind == ResponseDelta::Kind::kContent
                                            ? "output_text"
                                            : "reasoning_text")},
                        {"text", String("")}});
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
  if (!tool && delta.text.empty()) return {};
  io::Json probabilities;
  auto status = Probabilities(chat_logprobs, &probabilities);
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
    } else {
      if (it == calls_.end())
        return Status::Fail("Responses tool fragment without a start");
      index = it->second;
      const auto& item = Items().array[index];
      if ((!delta.call.id.empty() &&
           delta.call.id != item.GetString("call_id")) ||
          (!delta.call.name.empty() &&
           delta.call.name != item.GetString("name")) ||
          delta.call.custom != (item.GetString("type") == "custom_tool_call"))
        return Status::Fail("Responses tool identity changed within stream");
    }
  } else {
    const auto type =
        delta.kind == ResponseDelta::Kind::kContent ? "message" : "reasoning";
    if (Items().array.empty() ||
        Items().array.back().GetString("type") != type) {
      status = AddItem(delta.kind, nullptr, &index, wire);
      if (!status) return status;
    } else {
      index = Items().array.size() - 1;
    }
  }
  auto& item = Items().array[index];
  if (tool) {
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
    Field(part, "text").str += added;
    auto fields = Location(index, true);
    Set(&fields, "delta", String(added));
    if (delta.kind == ResponseDelta::Kind::kContent) {
      auto& logs = Field(part, "logprobs").array;
      logs.insert(logs.end(), probabilities.array.begin(),
                  probabilities.array.end());
      Set(&fields, "logprobs", EventProbabilities(std::move(probabilities)));
    }
    Event(delta.kind == ResponseDelta::Kind::kContent
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
  auto& item = Items().array[index];
  // Compaction has no status/arguments/content fields in the SDK contract.
  // Its done event was published when the authenticated window was ready.
  if (item.GetString("type") == "compaction") return;
  Set(&item, "status", String(incomplete ? "incomplete" : "completed"));
  const auto type = item.GetString("type");
  if (type == "message" || (type == "reasoning" && item.GetArray("content") &&
                            !item.GetArray("content")->array.empty())) {
    const auto& part = Field(item, "content").array[0];
    auto fields = Location(index, true);
    Set(&fields, "text", *part.Find("text"));
    if (type == "message")
      Set(&fields, "logprobs", EventProbabilities(*part.Find("logprobs")));
    Event(type == "message" ? "response.output_text.done"
                            : "response.reasoning_text.done",
          std::move(fields), wire);
    fields = Location(index, true);
    Set(&fields, "part", part);
    Event("response.content_part.done", std::move(fields), wire);
  } else if (type != "reasoning" && !incomplete) {
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
}

Status ResponsesOutput::Finish(bool incomplete, size_t completed_calls,
                               const ResponsesUsage& usage,
                               int64_t completed_at, std::string* wire,
                               const ResponsesReasoningSeal& seal) {
  wire->clear();
  if (!started_ || terminal_ ||
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
      (void)index;
      if (call >= completed_calls)
        return Status::Fail("cannot complete an unvalidated Responses tool");
    }
  for (size_t i = 0; i < Items().array.size(); ++i) {
    if (Items().array[i].GetString("type") == "compaction") continue;
    const bool partial =
        incomplete &&
        (call_indices_[i] != kNoCall ? call_indices_[i] >= completed_calls
                                     : i + 1 == Items().array.size());
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
    CloseItem(i, Items().array[i].GetString("status") == "incomplete", wire);
  }
  Set(&response_, "status", String(incomplete ? "incomplete" : "completed"));
  if (incomplete)
    Set(&response_, "incomplete_details",
        Object({{"reason", String("max_output_tokens")}}));
  else
    Set(&response_, "completed_at", Number(completed_at));
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
  // No item/argument done events on a failed response.
  for (auto& item : Items().array)
    if (item.GetString("type") != "compaction")
      Set(&item, "status", String("incomplete"));
  Set(&response_, "status", String("failed"));
  Set(&response_, "error",
      Object({{"code", String(std::string(code))},
              {"message", String(std::string(message))}}));
  Event("response.failed", Object({{"response", response_}}), wire);
  terminal_ = true;
  return {};
}

std::string ResponsesOutput::Json() const { return OutputJson(response_); }

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
