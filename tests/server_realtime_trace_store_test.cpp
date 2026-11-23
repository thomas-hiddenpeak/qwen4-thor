#include "q4t/server/realtime_trace_store.h"
#include "q4t/server/realtime_trace_api.h"
#include "q4t/server/realtime_trace_session.h"
#include "q4t/server/application_owner.h"
#include "q4t/server/http_connection.h"

#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/grammar_process.h"
#include "q4t/test.h"
#include "q4t/server/realtime_trace.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  fs::path root;
  ObjectStoreOptions application;
  RealtimeTraceStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto directory =
        fs::absolute(output ? output : ".q4t-work/trace-store-test");
    fs::create_directories(directory);
    auto name = (directory / "trace-XXXXXX").string();
    if (!mkdtemp(name.data()))
      throw std::runtime_error("trace fixture directory");
    root = name;
    application.directory = (root / "objects").string();
    options = ResolveRealtimeTraceStoreOptions(application.directory + "/");
  }
};
RealtimeTraceCollector Collector(std::string session = "sess_test") {
  RealtimeTraceConfiguration config;
  config.enabled = true;
  config.workflow_name = "project query";
  config.metadata = Object({{"private_metadata", String("caller-private")}});
  RealtimeTraceCollector result(std::move(session), "conv_test", config,
                                {1795185600000, 0});
  result.Begin("response", "resp_test", {}, {1795185600001, 1000000});
  return result;
}
ObjectRequest Request(ObjectOperation op, std::string id,
                      std::string scope = "client:a") {
  ObjectRequest request;
  request.operation = op;
  request.id = std::move(id);
  request.scope = std::move(scope);
  request.kind = ObjectKind::kTrace;
  return request;
}
RealtimeTraceStore::Producer Open(RealtimeTraceStore& store, std::string scope,
                                  std::string session) {
  const auto deadline = Clock::now() + 1s;
  while (Clock::now() < deadline) {
    if (auto producer = store.Open(scope, session)) return producer;
    std::this_thread::sleep_for(1ms);
  }
  return {};
}
ObjectReply Read(RealtimeTraceStore& store, std::string id,
                 std::string scope = "client:a") {
  return store.Execute(
      Request(ObjectOperation::kGet, std::move(id), std::move(scope)),
      Clock::now() + 5s);
}
ObjectReply AppWrite(ObjectStore& store) {
  auto request = Request(ObjectOperation::kPut, "resp_business");
  request.kind = ObjectKind::kResponse;
  request.data = Object({{"value", String("healthy application")}});
  return store.Execute(request, Clock::now() + 2s);
}
bool Wait(const std::function<bool()>& predicate, int ms = 2500) {
  const auto deadline = Clock::now() + std::chrono::milliseconds(ms);
  while (Clock::now() < deadline) {
    if (predicate()) return true;
    std::this_thread::sleep_for(2ms);
  }
  return predicate();
}
bool Submit(RealtimeTraceStore& store,
            const RealtimeTraceStore::Producer& producer,
            const RealtimeTraceCollector& collector) {
  // An actual owner retries a rejected snapshot on a later bounded tick.
  // Fixture waits only for transient lock contention, never false durability.
  return Wait([&] { return store.Publish(producer, collector.Snapshot()); });
}
void Advance(RealtimeTraceCollector& collector) {
  collector.EventSeen();
  if (!collector.Begin("function_arguments",
                       "call_" + std::to_string(collector.Revision()),
                       "resp_test", {1795185600002, 2000000}))
    throw std::runtime_error("fixture lifecycle did not advance");
}
struct Barrier {
  std::mutex mutex;
  std::condition_variable changed;
  bool entered = false, released = false;
  explicit Barrier(std::string point) {
    RealtimeTraceExporterHookForTesting([this, point](std::string_view stage) {
      if (stage != point) return;
      std::unique_lock lock(mutex);
      if (released) return;
      entered = true;
      changed.notify_all();
      changed.wait_for(lock, 5s, [&] { return released; });
    });
  }
  bool Entered() {
    std::unique_lock lock(mutex);
    return changed.wait_for(lock, 2s, [&] { return entered; });
  }
  void Release() {
    const std::lock_guard lock(mutex);
    released = true;
    changed.notify_all();
  }
  ~Barrier() {
    Release();
    RealtimeTraceExporterHookForTesting({});
  }
};
struct HookReset {
  ~HookReset() {
    RealtimeTraceExporterHookForTesting({});
    ObjectWorkerEntryForTesting({});
  }
};
}  // namespace

