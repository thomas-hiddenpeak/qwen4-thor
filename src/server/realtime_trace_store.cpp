#include "q4t/server/realtime_trace_store.h"

#include <algorithm>
#include <array>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_trace.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
constexpr uint64_t kMaxCount = (1ULL << 53) - 1;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
std::mutex hook_mutex;
std::function<void(std::string_view)> test_hook;
std::atomic<bool> fail_start{false};
void TestHook(std::string_view point) {
  std::function<void(std::string_view)> hook;
  {
    const std::lock_guard lock(hook_mutex);
    hook = test_hook;
  }
  if (hook) hook(point);
}
#endif
std::atomic<bool>& ExporterOwned() {
  static auto* owned = new std::atomic<bool>(false);
  return *owned;
}
void Increment(std::atomic<uint64_t>* value) noexcept {
  auto old = value->load();
  while (old < kMaxCount && !value->compare_exchange_weak(old, old + 1)) {
  }
}
ObjectReply Unavailable(bool unknown = false) {
  return Error(503, "realtime_trace_unavailable",
               "Trace persistence is temporarily unavailable", unknown);
}
bool ValidOptions(const RealtimeTraceStoreOptions& o) {
  return !o.objects.directory.empty() &&
         o.objects.domain == ObjectStoreDomain::kTracing && o.max_producers &&
         o.max_producers <= 64 && o.max_client_producers &&
         o.max_client_producers <= o.max_producers && o.max_queue &&
         o.max_queue <= 32 &&
         o.max_queue_bytes >= RealtimeTraceCollector::kMaximumRecordBytes &&
         o.max_queue_bytes <= (32ULL << 20);
}
// A previous engine's last in-progress snapshot is an observation of an
// interrupted owner. Do not invent an end time or replay any work.
ObjectReply Project(Json record, std::string_view owner, bool producer_alive) {
  auto* data = record.Find("data");
  auto* value = data ? data->Find("trace") : nullptr;
  if (!data || data->GetString("format") != "q4t.realtime_trace.v1" || !value ||
      !value->IsObject())
    return Error(503, "realtime_trace_record_invalid", "Invalid trace record");
  auto result = std::move(Field(Field(record, "data"), "trace"));
  const bool other_owner = data->GetString("owner") != owner;
  const bool interrupted = (other_owner || !producer_alive) &&
                           result.GetString("status") == "in_progress";
  if (interrupted) {
    Set(&result, "status", String("interrupted"));
    Set(&result, "close_reason",
        String(other_owner ? "owner_interrupted" : "capture_interrupted"));
    Set(&result, "ended_at_ms", Json());
    Set(&result, "duration_ms", Json());
    const auto* spans = result.Find("spans");
    if (spans && spans->IsArray())
      for (auto& span : Field(result, "spans").array)
        if (span.GetString("status") == "in_progress") {
          Set(&span, "status", String("interrupted"));
          Set(&span, "ended_at_ms", Json());
          Set(&span, "duration_ms", Json());
        }
  }
  Set(&result, "persistence",
      Object({{"state", String("durable_snapshot")},
              {"object_revision", Number(record.GetInt("revision"))},
              {"snapshot_revision", Number(result.GetInt("revision"))},
              {"owner_interrupted", Boolean(interrupted && other_owner)},
              {"capture_interrupted", Boolean(interrupted)},
              {"expires_at", Number(record.GetInt("expires_at"))}}));
  ObjectReply reply;
  reply.value = std::move(result);
  return reply;
}
}  // namespace

RealtimeTraceStoreOptions ResolveRealtimeTraceStoreOptions(std::string path) {
  while (path.size() > 1 && path.back() == '/') path.pop_back();
  RealtimeTraceStoreOptions result;
  if (path.size() > 1) result.objects.directory = path + "-traces";
  result.objects.domain = ObjectStoreDomain::kTracing;
  result.objects.max_object_bytes = 4ULL << 20;
  result.objects.max_client_bytes = 16ULL << 20;
  result.objects.max_total_bytes = 64ULL << 20;
  result.objects.max_client_objects = 128;
  result.objects.max_total_objects = 1024;
  result.objects.max_ttl_seconds = 7 * 24 * 3600;
  result.objects.max_queue = 2;
  result.objects.max_queue_bytes = 8ULL << 20;
  result.objects.io_timeout_ms = 1000;
  return result;
}
RealtimeTraceProducer::RealtimeTraceProducer(std::string scope,
                                             std::string session,
                                             std::string owner)
    : scope_(std::move(scope)),
      session_(std::move(session)),
      owner_(std::move(owner)),
      id_("trace_" + Digest(session_)) {}

