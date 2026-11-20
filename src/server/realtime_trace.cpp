#include "q4t/server/realtime_trace.h"

#include <algorithm>
#include <cmath>
#include <limits>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr uint64_t kMaximumCount = (1ULL << 53) - 1;
void Increment(uint64_t* value) noexcept {
  if (*value < kMaximumCount) ++*value;
}
ObjectReply Invalid(std::string message, std::string field) {
  auto result = Error(400, "realtime_tracing_invalid", std::move(message));
  result.value = Object({{"param", String("session.tracing" + field)}});
  return result;
}
bool SameJson(const Json& a, const Json& b) {
  if (a.type != b.type) return false;
  if (a.IsString()) return a.str == b.str;
  if (a.IsNumber()) {
    if (!a.str.empty() || !b.str.empty()) return a.str == b.str;
    return a.number == b.number;
  }
  if (a.IsBool()) return a.boolean == b.boolean;
  if (a.IsArray()) {
    if (a.array.size() != b.array.size()) return false;
    for (size_t i = 0; i < a.array.size(); ++i)
      if (!SameJson(a.array[i], b.array[i])) return false;
  }
  if (a.IsObject()) {
    if (a.object.size() != b.object.size()) return false;
    for (const auto& [key, child] : a.object) {
      const auto* other = b.Find(key);
      if (!other || !SameJson(child, *other)) return false;
    }
  }
  return true;
}
bool TimeValid(RealtimeTraceTime value) {
  return value.unix_ms > 0 && value.unix_ms <= int64_t(kMaximumCount) &&
         value.monotonic_ns >= 0;
}
bool Kind(std::string_view kind) {
  return kind == "response" || kind == "mcp_import" || kind == "mcp_call" ||
         kind == "mcp_approval" || kind == "function_arguments" ||
         kind == "input_transcription" || kind == "output_audio";
}
bool Terminal(std::string_view status) {
  return status == "completed" || status == "failed" || status == "cancelled" ||
         status == "incomplete" || status == "interrupted";
}
Json SafeUsage(const Json* source) {
  auto result = Object();
  if (!source || !source->IsObject()) return result;
  for (const auto* key : {"input_tokens", "output_tokens", "total_tokens"}) {
    const auto* value = source->Find(key);
    if (value && value->IsNumber() && value->AsInt(-1) >= 0 &&
        value->AsInt(-1) <= int64_t(kMaximumCount))
      Set(&result, key, Number(value->AsInt()));
  }
  for (const auto* key : {"input_token_details", "output_token_details"}) {
    const auto* value = source->Find(key);
    if (!value || !value->IsObject()) continue;
    auto details = Object();
    for (const auto* field :
         {"text_tokens", "audio_tokens", "cached_tokens", "image_tokens"}) {
      const auto* count = value->Find(field);
      if (count && count->IsNumber() && count->AsInt(-1) >= 0 &&
          count->AsInt(-1) <= int64_t(kMaximumCount))
        Set(&details, field, Number(count->AsInt()));
    }
    Set(&result, key, std::move(details));
  }
  return result;
}
Json Duration(RealtimeTraceTime begin, RealtimeTraceTime end) {
  if (end.monotonic_ns < begin.monotonic_ns) return Json();
  Json result;
  result.type = Json::Type::kNumber;
  result.number = double(end.monotonic_ns - begin.monotonic_ns) / 1e6;
  return result;
}
}  // namespace

ObjectReply ParseRealtimeTraceConfiguration(
    const Json* value, RealtimeTraceConfiguration* configuration) {
  if (!configuration)
    return Error(500, "realtime_tracing_owner_missing",
                 "Tracing owner missing");
  RealtimeTraceConfiguration next;
  if (!value || value->IsNull()) {
    *configuration = std::move(next);
    return {};
  }
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(
          *value, RealtimeTraceCollector::kMaximumMetadataBytes, &bytes)) {
    auto reply = Error(413, "realtime_tracing_capacity",
                       "Tracing configuration exceeds its storage limit");
    reply.value = Object({{"param", String("session.tracing")}});
    return reply;
  }
  next.enabled = true;
  next.workflow_name = "Realtime session";
  next.metadata = Object();
  if (value->IsString()) {
    if (value->str != "auto")
      return Invalid("Expected auto, object or null", "");
  } else if (value->IsObject()) {
    std::vector<std::string_view> names;
    for (const auto& [key, child] : value->object) {
      if ((key != "workflow_name" && key != "group_id" && key != "metadata") ||
          std::ranges::find(names, key) != names.end())
        return Invalid("Unknown or duplicate tracing field", "." + key);
      names.push_back(key);
      if (key == "metadata") {
        next.metadata = child;
      } else {
        if (!child.IsString() || child.str.size() > 256)
          return Invalid("Expected a string of at most 256 bytes", "." + key);
        (key == "workflow_name" ? next.workflow_name : next.group_id) =
            child.str;
      }
    }
  } else {
    return Invalid("Expected auto, object or null", "");
  }
  *configuration = std::move(next);
  return {};
}