Q4T_TEST(trace_store_application_and_trace_domains_are_independent) {
  Fixture f;
  ObjectStore app(f.application);
  ObjectStore traces(f.options.objects);
  Q4T_CHECK(AppWrite(app).ok());
  auto put = Request(ObjectOperation::kPut, "trace_domain");
  put.data = Object({{"value", Number(1)}});
  put.ttl_seconds = f.options.objects.max_ttl_seconds;
  Q4T_CHECK(traces.Execute(put, Clock::now() + 2s).ok());
  Q4T_CHECK(app.Execute(put, Clock::now() + 2s).http_status == 400);
  put.kind = ObjectKind::kResponse;
  Q4T_CHECK(traces.Execute(put, Clock::now() + 2s).http_status == 400);
  ObjectStore duplicate(f.options.objects);
  Q4T_CHECK(duplicate.Health().state == "controller_owned");
  Q4T_CHECK(app.Health().workers_started == 1);
  Q4T_CHECK(traces.Health().workers_started == 1);
  Q4T_CHECK(traces.ScanResponses({}, Clock::now() + 1s).http_status == 400);
  Q4T_CHECK(traces.Retention(1, 1, {}, Clock::now() + 1s).http_status == 400);
  return true;
}

Q4T_TEST(trace_store_scoped_durable_crud_and_bounded_summary) {
  Fixture f;
  Q4T_CHECK(f.options.objects.directory == f.application.directory + "-traces");
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  auto collector = Collector();
  Q4T_CHECK(producer && Submit(store, producer, collector));
  auto record = Read(store, producer->Id());
  Q4T_CHECK(record.ok() && record.value.GetString("status") == "in_progress");
  Q4T_CHECK(record.value.Find("metadata")->GetString("private_metadata") ==
            "caller-private");
  Q4T_CHECK(record.value.Find("persistence")->GetInt("object_revision") == 1);
  Q4T_CHECK(Read(store, producer->Id(), "client:b").http_status == 404);
  auto foreign = Request(ObjectOperation::kErase, producer->Id(), "client:b");
  Q4T_CHECK(store.Execute(foreign, Clock::now() + 2s).http_status == 404);
  Q4T_CHECK(!producer->Revoked());
  auto page =
      store.Execute(Request(ObjectOperation::kList, {}), Clock::now() + 2s);
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() == 1);
  const auto& summary = page.value.GetArray("data")->array.front();
  Q4T_CHECK(summary.GetBool("summary") && summary.GetInt("span_count") == 1);
  Q4T_CHECK(!summary.Has("metadata") && !summary.Has("spans"));
  auto erased = store.Execute(Request(ObjectOperation::kErase, producer->Id()),
                              Clock::now() + 2s);
  Q4T_CHECK(erased.ok() && erased.value.GetBool("deleted"));
  Q4T_CHECK(producer->Revoked());
  collector.EventSeen();
  Q4T_CHECK(!store.Publish(producer, collector.Snapshot()));
  Q4T_CHECK(Read(store, producer->Id()).http_status == 404);
  return true;
}