struct RealtimeTraceStore::State {
  struct Job {
    Producer producer;
    Json snapshot;
    ObjectRequest request;
    Clock::time_point deadline;
    size_t bytes = 0;
    bool started = false, complete = false;
    std::atomic<bool> abandoned{false};
    ObjectReply reply = Unavailable();
  };
  explicit State(RealtimeTraceStoreOptions value)
      : options(std::move(value)), owner(NewObjectId(ObjectKind::kTrace)) {}
  const RealtimeTraceStoreOptions options;
  const std::string owner;
  std::unique_ptr<ObjectStore> objects;
  mutable std::mutex mutex;
  std::condition_variable changed;
  std::vector<std::weak_ptr<RealtimeTraceProducer>> producers;
  std::deque<std::shared_ptr<Job>> queue;
  std::shared_ptr<Job> active;
  size_t queued_bytes = 0;
  bool valid = false, owns_slot = false, running = false, done = false;
  std::atomic<bool> stop{false};
  std::atomic<uint64_t> accepted{0}, persisted{0}, dropped{0}, failures{0};
  uint64_t controller_failures = 0, controller_recoveries = 0;
  std::array<Clock::time_point, 3> failed_at{};
  size_t failed_count = 0;
  Clock::time_point retry_after = Clock::time_point::min();
  bool recovering = false;