bool SameRealtimeTraceConfiguration(const RealtimeTraceConfiguration& a,
                                    const RealtimeTraceConfiguration& b) {
  return a.enabled == b.enabled && a.workflow_name == b.workflow_name &&
         a.group_id == b.group_id && SameJson(a.metadata, b.metadata);
}

RealtimeTraceCollector::RealtimeTraceCollector(
    std::string session_id, std::string conversation_id,
    RealtimeTraceConfiguration configuration, RealtimeTraceTime started,
    size_t maximum_spans)
    : session_id_(std::move(session_id)),
      conversation_id_(std::move(conversation_id)),
      configuration_(std::move(configuration)),
      started_(started),
      maximum_spans_(maximum_spans) {
  size_t bytes = 0;
  valid_ = configuration_.enabled && ValidRequestId(session_id_) &&
           ValidRequestId(conversation_id_) && TimeValid(started_) &&
           maximum_spans_ && maximum_spans_ <= kMaximumSpans &&
           configuration_.workflow_name.capacity() <= kMaximumMetadataBytes &&
           configuration_.group_id.capacity() <= kMaximumMetadataBytes &&
           MeasureRealtimeStorage(configuration_.metadata,
                                  kMaximumMetadataBytes, &bytes);
}

RealtimeTraceCollector::Span* RealtimeTraceCollector::Find(
    std::string_view kind, std::string_view entity) {
  const auto found = std::ranges::find_if(spans_, [&](const Span& span) {
    return span.kind == kind && span.entity_id == entity;
  });
  return found == spans_.end() ? nullptr : &*found;
}
void RealtimeTraceCollector::Fault() noexcept {
  Increment(&faults_);
  Increment(&revision_);
}
bool RealtimeTraceCollector::New(std::string_view kind, std::string_view entity,
                                 std::string_view parent, RealtimeTraceTime now,
                                 bool partial) {
  if (!valid_ || closed_ || !TimeValid(now) || !Kind(kind) || entity.empty() ||
      entity.size() > 256 || parent.size() > 256)
    return false;
  if (Find(kind, entity)) return false;
  if (spans_.size() == maximum_spans_) {
    Increment(&dropped_);
    Increment(&revision_);
    return false;
  }
  Span span;
  span.id = "span_" + std::to_string(spans_.size() + 1);
  span.kind = kind;
  span.entity_id = entity;
  span.parent_entity = parent;
  span.status = "in_progress";
  span.begin = now;
  span.partial = partial;
  spans_.push_back(std::move(span));
  Increment(&revision_);
  return true;
}
bool RealtimeTraceCollector::Begin(std::string_view kind,
                                   std::string_view entity_id,
                                   std::string_view parent,
                                   RealtimeTraceTime now) noexcept {
  try {
    return New(kind, entity_id, parent, now, false);
  } catch (...) {
    Fault();
    return false;
  }
}
bool RealtimeTraceCollector::End(std::string_view kind,
                                 std::string_view entity_id,
                                 std::string_view status, RealtimeTraceTime now,
                                 const Json* usage) noexcept {
  try {
    if (!valid_ || closed_ || !Terminal(status) || !TimeValid(now))
      return false;
    auto* span = Find(kind, entity_id);
    if (!span) {
      if (!New(kind, entity_id, {}, now, true)) return false;
      span = &spans_.back();
    }
    if (span->terminal) return false;
    // Prepare allocations before publishing the terminal transition.
    auto captured = SafeUsage(usage);
    auto final = std::string(status);
    span->usage = std::move(captured);
    span->status = std::move(final);
    span->end = now;
    span->terminal = true;
    Increment(&revision_);
    return true;
  } catch (...) {
    Fault();
    return false;
  }
}
bool RealtimeTraceCollector::Phase(std::string_view kind,
                                   std::string_view entity_id,
                                   std::string_view phase,
                                   RealtimeTraceTime now) noexcept {
  try {
    if (!valid_ || closed_ || !TimeValid(now) ||
        (phase != "arguments_ready" && phase != "awaiting_approval" &&
         phase != "dispatched"))
      return false;
    auto* span = Find(kind, entity_id);
    if (!span || span->terminal || span->phase == phase) return false;
    if (span->was_dispatched && phase != "dispatched") return false;
    span->phase = phase;
    if (phase == "dispatched") {
      span->was_dispatched = true;
      span->dispatched = now;
    }
    Increment(&revision_);
    return true;
  } catch (...) {
    Fault();
    return false;
  }
}
void RealtimeTraceCollector::EventSeen() noexcept {
  if (!closed_) Increment(&seen_);
}
bool RealtimeTraceCollector::LinkApproval(
    std::string_view entity_id, std::string_view approval_id) noexcept {
  try {
    if (!valid_ || closed_ || approval_id.empty() || approval_id.size() > 256)
      return false;
    auto* span = Find("mcp_call", entity_id);
    if (!span || !span->approval_id.empty()) return false;
    span->approval_id = approval_id;
    Increment(&revision_);
    return true;
  } catch (...) {
    Fault();
    return false;
  }
}
void RealtimeTraceCollector::Close(std::string_view reason,
                                   RealtimeTraceTime now) noexcept {
  try {
    if (!valid_ || closed_ || !TimeValid(now)) return;
    if (reason != "session_closed" && reason != "client_disconnected" &&
        reason != "service_stopping" && reason != "session_failed")
      reason = "session_ended";
    close_reason_ = reason;
    for (auto& span : spans_)
      if (!span.terminal) {
        span.status = "interrupted";
        span.end = now;
        span.terminal = true;
      }
    ended_ = now;
    closed_ = true;
    Increment(&revision_);
  } catch (...) {
    Fault();
  }
}
Json RealtimeTraceCollector::Snapshot() const {
  if (!valid_) return Json();
  auto spans = Array();
  for (const auto& span : spans_) {
    std::string parent_id;
    for (const auto& parent : spans_)
      if (parent.kind == "response" && parent.entity_id == span.parent_entity)
        parent_id = parent.id;
    auto value = Object(
        {{"id", String(span.id)},
         {"kind", String(span.kind)},
         {"entity_id", String(span.entity_id)},
         {"parent_span_id", parent_id.empty() ? Json() : String(parent_id)},
         {"parent_entity_id",
          span.parent_entity.empty() ? Json() : String(span.parent_entity)},
         {"status", String(span.status)},
         {"phase", String(span.phase)},
         {"approval_request_id",
          span.approval_id.empty() ? Json() : String(span.approval_id)},
         {"partial", Boolean(span.partial)},
         {"started_at_ms", span.partial ? Json() : Number(span.begin.unix_ms)},
         {"ended_at_ms", span.terminal ? Number(span.end.unix_ms) : Json()},
         {"duration_ms", span.terminal && !span.partial
                             ? Duration(span.begin, span.end)
                             : Json()},
         {"dispatched_at_ms",
          span.was_dispatched ? Number(span.dispatched.unix_ms) : Json()},
         {"usage", span.usage}});
    spans.array.push_back(std::move(value));
  }
  auto value =
      Object({{"object", String("realtime.trace")},
              {"observation_boundary", String("native_protocol_owner")},
              {"version", Number(1)},
              {"id", String("trace_" + Digest(session_id_))},
              {"session_id", String(session_id_)},
              {"conversation_id", String(conversation_id_)},
              {"workflow_name", String(configuration_.workflow_name)},
              {"group_id", String(configuration_.group_id.empty()
                                      ? session_id_
                                      : configuration_.group_id)},
              {"metadata", configuration_.metadata},
              {"status", String(closed_ ? "closed" : "in_progress")},
              {"close_reason", closed_ ? String(close_reason_) : Json()},
              {"started_at_ms", Number(started_.unix_ms)},
              {"ended_at_ms", closed_ ? Number(ended_.unix_ms) : Json()},
              {"duration_ms", closed_ ? Duration(started_, ended_) : Json()},
              {"revision", Number(revision_)},
              {"events_seen", Number(seen_)},
              {"dropped_span_events", Number(dropped_)},
              {"collector_errors", Number(faults_)},
              {"spans", std::move(spans)}});
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(value, kMaximumRecordBytes, &bytes))
    return Json();
  return value;
}
}  // namespace q4t::server