Q4T_TEST(trace_store_coalesces_latest_snapshot_without_growing_queue) {
  Fixture f;
  f.options.max_queue = 2;
  RealtimeTraceStore store(f.options);
  auto a = Open(store, "client:a", "sess_test");
  auto b = Open(store, "client:b", "sess_other");
  auto collector = Collector();
  Barrier barrier("before_dispatch");
  Q4T_CHECK(a && b && Submit(store, a, collector));
  Q4T_CHECK(barrier.Entered());
  Advance(collector);
  Q4T_CHECK(Submit(store, a, collector));
  Advance(collector);
  Q4T_CHECK(Submit(store, a, collector));
  const auto before = Clock::now();
  Q4T_CHECK(!store.Publish(b, Collector("sess_other").Snapshot()));
  Q4T_CHECK(Clock::now() - before < 100ms);
  Q4T_CHECK(store.Health().pending == 2);
  barrier.Release();
  Q4T_CHECK(Wait([&] {
    const auto health = store.Health();
    return health.snapshot_available && health.pending == 0;
  }));
  const auto record = Read(store, a->Id());
  Q4T_CHECK(record.ok() && record.value.GetInt("revision") ==
                               static_cast<int64_t>(collector.Revision()));
  Q4T_CHECK(store.Health().persisted == 2);
  return true;
}

Q4T_TEST(trace_store_deletion_prevents_late_and_queued_resurrection) {
  Fixture f;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  auto collector = Collector();
  Q4T_CHECK(producer && Submit(store, producer, collector));
  Q4T_CHECK(Read(store, producer->Id()).ok());
  Barrier barrier("before_save_commit");
  Advance(collector);
  Q4T_CHECK(Submit(store, producer, collector));
  Q4T_CHECK(barrier.Entered());
  Advance(collector);
  Q4T_CHECK(Submit(store, producer, collector));
  auto erased = std::async(std::launch::async, [&] {
    return store.Execute(Request(ObjectOperation::kErase, producer->Id()),
                         Clock::now() + 3s);
  });
  Q4T_CHECK(Wait([&] { return producer->Revoked(); }));
  barrier.Release();
  Q4T_CHECK(erased.get().ok());
  Q4T_CHECK(Read(store, producer->Id()).http_status == 404);
  Q4T_CHECK(store.Health().persisted == 1);
  return true;
}

Q4T_TEST(trace_store_lost_final_snapshot_reports_capture_interrupted) {
  Fixture f;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  Q4T_CHECK(producer && Submit(store, producer, Collector()));
  const auto id = producer->Id();
  Q4T_CHECK(Read(store, id).ok());
  producer.reset();
  auto record = Read(store, id);
  Q4T_CHECK(record.ok() && record.value.GetString("status") == "interrupted");
  Q4T_CHECK(record.value.GetString("close_reason") == "capture_interrupted");
  Q4T_CHECK(record.value.Find("ended_at_ms")->IsNull());
  const auto& span = record.value.GetArray("spans")->array.front();
  Q4T_CHECK(span.GetString("status") == "interrupted");
  Q4T_CHECK(span.Find("duration_ms")->IsNull());
  return true;
}

Q4T_TEST(trace_store_restart_marks_orphan_without_replaying_work) {
  Fixture f;
  std::string id;
  {
    RealtimeTraceStore store(f.options);
    auto producer = Open(store, "client:a", "sess_test");
    Q4T_CHECK(producer && Submit(store, producer, Collector()));
    id = producer->Id();
    Q4T_CHECK(Read(store, id).ok());
  }
  RealtimeTraceStore restarted(f.options);
  const auto record = Read(restarted, id);
  Q4T_CHECK(record.ok() && record.value.GetString("status") == "interrupted");
  Q4T_CHECK(record.value.GetString("close_reason") == "owner_interrupted");
  Q4T_CHECK(record.value.Find("persistence")->GetBool("owner_interrupted"));
  Q4T_CHECK(record.value.Find("persistence")->GetInt("object_revision") == 1);
  Q4T_CHECK(restarted.Health().persisted == 0);
  return true;
}

Q4T_TEST(trace_store_io_failure_does_not_disable_application_store) {
  Fixture f;
  std::ofstream(f.options.objects.directory) << "not a directory";
  ObjectStore application(f.application);
  RealtimeTraceStore traces(f.options);
  auto producer = Open(traces, "client:a", "sess_test");
  Q4T_CHECK(producer && Submit(traces, producer, Collector()));
  Q4T_CHECK(!Read(traces, producer->Id()).ok());
  Q4T_CHECK(traces.Health().failures > 0);
  Q4T_CHECK(AppWrite(application).ok());
  Q4T_CHECK(application.Health().state == "ready");
  return true;
}

