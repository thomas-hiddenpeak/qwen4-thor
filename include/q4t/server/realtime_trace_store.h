// Native auxiliary persistence. A trace failure never owns model admission.
#pragma once

#include <atomic>
#include <memory>

#include "q4t/server/object_store.h"

namespace q4t::server {
struct RealtimeTraceStoreOptions {
  ObjectStoreOptions objects;
  size_t max_producers = 64;
  size_t max_client_producers = 8;
  size_t max_queue = 32;
  size_t max_queue_bytes = 32ULL << 20;
};
RealtimeTraceStoreOptions ResolveRealtimeTraceStoreOptions(
    std::string application_directory);

// Only Open() creates a producer. The session keeps this handle even when an
// HTTP deletion revokes it; reconnecting requires a different session ID.
class RealtimeTraceProducer {
 public:
  bool Revoked() const { return revoked_.load(); }
  const std::string& Id() const { return id_; }

 private:
  friend class RealtimeTraceStore;
  RealtimeTraceProducer(std::string scope, std::string session,
                        std::string owner);
  const std::string scope_, session_, owner_, id_;
  std::atomic<bool> revoked_{false};
  // Owned under the exporter's queue lock, never an HTTP user revision.
  int64_t accepted_revision_ = 0;
};

struct RealtimeTraceStoreHealth {
  // False is unknown, never an observed zero/healthy state. Diagnostics do
  // not wait on an auxiliary owner which may be stalled.
  bool snapshot_available = false;
  bool running = false;
  size_t pending = 0, queued_bytes = 0, producers = 0;
  uint64_t accepted = 0, persisted = 0, dropped = 0, failures = 0;
  uint64_t controller_failures = 0, controller_recoveries = 0;
};

class RealtimeTraceStore {
 public:
  explicit RealtimeTraceStore(RealtimeTraceStoreOptions options);
  ~RealtimeTraceStore();
  RealtimeTraceStore(const RealtimeTraceStore&) = delete;
  RealtimeTraceStore& operator=(const RealtimeTraceStore&) = delete;
  using Producer = std::shared_ptr<RealtimeTraceProducer>;
  // No I/O and no waiting for the queue lock on either producer method.
  Producer Open(std::string scope, std::string session_id) noexcept;
  bool Publish(const Producer& producer, io::Json snapshot) noexcept;
  // Read/list/delete confirmed durable state in the authenticated scope.
  // Deletion is ordered after an in-flight write and revokes queued writers.
  // Cancellation of an already started deletion retains commit_unknown.
  ObjectReply Execute(ObjectRequest request,
                      std::chrono::steady_clock::time_point deadline,
                      const std::function<bool()>& cancelled = {});
  void RequestStop() noexcept;
  void Join() noexcept;
  RealtimeTraceStoreHealth Health() const;
  void RecordCollectionFailure() noexcept;

 private:
  struct State;
  std::shared_ptr<State> state_;
};
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
// Native CPU fault boundary only; absent from serving builds.
void RealtimeTraceExporterHookForTesting(
    std::function<void(std::string_view)> hook);
void FailRealtimeTraceExporterStartForTesting();
#endif
}  // namespace q4t::server
