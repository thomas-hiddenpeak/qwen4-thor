#include "q4t/server/realtime_trace_session.h"

#include <chrono>
#include <limits>

namespace q4t::server {
RealtimeTraceTime RealtimeTraceNow() noexcept {
  using namespace std::chrono;
  return {duration_cast<milliseconds>(system_clock::now().time_since_epoch())
              .count(),
          duration_cast<nanoseconds>(steady_clock::now().time_since_epoch())
              .count()};
}
RealtimeSessionTrace::RealtimeSessionTrace(
    std::string session, std::string conversation,
    std::function<RealtimeTraceStore*()> store, std::string scope)
    : session_(std::move(session)),
      conversation_(std::move(conversation)),
      scope_(std::move(scope)),
      store_(std::move(store)) {}
void RealtimeSessionTrace::Configure(const io::Json& settings,
                                     RealtimeTraceTime now) noexcept {
  if (closed_ || collector_) return;
  try {
    RealtimeTraceConfiguration configuration;
    const auto parsed = ParseRealtimeTraceConfiguration(
        settings.Find("tracing"), &configuration);
    if (!parsed.ok() || !configuration.enabled) return;
    collector_.emplace(session_, conversation_, std::move(configuration), now);
    Publish(now, true);
  } catch (...) {
    if (store_) {
      try {
        if (auto* store = store_()) store->RecordCollectionFailure();
      } catch (...) {
      }
    }
  }
}
void RealtimeSessionTrace::Observe(const io::Json& event,
                                   RealtimeTraceTime now) noexcept {
  if (closed_ || !collector_) return;
  ObserveRealtimeTraceEvent(&*collector_, event, now);
  // Text/audio deltas never allocate a new snapshot. Lifecycle changes are
  // exported on the next bounded tick; session close requests a final flush.
}
void RealtimeSessionTrace::Tick(RealtimeTraceTime now) noexcept {
  Publish(now, false);
}
void RealtimeSessionTrace::Close(std::string_view reason,
                                 RealtimeTraceTime now) noexcept {
  if (closed_) {
    Publish(now, true);
    return;
  }
  closed_ = true;
  if (collector_) collector_->Close(reason, now);
  Publish(now, true);
}
void RealtimeSessionTrace::Publish(RealtimeTraceTime now, bool force) noexcept {
  if (!collector_ || !store_ || (producer_ && producer_->Revoked()) ||
      (!force && now.monotonic_ns < next_publish_ns_) ||
      collector_->Revision() == published_revision_)
    return;
  constexpr auto maximum = std::numeric_limits<int64_t>::max();
  next_publish_ns_ = now.monotonic_ns > maximum - 250000000
                         ? maximum
                         : now.monotonic_ns + 250000000;
  try {
    auto* store = store_();
    if (!store) return;
    if (!producer_) producer_ = store->Open(scope_, session_);
    if (!producer_) return;
    if (store->Publish(producer_, collector_->Snapshot()))
      published_revision_ = collector_->Revision();
  } catch (...) {
    collector_->ObserverError();
    try {
      if (auto* store = store_()) store->RecordCollectionFailure();
    } catch (...) {
    }
  }
}
}  // namespace q4t::server