  void Failure() noexcept {
    const auto now = Clock::now();
    size_t kept = 0;
    for (size_t i = 0; i < failed_count; ++i)
      if (now - failed_at[i] < 60s) failed_at[kept++] = failed_at[i];
    failed_count = kept;
    if (failed_count < failed_at.size()) failed_at[failed_count++] = now;
    retry_after = now + 100ms;
    if (failed_count == failed_at.size())
      retry_after = std::max(retry_after, failed_at[0] + 60s);
    ++controller_failures;
    recovering = true;
    Increment(&failures);
  }
  // Queue lock held. A failed construction may retry; a running or blocked
  // thread retains its process-wide slot and cannot create a replacement.
  void Launch(const std::shared_ptr<State>& self) noexcept {
    if (!valid || !owns_slot || running || done || stop ||
        Clock::now() < retry_after)
      return;
    try {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      if (fail_start.exchange(false)) throw std::bad_alloc();
#endif
      std::thread([self] { self->Run(); }).detach();
      running = true;
    } catch (...) {
      Failure();
    }
  }
  ObjectReply Save(Job& job) {
    const auto& producer = *job.producer;
    if (producer.revoked_ || job.abandoned || stop) return {};
    ObjectRequest request;
    request.scope = producer.scope_;
    request.kind = ObjectKind::kTrace;
    request.id = producer.id_;
    const auto deadline = Clock::now() + 2s;
    const auto cancelled = [&] {
      return producer.revoked_ || job.abandoned || stop;
    };
    auto previous = objects->Execute(request, deadline, cancelled);
    if (!previous.ok() && previous.http_status != 404) return previous;
    if (previous.ok()) {
      const auto* data = previous.value.Find("data");
      if (!data || data->GetString("owner") != owner)
        return Error(409, "realtime_trace_owner_conflict",
                     "Trace belongs to a different session owner");
      const auto* trace = data->Find("trace");
      if (trace && trace->GetInt("revision") >= job.snapshot.GetInt("revision"))
        return {};
      request.expected_revision = previous.value.GetInt("revision");
      request.expires_at = previous.value.GetInt("expires_at");
    }
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    TestHook("before_save_commit");
#endif
    if (cancelled()) return {};
    request.operation = ObjectOperation::kPut;
    request.ttl_seconds = options.objects.max_ttl_seconds;
    request.data = Object({{"format", String("q4t.realtime_trace.v1")},
                           {"owner", String(owner)}});
    Set(&request.data, "trace", std::move(job.snapshot));
    auto reply = objects->Execute(request, deadline, cancelled);
    if (reply.ok()) Increment(&persisted);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    TestHook("after_save_commit");
#endif
    return reply;
  }
  bool ProducerAlive(std::string_view scope, std::string_view id) const {
    const std::lock_guard lock(mutex);
    for (const auto& weak : producers)
      if (auto p = weak.lock();
          p && p->scope_ == scope && p->id_ == id && !p->revoked_)
        return true;
    return false;
  }
  ObjectReply ReadOrDelete(Job& job) {
    auto reply = objects->Execute(job.request, job.deadline,
                                  [&] { return job.abandoned || stop; });
    if (!reply.ok()) return reply;
    if (job.request.operation == ObjectOperation::kErase) {
      reply.value = Object({{"object", String("realtime.trace.deleted")},
                            {"id", String(job.request.id)},
                            {"deleted", Boolean(true)}});
    } else if (job.request.operation == ObjectOperation::kGet) {
      const bool alive = ProducerAlive(job.request.scope, job.request.id);
      reply = Project(std::move(reply.value), owner, alive);
    } else {
      auto* rows = reply.value.Find("data");
      if (!rows || !rows->IsArray())
        return Error(503, "realtime_trace_record_invalid",
                     "Invalid trace page");
      for (auto& row : Field(reply.value, "data").array) {
        const bool alive =
            ProducerAlive(job.request.scope, row.GetString("id"));
        auto projected = Project(std::move(row), owner, alive);
        if (!projected.ok()) return projected;
        row = std::move(projected.value);
      }
    }
    return reply;
  }
  void Run() noexcept {
    while (!stop) {
      std::unique_lock lock(mutex);
      changed.wait_until(lock, retry_after, [&] { return stop.load(); });
      if (stop) break;
      changed.wait(lock, [&] { return stop || !queue.empty(); });
      if (stop) break;
      active = queue.front();
      queue.pop_front();
      active->started = true;
      const auto job = active;
      const auto revision = job->producer ? job->snapshot.GetInt("revision") : 0;
      if (recovering) {
        recovering = false;
        ++controller_recoveries;
      }
      lock.unlock();
      bool controller_failed = false;
      try {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
        TestHook("before_dispatch");
#endif
        if (job->abandoned || Clock::now() >= job->deadline) {
          job->reply =
              Error(408, "realtime_trace_cancelled", "Trace operation expired");
        } else {
          if (!objects)
            objects = std::make_unique<ObjectStore>(options.objects);
          job->reply = job->producer ? Save(*job) : ReadOrDelete(*job);
          if (!job->reply.ok()) Increment(&failures);
        }
      } catch (...) {
        // The preallocated error survives even an allocation failure. A
        // mutation already dispatched may have committed; never auto-replay.
        job->reply.commit_unknown =
            job->producer || job->request.operation == ObjectOperation::kErase;
        controller_failed = true;
      }
      lock.lock();
      if (controller_failed) Failure();
      if (job->producer && !job->reply.ok() && !job->producer->revoked_)
        job->producer->failed_revision_ = revision;
      job->complete = true;
      queued_bytes -= job->bytes;
      active.reset();
      changed.notify_all();
    }
    // Release I/O outside the queue lock. ObjectStore itself retains any
    // unconfirmed controller/worker until its own bounded cleanup completes.
    objects.reset();
    const std::lock_guard lock(mutex);
    for (const auto& job : queue) job->complete = true;
    queue.clear();
    queued_bytes = 0;
    running = false;
    done = true;
    if (owns_slot) ExporterOwned().store(false);
    owns_slot = false;
    changed.notify_all();
  }
};