Q4T_TEST(trace_session_storage_repair_flushes_idle_latest_snapshot) {
  Fixture f;
  f.options.objects.restart_window_ms = 100;
  f.options.objects.restart_backoff_ms = 10;
  std::ofstream(f.options.objects.directory) << "owned storage outage";
  RealtimeTraceStore store(f.options);
  // Opening the exporter is opportunistic. Exercise the production idle
  // tick even when the first configure call cannot obtain a producer.
  bool exporter_available = false;
  RealtimeSessionTrace trace("sess_idle", "conv_idle", [&] {
    return exporter_available ? &store : nullptr;
  }, "client:a");
  const auto started = RealtimeTraceNow();
  trace.Configure(Object({{"tracing", String("auto")}}), started);
  exporter_available = true;
  Q4T_CHECK(Wait([&] {
    const auto health = store.Health();
    if (health.snapshot_available && health.accepted == 0)
      trace.Tick(RealtimeTraceNow());
    return health.snapshot_available && health.pending == 0 &&
           health.failures > 0;
  }));
  Q4T_CHECK(fs::remove(f.options.objects.directory));
  Q4T_CHECK(fs::create_directory(f.options.objects.directory));
  fs::permissions(f.options.objects.directory, fs::perms::owner_all);
  ObjectReply saved;
  const auto id = "trace_" + Digest("sess_idle");
  // No new event, response or close creates another collector revision.
  Q4T_CHECK(Wait([&] {
    trace.Tick(RealtimeTraceNow());
    saved = Read(store, id);
    return saved.ok();
  }));
  Q4T_CHECK(saved.value.GetString("status") == "in_progress" &&
            saved.value.GetInt("revision") == 1 &&
            saved.value.GetInt("started_at_ms") == started.unix_ms &&
            saved.value.GetArray("spans")->array.empty());
  Q4T_CHECK(store.Health().persisted == 1 && store.Health().accepted >= 2);
  return true;
}

Q4T_TEST(trace_store_failed_commit_retry_reads_original_without_rewrite) {
  Fixture f;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  auto collector = Collector();
  HookReset reset;
  std::atomic<bool> fail{true};
  RealtimeTraceExporterHookForTesting([&](std::string_view point) {
    if (point == "after_save_commit" && fail.exchange(false))
      throw std::runtime_error("confirmed write lost before completion");
  });
  Q4T_CHECK(producer && Submit(store, producer, collector));
  Q4T_CHECK(Wait([&] { return producer->RetryNeeded(collector.Revision()); }));
  const auto first = Read(store, producer->Id());
  Q4T_CHECK(first.ok() &&
            first.value.Find("persistence")->GetInt("object_revision") == 1);
  Q4T_CHECK(Submit(store, producer, collector));
  const auto second = Read(store, producer->Id());
  Q4T_CHECK(second.ok() && OutputJson(first.value) == OutputJson(second.value));
  Q4T_CHECK(!producer->RetryNeeded(collector.Revision()) &&
            !store.Publish(producer, collector.Snapshot()));
  Q4T_CHECK(store.Health().persisted == 1 && store.Health().accepted == 2);
  return true;
}

Q4T_TEST(trace_store_failed_revision_cannot_replace_newer_or_deleted_owner) {
  Fixture f;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  auto collector = Collector();
  const auto old = collector.Snapshot();
  HookReset reset;
  std::atomic<bool> fail{true};
  RealtimeTraceExporterHookForTesting([&](std::string_view point) {
    if (point == "before_dispatch" && fail.exchange(false))
      throw std::runtime_error("queued snapshot failed");
  });
  Q4T_CHECK(producer && Submit(store, producer, collector));
  Q4T_CHECK(Wait([&] { return producer->RetryNeeded(collector.Revision()); }));
  Advance(collector);
  Q4T_CHECK(Submit(store, producer, collector));
  Q4T_CHECK(!store.Publish(producer, old));
  const auto saved = Read(store, producer->Id());
  Q4T_CHECK(saved.ok() && saved.value.GetInt("revision") ==
                             static_cast<int64_t>(collector.Revision()));
  Q4T_CHECK(store.Execute(Request(ObjectOperation::kErase, producer->Id()),
                          Clock::now() + 2s).ok());
  Q4T_CHECK(producer->Revoked() &&
            !producer->RetryNeeded(collector.Revision()) &&
            !store.Publish(producer, collector.Snapshot()) &&
            Read(store, producer->Id()).http_status == 404);
  return true;
}

