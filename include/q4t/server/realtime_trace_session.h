// Auxiliary observation owned by one Realtime session thread.
#pragma once

#include <optional>

#include "q4t/server/realtime_trace.h"
#include "q4t/server/realtime_trace_store.h"

namespace q4t::server {
RealtimeTraceTime RealtimeTraceNow() noexcept;
class RealtimeSessionTrace {
 public:
  RealtimeSessionTrace(std::string session, std::string conversation,
                       std::function<RealtimeTraceStore*()> store,
                       std::string scope);
  // Configuration has already passed the session's atomic settings contract.
  // Allocation, queue and storage errors are observations, never LLM errors.
  void Configure(const io::Json& settings, RealtimeTraceTime now) noexcept;
  void Observe(const io::Json& event, RealtimeTraceTime now) noexcept;
  void Tick(RealtimeTraceTime now) noexcept;
  void Close(std::string_view reason, RealtimeTraceTime now) noexcept;
  bool Enabled() const { return collector_.has_value(); }

 private:
  void Publish(RealtimeTraceTime now, bool force) noexcept;
  const std::string session_, conversation_, scope_;
  const std::function<RealtimeTraceStore*()> store_;
  std::optional<RealtimeTraceCollector> collector_;
  RealtimeTraceStore::Producer producer_;
  int64_t next_publish_ns_ = 0;
  uint64_t published_revision_ = 0;
  bool closed_ = false;
};
}  // namespace q4t::server
