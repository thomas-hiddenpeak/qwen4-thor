// Native bounded lifecycle tracing. The protocol owner never performs I/O.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/object_store.h"

namespace q4t::server {

struct RealtimeTraceConfiguration {
  bool enabled = false;
  std::string workflow_name, group_id;
  io::Json metadata;
};

// A null/missing value disables tracing. Unknown fields and resource overflow
// are errors; caller-owned metadata must not silently disappear or truncate.
ObjectReply ParseRealtimeTraceConfiguration(
    const io::Json* value, RealtimeTraceConfiguration* configuration);
bool SameRealtimeTraceConfiguration(const RealtimeTraceConfiguration& left,
                                    const RealtimeTraceConfiguration& right);

struct RealtimeTraceTime {
  int64_t unix_ms = 0;
  int64_t monotonic_ns = 0;
};

// One session-thread owner. This component never performs I/O, invokes a
// callback, changes request state, or retains event/prompt/tool payloads.
class RealtimeTraceCollector {
 public:
  static constexpr size_t kMaximumSpans = 256;
  static constexpr size_t kMaximumMetadataBytes = 64ULL << 10;
  // Counts the expanded Json tree and capacities, including every numeric
  // usage leaf; the serialized wire representation is substantially smaller.
  static constexpr size_t kMaximumRecordBytes = 4ULL << 20;
  RealtimeTraceCollector(std::string session_id, std::string conversation_id,
                         RealtimeTraceConfiguration configuration,
                         RealtimeTraceTime started,
                         size_t maximum_spans = kMaximumSpans);
  bool Valid() const { return valid_; }
  bool Begin(std::string_view kind, std::string_view entity_id,
             std::string_view parent_entity, RealtimeTraceTime now) noexcept;
  bool End(std::string_view kind, std::string_view entity_id,
           std::string_view status, RealtimeTraceTime now,
           const io::Json* usage = nullptr) noexcept;
  bool Phase(std::string_view kind, std::string_view entity_id,
             std::string_view phase, RealtimeTraceTime now) noexcept;
  bool LinkApproval(std::string_view entity_id,
                    std::string_view approval_id) noexcept;
  void EventSeen() noexcept;
  void ObserverError() noexcept {
    if (valid_ && !closed_) Fault();
  }
  void Close(std::string_view reason, RealtimeTraceTime now) noexcept;
  // The sink handles allocation failures independently from healthy inference.
  io::Json Snapshot() const;
  uint64_t Revision() const { return revision_; }

 private:
  struct Span {
    std::string id, kind, entity_id, parent_entity, status, phase, approval_id;
    RealtimeTraceTime begin, end, dispatched;
    bool partial = false, terminal = false, was_dispatched = false;
    io::Json usage;
  };
  Span* Find(std::string_view kind, std::string_view entity);
  bool New(std::string_view kind, std::string_view entity,
           std::string_view parent, RealtimeTraceTime now, bool partial);
  void Fault() noexcept;
  const std::string session_id_, conversation_id_;
  const RealtimeTraceConfiguration configuration_;
  const RealtimeTraceTime started_;
  const size_t maximum_spans_;
  std::vector<Span> spans_;
  RealtimeTraceTime ended_;
  std::string close_reason_;
  uint64_t revision_ = 1, seen_ = 0, dropped_ = 0, faults_ = 0;
  bool valid_ = false, closed_ = false;
};
// Projects only lifecycle and numeric usage; never retains delta payloads.
bool ObserveRealtimeTraceEvent(RealtimeTraceCollector* collector,
                               const io::Json& event,
                               RealtimeTraceTime now) noexcept;
}  // namespace q4t::server