Q4T_TEST(trace_store_exporter_exception_recovers_in_same_bounded_owner) {
  Fixture f;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  auto collector = Collector();
  HookReset reset;
  std::atomic<bool> fail{true};
  RealtimeTraceExporterHookForTesting([&](std::string_view stage) {
    if (stage == "before_dispatch" && fail.exchange(false))
      throw std::bad_alloc();
  });
  Q4T_CHECK(producer && Submit(store, producer, collector));
  Q4T_CHECK(Read(store, producer->Id()).http_status == 404);
  Advance(collector);
  Q4T_CHECK(Submit(store, producer, collector));
  Q4T_CHECK(Read(store, producer->Id()).ok());
  const auto health = store.Health();
  Q4T_CHECK(health.controller_failures == 1 &&
            health.controller_recoveries == 1);
  return true;
}

Q4T_TEST(trace_store_thread_construction_failure_recovers_with_backoff) {
  Fixture f;
  FailRealtimeTraceExporterStartForTesting();
  RealtimeTraceStore store(f.options);
  Q4T_CHECK(!store.Health().running && store.Health().controller_failures == 1);
  auto producer = Open(store, "client:a", "sess_test");
  Q4T_CHECK(producer && !store.Publish(producer, Collector().Snapshot()));
  std::this_thread::sleep_for(110ms);
  Q4T_CHECK(Read(store, producer->Id()).http_status == 404);
  Q4T_CHECK(Submit(store, producer, Collector()));
  Q4T_CHECK(Read(store, producer->Id()).ok());
  Q4T_CHECK(store.Health().controller_recoveries == 1);
  return true;
}

Q4T_TEST(trace_session_exporter_start_recovers_on_idle_tick_without_query) {
  Fixture f;
  FailRealtimeTraceExporterStartForTesting();
  RealtimeTraceStore store(f.options);
  RealtimeSessionTrace trace("sess_start", "conv_start", [&] { return &store; },
                             "client:a");
  trace.Configure(Object({{"tracing", String("auto")}}), RealtimeTraceNow());
  Q4T_CHECK(store.Health().controller_failures == 1);
  Q4T_CHECK(Wait([&] {
    trace.Tick(RealtimeTraceNow());
    return store.Health().persisted == 1;
  }));
  const auto record = Read(store, "trace_" + Digest("sess_start"));
  Q4T_CHECK(record.ok() && record.value.GetInt("revision") == 1);
  Q4T_CHECK(store.Health().controller_recoveries == 1 &&
            store.Health().accepted == 1);
  return true;
}

Q4T_TEST(trace_store_uncertain_commit_is_read_back_without_blind_replay) {
  Fixture f;
  HookReset reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  auto collector = Collector();
  Q4T_CHECK(producer && Submit(store, producer, collector));
  Q4T_CHECK(Wait(
      [&] {
        const auto health = store.Health();
        return health.snapshot_available && health.pending == 0;
      },
      4000));
  Q4T_CHECK(store.Health().failures == 1 && store.Health().persisted == 0);
  std::this_thread::sleep_for(110ms);
  const auto record = Read(store, producer->Id());
  Q4T_CHECK(record.ok());
  Q4T_CHECK(record.value.Find("persistence")->GetInt("object_revision") == 1);
  Q4T_CHECK(record.value.GetInt("revision") ==
            static_cast<int64_t>(collector.Revision()));
  Q4T_CHECK(Submit(store, producer, collector));
  Q4T_CHECK(Read(store, producer->Id())
                .value.Find("persistence")
                ->GetInt("object_revision") == 1);
  return true;
}