RealtimeTraceStore::RealtimeTraceStore(RealtimeTraceStoreOptions options)
    : state_(std::make_shared<State>(std::move(options))) {
  state_->valid = ValidOptions(state_->options) && !state_->owner.empty();
  bool expected = false;
  if (state_->valid)
    state_->owns_slot = ExporterOwned().compare_exchange_strong(expected, true);
  const std::lock_guard lock(state_->mutex);
  state_->Launch(state_);
}
RealtimeTraceStore::~RealtimeTraceStore() {
  RequestStop();
  Join();
}
void RealtimeTraceStore::RequestStop() noexcept {
  const std::lock_guard lock(state_->mutex);
  state_->stop = true;
  for (auto& weak : state_->producers)
    if (auto producer = weak.lock()) producer->revoked_ = true;
  state_->changed.notify_all();
}
void RealtimeTraceStore::Join() noexcept {
  std::unique_lock lock(state_->mutex);
  if (!state_->running) {
    state_->done = true;
    if (state_->owns_slot) ExporterOwned().store(false);
    state_->owns_slot = false;
  }
  state_->changed.wait_for(lock, 100ms, [&] { return state_->done; });
}
RealtimeTraceStore::Producer RealtimeTraceStore::Open(
    std::string scope, std::string session_id) noexcept {
  try {
    if (scope.empty() || scope.size() > 256 || !ValidRequestId(session_id))
      return {};
    std::unique_lock lock(state_->mutex, std::try_to_lock);
    if (!lock || state_->stop || !state_->valid || !state_->owns_slot)
      return {};
    std::erase_if(state_->producers, [](const auto& p) { return p.expired(); });
    size_t client_count = 0;
    for (const auto& weak : state_->producers)
      if (auto producer = weak.lock(); producer && producer->scope_ == scope) {
        if (producer->session_ == session_id) return {};
        ++client_count;
      }
    if (state_->producers.size() >= state_->options.max_producers ||
        client_count >= state_->options.max_client_producers)
      return {};
    auto producer = Producer(new RealtimeTraceProducer(
        std::move(scope), std::move(session_id), state_->owner));
    state_->producers.emplace_back(producer);
    state_->Launch(state_);
    return producer;
  } catch (...) {
    Increment(&state_->failures);
    return {};
  }
}
bool RealtimeTraceStore::Publish(const Producer& producer,
                                 Json snapshot) noexcept {
  try {
    size_t bytes = 0;
    if (!producer || producer->revoked_ || producer->owner_ != state_->owner ||
        snapshot.GetString("object") != "realtime.trace" ||
        snapshot.GetString("id") != producer->id_ ||
        snapshot.GetString("session_id") != producer->session_ ||
        snapshot.GetInt("revision") <= 0 ||
        !MeasureRealtimeStorage(
            snapshot, RealtimeTraceCollector::kMaximumRecordBytes, &bytes)) {
      Increment(&state_->dropped);
      return false;
    }
    bytes += sizeof(State::Job);
    std::unique_lock lock(state_->mutex, std::try_to_lock);
    if (lock) state_->Launch(state_);
    if (!lock || state_->stop || !state_->running || producer->revoked_ ||
        snapshot.GetInt("revision") < producer->accepted_revision_ ||
        (snapshot.GetInt("revision") == producer->accepted_revision_ &&
         !producer->RetryNeeded(producer->accepted_revision_))) {
      Increment(&state_->dropped);
      return false;
    }
    auto existing = std::ranges::find_if(state_->queue, [&](const auto& job) {
      return job->producer == producer;
    });
    const auto old_bytes =
        existing == state_->queue.end() ? 0 : (*existing)->bytes;
    const auto retained = state_->queued_bytes - old_bytes;
    if (bytes > state_->options.max_queue_bytes - retained ||
        (existing == state_->queue.end() &&
         state_->queue.size() + bool(state_->active) >=
             state_->options.max_queue)) {
      Increment(&state_->dropped);
      return false;
    }
    const auto revision = snapshot.GetInt("revision");
    if (existing != state_->queue.end()) {
      (*existing)->snapshot = std::move(snapshot);
      (*existing)->bytes = bytes;
      (*existing)->deadline = Clock::now() + 5s;
    } else {
      auto job = std::make_shared<State::Job>();
      job->producer = producer;
      job->snapshot = std::move(snapshot);
      job->bytes = bytes;
      job->deadline = Clock::now() + 5s;
      state_->queue.push_back(std::move(job));
    }
    producer->accepted_revision_ = revision;
    producer->failed_revision_ = 0;
    state_->queued_bytes = retained + bytes;
    Increment(&state_->accepted);
    state_->changed.notify_all();
    return true;
  } catch (...) {
    Increment(&state_->dropped);
    Increment(&state_->failures);
    return false;
  }
}
ObjectReply RealtimeTraceStore::Execute(
    ObjectRequest request, Clock::time_point deadline,
    const std::function<bool()>& cancelled) {
  if (request.scope.empty() || request.scope.size() > 256 ||
      (request.operation != ObjectOperation::kGet &&
       request.operation != ObjectOperation::kList &&
       request.operation != ObjectOperation::kErase) ||
      (request.operation != ObjectOperation::kList && !ValidId(request.id)))
    return Error(400, "realtime_trace_request_invalid",
                 "Invalid trace operation");
  request.kind = ObjectKind::kTrace;
  request.data = Json();
  auto job = std::make_shared<State::Job>();
  job->request = std::move(request);
  job->deadline = std::min(deadline, Clock::now() + 5s);
  job->bytes = sizeof(State::Job) + job->request.scope.capacity() +
               job->request.id.capacity() + job->request.after.capacity() +
               job->request.before.capacity();
  std::unique_lock lock(state_->mutex);
  if (state_->stop || !state_->valid || !state_->owns_slot)
    return Unavailable();
  state_->Launch(state_);
  if (!state_->running) return Unavailable();
  if (job->request.operation == ObjectOperation::kErase) {
    for (auto& weak : state_->producers)
      if (auto p = weak.lock();
          p && p->scope_ == job->request.scope && p->id_ == job->request.id)
        p->revoked_ = true;
    std::erase_if(state_->queue, [&](const auto& queued) {
      const auto& p = queued->producer;
      if (!p || p->scope_ != job->request.scope || p->id_ != job->request.id)
        return false;
      state_->queued_bytes -= queued->bytes;
      Increment(&state_->dropped);
      return true;
    });
  }
  if (state_->queue.size() + bool(state_->active) >=
          state_->options.max_queue ||
      job->bytes > state_->options.max_queue_bytes - state_->queued_bytes)
    return Error(503, "realtime_trace_capacity",
                 "Trace queue capacity exhausted");
  state_->queue.push_back(job);
  state_->queued_bytes += job->bytes;
  state_->changed.notify_all();
  while (!job->complete) {
    if (Clock::now() >= job->deadline || (cancelled && cancelled())) {
      job->abandoned = true;
      return Error(
          408, "realtime_trace_cancelled", "Trace operation cancelled",
          job->started && job->request.operation == ObjectOperation::kErase);
    }
    state_->changed.wait_for(lock, 10ms);
  }
  return std::move(job->reply);
}
RealtimeTraceStoreHealth RealtimeTraceStore::Health() const {
  RealtimeTraceStoreHealth result;
  result.accepted = state_->accepted;
  result.persisted = state_->persisted;
  result.dropped = state_->dropped;
  result.failures = state_->failures;
  const std::unique_lock lock(state_->mutex, std::try_to_lock);
  if (!lock) return result;
  result.snapshot_available = true;
  result.running = state_->running;
  result.pending = state_->queue.size() + bool(state_->active);
  result.queued_bytes = state_->queued_bytes;
  for (const auto& p : state_->producers) result.producers += !p.expired();
  result.controller_failures = state_->controller_failures;
  result.controller_recoveries = state_->controller_recoveries;
  return result;
}
void RealtimeTraceStore::RecordCollectionFailure() noexcept {
  Increment(&state_->failures);
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void RealtimeTraceExporterHookForTesting(
    std::function<void(std::string_view)> hook) {
  const std::lock_guard lock(hook_mutex);
  test_hook = std::move(hook);
}
void FailRealtimeTraceExporterStartForTesting() { fail_start = true; }
#endif
}  // namespace q4t::server