Q4T_TEST(trace_store_allocation_and_producer_limits_are_enforced) {
  Fixture f;
  f.options.max_client_producers = 1;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  Q4T_CHECK(producer && !store.Open("client:a", "sess_another"));
  Q4T_CHECK(!store.Open("client:a", "sess_test"));
  auto huge = Collector().Snapshot();
  Field(huge, "metadata").str.reserve(8ULL << 20);
  const auto start = Clock::now();
  Q4T_CHECK(!store.Publish(producer, std::move(huge)));
  Q4T_CHECK(Clock::now() - start < 100ms);
  Q4T_CHECK(store.Health().pending == 0);
  Q4T_CHECK(Submit(store, producer, Collector()));
  Q4T_CHECK(Read(store, producer->Id()).ok());
  return true;
}

Q4T_TEST(trace_store_blocked_exporter_retains_its_slot_after_bounded_shutdown) {
  Fixture f;
  Barrier barrier("before_dispatch");
  auto store = std::make_unique<RealtimeTraceStore>(f.options);
  auto producer = Open(*store, "client:a", "sess_test");
  Q4T_CHECK(producer && Submit(*store, producer, Collector()));
  Q4T_CHECK(barrier.Entered());
  const auto start = Clock::now();
  store.reset();
  Q4T_CHECK(Clock::now() - start < 500ms);
  {
    RealtimeTraceStore replacement(f.options);
    Q4T_CHECK(!replacement.Health().running);
    Q4T_CHECK(!replacement.Open("client:b", "sess_other"));
    ObjectStore application(f.application);
    Q4T_CHECK(AppWrite(application).ok());
  }
  barrier.Release();
  Q4T_CHECK(Wait([&] {
    RealtimeTraceStore replacement(f.options);
    const auto health = replacement.Health();
    return health.snapshot_available && health.running;
  }));
  return true;
}

Q4T_TEST(realtime_trace_api_uses_authenticated_scope_and_strict_queries) {
  Fixture f;
  RealtimeTraceStore store(f.options);
  auto producer = Open(store, "client:a", "sess_test");
  Q4T_CHECK(producer && Submit(store, producer, Collector()));
  StateAccess access{"client:a", Clock::now() + 5s, {}};
  const auto get = [&](std::string_view target) {
    return HandleRealtimeTraceRequest(store, "GET", target, {}, access);
  };
  auto page = get("/v1/realtime/traces?session_id=sess_test");
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(page.value.GetArray("data")->array.front().GetString("id") ==
            producer->Id());
  access.scope = "client:b";
  page = get("/v1/realtime/traces?session_id=sess_test");
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.empty());
  Q4T_CHECK(get("/v1/realtime/traces/" + producer->Id()).http_status == 404);
  access.scope = "client:a";
  for (const auto* target :
       {"/v1/realtime/traces?scope=client:b", "/v1/realtime/traces?limit=0",
        "/v1/realtime/traces?limit=101", "/v1/realtime/traces?limit=1&limit=2",
        "/v1/realtime/traces?session_id=sess_test&limit=1",
        "/v1/realtime/traces?order=invalid"})
    Q4T_CHECK(get(target).http_status == 400);
  Q4T_CHECK(HandleRealtimeTraceRequest(store, "POST", "/v1/realtime/traces",
                                       "{}", access)
                .http_status == 405);
  Q4T_CHECK(HandleRealtimeTraceRequest(store, "GET", "/v1/realtime/traces",
                                       "{}", access)
                .http_status == 400);
  auto erased = HandleRealtimeTraceRequest(
      store, "DELETE", "/v1/realtime/traces/" + producer->Id(), {}, access);
  Q4T_CHECK(erased.ok() && erased.value.GetBool("deleted"));
  Q4T_CHECK(producer->Revoked());
  return true;
}

Q4T_TEST(realtime_trace_observer_late_start_and_close_are_durable) {
  Fixture f;
  ApplicationOwner<RealtimeTraceStore> owner({1ms, 100ms});
  RealtimeSessionTrace observer(
      "sess_observer", "conv_observer", [&] { return owner.Get(); },
      "client:a");
  const auto now = RealtimeTraceNow();
  observer.Configure(Object({{"tracing", String("auto")}}), now);
  Q4T_CHECK(observer.Enabled());
  Q4T_CHECK(!owner.TryStart(
      []() -> std::unique_ptr<RealtimeTraceStore> { throw std::bad_alloc(); }));
  std::this_thread::sleep_for(2ms);
  Q4T_CHECK(owner.TryStart(
      [&] { return std::make_unique<RealtimeTraceStore>(f.options); }));
  observer.Observe(Object({{"type", String("response.created")},
                           {"response", Object({{"id", String("resp_one")}})}}),
                   now);
  const auto id = "trace_" + Digest("sess_observer");
  auto later = now;
  later.monotonic_ns += 300000000;
  later.unix_ms += 300;
  observer.Tick(later);
  Q4T_CHECK(Wait([&] {
    later.monotonic_ns += 300000000;
    later.unix_ms += 300;
    observer.Tick(later);
    return Read(*owner, id).ok();
  }));
  observer.Close("session_closed", later);
  ObjectReply record;
  Q4T_CHECK(Wait([&] {
    later.monotonic_ns += 300000000;
    observer.Tick(later);
    record = Read(*owner, id);
    return record.ok() && record.value.GetString("status") == "closed";
  }));
  Q4T_CHECK(record.ok() && record.value.GetString("status") == "closed");
  Q4T_CHECK(record.value.GetArray("spans")->array.size() == 1);
  Q4T_CHECK(record.value.GetArray("spans")->array.front().GetString("status") ==
            "interrupted");
  Q4T_CHECK(owner.StartupHealth().recoveries == 1);
  owner.RequestStop();
  owner.Join();
  return true;
}

Q4T_TEST(realtime_trace_observer_callback_failure_does_not_escape) {
  bool fail = true;
  RealtimeSessionTrace observer(
      "sess_observer", "conv_observer",
      [&]() -> RealtimeTraceStore* {
        if (fail)
          throw std::runtime_error("fixture auxiliary owner unavailable");
        return nullptr;
      },
      "client:a");
  const auto now = RealtimeTraceNow();
  observer.Configure(Object({{"tracing", String("auto")}}), now);
  observer.Observe(Object({{"type", String("response.created")},
                           {"response", Object({{"id", String("resp_one")}})}}),
                   now);
  auto later = now;
  later.monotonic_ns += 300000000;
  observer.Tick(later);
  fail = false;
  observer.Close("client_disconnected", later);
  Q4T_CHECK(observer.Enabled());
  const auto health = RealtimeTraceDiagnostics(nullptr);
  Q4T_CHECK(!health.GetBool("configured", true) &&
            !health.GetBool("readiness_dependency", true) &&
            !health.GetBool("model_restart_dependency", true));
  Q4T_CHECK(health.Find("pending")->IsNull() &&
            health.Find("exporter_running")->IsNull());
  return true;
}

Q4T_TEST(realtime_trace_routes_use_observation_and_never_websocket_slots) {
  for (const auto* target :
       {"/v1/realtime/traces", "/v1/realtime/traces?limit=1",
        "/v1/realtime/traces/trace_one"}) {
    Q4T_CHECK(RealtimeTraceMethodAllowed("GET", target));
    Q4T_CHECK(!IsRealtimeUpgrade("GET", target));
    Q4T_CHECK(ClassifyHttpConnection("GET", target) ==
              HttpConnectionClass::kObservation);
  }
  Q4T_CHECK(
      RealtimeTraceMethodAllowed("DELETE", "/v1/realtime/traces/trace_one"));
  Q4T_CHECK(!RealtimeTraceMethodAllowed("DELETE", "/v1/realtime/traces"));
  Q4T_CHECK(
      !RealtimeTraceMethodAllowed("GET", "/v1/realtime/traces/trace_one/more"));
  Q4T_CHECK(!RealtimeTraceMethodAllowed(
      "GET", "/v1/realtime/traces/trace_one#fragment"));
  Q4T_CHECK(!RealtimeTraceMethodAllowed("GET", "/v1/realtime/traces-other"));
  return true;
}
