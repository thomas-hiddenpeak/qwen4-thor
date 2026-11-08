#include "q4t/server/object_store.h"

#include <sys/random.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
std::mutex test_mutex;
std::string test_entry;
std::atomic<bool> test_defer_spawn{false};
#endif
// Process-lifetime ownership survives a destructor's bounded wait. A stuck
// spawn cannot cause another ObjectStore to create replacement threads.
std::atomic<bool>& ControllerOwned() {
  static auto* owned = new std::atomic<bool>(false);
  return *owned;
}
bool ValidOptions(const ObjectStoreOptions& o) {
  return !o.directory.empty() && o.directory.front() == '/' &&
         o.directory.size() <= 4096 &&
         o.directory.find('\0') == std::string::npos &&
         o.max_object_bytes >= 1024 && o.max_object_bytes <= (kMaxFrame / 2) &&
         o.max_client_bytes >= o.max_object_bytes &&
         o.max_client_bytes <= o.max_total_bytes &&
         o.max_total_bytes <= (1ULL << 40) && o.max_client_objects > 0 &&
         o.max_total_objects >= o.max_client_objects &&
         o.max_total_objects <= 100000 && o.max_ttl_seconds > 0 &&
         o.max_ttl_seconds <= 366 * 24 * 3600 && o.io_timeout_ms > 0 &&
         o.io_timeout_ms <= 30000 && o.max_queue > 0 && o.max_queue <= 64 &&
         o.max_queue_bytes >= o.max_object_bytes &&
         o.max_queue_bytes <= (256ULL << 20) && o.restart_limit > 0 &&
         o.restart_limit <= 10 && o.restart_window_ms >= 100 &&
         o.restart_backoff_ms >= 0 &&
         o.restart_backoff_ms <= o.restart_window_ms;
}
Json Config(const ObjectStoreOptions& o) {
  return Object({{"operation", String("init")},
                 {"directory", String(o.directory)},
                 {"max_object_bytes", Number(o.max_object_bytes)},
                 {"max_client_bytes", Number(o.max_client_bytes)},
                 {"max_total_bytes", Number(o.max_total_bytes)},
                 {"max_client_objects", Number(o.max_client_objects)},
                 {"max_total_objects", Number(o.max_total_objects)},
                 {"max_ttl_seconds", Number(o.max_ttl_seconds)}});
}
Json RequestWire(const ObjectRequest& request) {
  return Object({{"operation", String(Operation(request.operation))},
                 {"kind", String(Kind(request.kind))},
                 {"owner", String(Digest(request.scope))},
                 {"id", String(request.id)},
                 {"data", request.data},
                 {"revision", Number(request.expected_revision)},
                 {"ttl", Number(request.ttl_seconds)},
                 {"limit", Number(request.limit)},
                 {"after", String(request.after)},
                 {"before", String(request.before)},
                 {"descending", Boolean(request.descending)}});
}
}  // namespace

struct ObjectStore::State {
  struct Job {
    std::string wire;
    Clock::time_point deadline;
    std::atomic<bool> abandoned{false};
    bool mutation = false;
    bool started = false;
    bool complete = false;
    ObjectReply reply;
  };
  ObjectStoreOptions options;
  std::string entry = "--q4t-object-worker";
  mutable std::mutex mutex;
  std::condition_variable changed;
  std::deque<std::shared_ptr<Job>> jobs;
  uint64_t queued_bytes = 0;
  size_t outstanding = 0;
  std::atomic<bool> stop{false};
  bool done = false;
  std::string health = "starting";
  std::shared_ptr<GrammarWorkerCounters> counters =
      std::make_shared<GrammarWorkerCounters>();

  void SetHealth(std::string value) {
    const std::lock_guard lock(mutex);
    health = std::move(value);
  }

  ObjectReply Exchange(detail::GrammarProcess& process, uint64_t sequence,
                       const std::string& text, const Job& job) {
    Frame request{kVersion, sequence, text.size()}, response;
    const auto cancelled = [&] { return stop.load() || job.abandoned.load(); };
    if (!process.Transfer(&request, sizeof(request), true, job.deadline,
                          cancelled) ||
        !process.Transfer(const_cast<char*>(text.data()), text.size(), true,
                          job.deadline, cancelled) ||
        !process.Transfer(&response, sizeof(response), false, job.deadline,
                          cancelled))
      return Error(503, "object_transport_error",
                   "Object worker operation did not return a receipt",
                   job.mutation);
    if (response.version != kVersion || response.sequence != sequence ||
        response.bytes == 0 || response.bytes > kMaxFrame)
      return Error(503, "object_transport_error", "Invalid object frame",
                   job.mutation);
    std::string payload(response.bytes, '\0');
    if (!process.Transfer(payload.data(), payload.size(), false, job.deadline,
                          cancelled))
      return Error(503, "object_transport_error", "Incomplete object reply",
                   job.mutation);
    Json value;
    if (!Parse(payload, &value))
      return Error(503, "object_transport_error", "Malformed object reply",
                   job.mutation);
    auto reply = DecodeReply(value);
    if (reply.code == "object_transport_error")
      reply.commit_unknown = job.mutation;
    return reply;
  }

  void Run() {
    std::unique_ptr<detail::GrammarProcess> process;
    std::deque<Clock::time_point> starts;
    auto retry_after = Clock::time_point::min();
    uint64_t sequence = 0;
    std::shared_ptr<Job> active;
    try {
      for (;;) {
        {
          std::unique_lock lock(mutex);
          changed.wait(lock, [&] { return stop.load() || !jobs.empty(); });
          if (stop.load()) break;
          active = jobs.front();
          jobs.pop_front();
          active->started = true;
        }
        ObjectReply reply;
        const auto now = Clock::now();
        if (active->abandoned || now >= active->deadline) {
          reply = Error(408, "object_cancelled", "Object request expired");
        } else {
          if (!process) {
            while (!starts.empty() &&
                   now - starts.front() >=
                       std::chrono::milliseconds(options.restart_window_ms))
              starts.pop_front();
            if (detail::IsolatedCleanupPending(
                    IsolatedWorkerDomain::kObjects)) {
              SetHealth("cleanup_pending");
              reply = Error(503, "object_cleanup_pending",
                            "Object worker cleanup remains unconfirmed");
            } else if (now < retry_after ||
                       starts.size() >=
                           static_cast<size_t>(options.restart_limit)) {
              SetHealth("backoff");
              reply = Error(503, "object_recovery_backoff",
                            "Object worker recovery budget or backoff active");
            } else {
              starts.push_back(now);
              SetHealth("starting");
              GrammarControl control;
              control.domain = IsolatedWorkerDomain::kObjects;
              control.counters = counters;
              sequence = 0;
              // No per-request callbacks survive in this persistent worker.
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
              while (test_defer_spawn.load()) std::this_thread::sleep_for(1ms);
#endif
              if (!detail::GrammarProcess::Start(entry.c_str(), control,
                                                 &process)) {
                reply = Error(503, "object_unavailable",
                              "Object worker could not start");
              } else {
                reply = Exchange(*process, ++sequence,
                                 OutputJson(Config(options)), *active);
              }
              if (!reply.ok()) {
                // Init has no application mutation, even for a Put caller.
                reply.commit_unknown = false;
                process.reset();
                retry_after = Clock::now() + std::chrono::milliseconds(
                                                 options.restart_backoff_ms);
                SetHealth("unavailable");
              }
            }
          }
          if (process && reply.ok()) {
            reply = Exchange(*process, ++sequence, active->wire, *active);
            if (reply.http_status >= 500) {
              process.reset();
              retry_after = Clock::now() + std::chrono::milliseconds(
                                               options.restart_backoff_ms);
              SetHealth("unavailable");
            } else {
              SetHealth("ready");
            }
          }
        }
        {
          const std::lock_guard lock(mutex);
          active->reply = std::move(reply);
          active->complete = true;
          queued_bytes -= active->wire.size();
          --outstanding;
        }
        changed.notify_all();
        active.reset();
      }
    } catch (...) {
      SetHealth("unavailable");
    }
    process.reset();
    {
      const std::lock_guard lock(mutex);
      if (active) {
        active->reply = Error(503, "object_unavailable",
                              "Object controller stopped", active->mutation);
        active->complete = true;
      }
      for (auto& job : jobs) {
        job->reply =
            Error(503, "object_unavailable", "Object controller stopped");
        job->complete = true;
      }
      jobs.clear();
      outstanding = 0;
      queued_bytes = 0;
      ControllerOwned().store(false);
      done = true;
      health = "stopped";
    }
    changed.notify_all();
  }
};

ObjectStore::ObjectStore(ObjectStoreOptions options)
    : state_(std::make_shared<State>()) {
  state_->options = std::move(options);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  {
    const std::lock_guard lock(test_mutex);
    if (!test_entry.empty()) state_->entry = test_entry;
  }
#endif
  if (!ValidOptions(state_->options)) {
    state_->health = "invalid_configuration";
    state_->done = true;
    return;
  }
  bool expected = false;
  if (!ControllerOwned().compare_exchange_strong(expected, true)) {
    state_->health = "controller_owned";
    state_->done = true;
    return;
  }
  try {
    std::thread([state = state_] { state->Run(); }).detach();
  } catch (...) {
    ControllerOwned().store(false);
    state_->health = "unavailable";
    state_->done = true;
  }
}

ObjectStore::~ObjectStore() {
  state_->stop.store(true);
  state_->changed.notify_all();
  std::unique_lock lock(state_->mutex);
  // A stuck exec/syscall retains the shared state and unique controller slot.
  state_->changed.wait_for(lock, 100ms, [&] { return state_->done; });
}

ObjectReply ObjectStore::Execute(const ObjectRequest& request,
                                 Clock::time_point deadline,
                                 const std::function<bool()>& cancelled) {
  const auto& options = state_->options;
  const auto kind = Kind(request.kind),
             operation = Operation(request.operation);
  if (request.scope.empty() || request.scope.size() > 256 || kind.empty() ||
      operation.empty() || request.expected_revision < 0 ||
      (operation != "list" && operation != "health" && !ValidId(request.id)) ||
      (operation == "put" && (request.ttl_seconds < 0 ||
                              request.ttl_seconds > options.max_ttl_seconds)) ||
      (operation == "list" &&
       (request.limit < 1 || request.limit > 100 ||
        (!request.after.empty() && !ValidId(request.after)) ||
        (!request.before.empty() && !ValidId(request.before)) ||
        (!request.after.empty() && !request.before.empty()))))
    return Error(400, "invalid_object_request", "Invalid object request");
  auto wire = OutputJson(RequestWire(request));
  if (wire.size() > options.max_object_bytes)
    return Error(413, "object_too_large", "Object request exceeds byte limit");
  return Dispatch(std::move(wire), operation == "put" || operation == "erase",
                  deadline, cancelled);
}

ObjectReply ObjectStore::ExecuteBatch(std::span<const ObjectRequest> requests,
                                      Clock::time_point deadline,
                                      const std::function<bool()>& cancelled) {
  if (requests.empty() || requests.size() > 1024)
    return Error(400, "invalid_object_request", "Invalid transaction size");
  Json mutations = Array();
  size_t bytes = 0;
  for (const auto& request : requests) {
    if ((request.operation != ObjectOperation::kPut &&
         request.operation != ObjectOperation::kErase) ||
        request.scope.empty() || request.scope.size() > 256 ||
        request.scope != requests.front().scope || !ValidId(request.id) ||
        Kind(request.kind).empty() || request.expected_revision < 0 ||
        request.ttl_seconds < 0 ||
        request.ttl_seconds > state_->options.max_ttl_seconds)
      return Error(400, "invalid_object_request",
                   "Invalid transaction mutation");
    auto value = RequestWire(request);
    const auto size = OutputJson(value).size();
    if (size > state_->options.max_object_bytes ||
        (bytes += size) > kMaxFrame / 2)
      return Error(413, "object_too_large", "Transaction exceeds byte limit");
    mutations.array.push_back(std::move(value));
  }
  return Dispatch(OutputJson(Object({{"operation", String("batch")},
                                     {"mutations", std::move(mutations)}})),
                  true, deadline, cancelled);
}

ObjectReply ObjectStore::Dispatch(std::string wire, bool mutation,
                                  Clock::time_point deadline,
                                  const std::function<bool()>& cancelled) {
  const auto& options = state_->options;
  auto job = std::make_shared<State::Job>();
  job->deadline =
      std::min(deadline,
               Clock::now() + std::chrono::milliseconds(options.io_timeout_ms));
  job->mutation = mutation;
  job->wire = std::move(wire);
  if (job->wire.size() > kMaxFrame / 2 ||
      job->wire.size() > options.max_queue_bytes)
    return Error(413, "object_too_large", "Object request exceeds byte limit");
  if (Clock::now() >= job->deadline)
    return Error(504, "object_timeout", "Object deadline already expired");
  if (cancelled && cancelled())
    return Error(499, "object_cancelled", "Object request already cancelled");
  std::unique_lock lock(state_->mutex);
  if (state_->done || state_->stop)
    return Error(503, "object_unavailable", "Object controller unavailable");
  if (state_->outstanding >= options.max_queue ||
      job->wire.size() > options.max_queue_bytes - state_->queued_bytes)
    return Error(429, "object_queue_full", "Object request queue is full");
  state_->jobs.push_back(job);
  state_->queued_bytes += job->wire.size();
  ++state_->outstanding;
  state_->changed.notify_all();
  for (;;) {
    if (job->complete) return std::move(job->reply);
    // User callbacks run only on the caller's thread and never under our lock.
    lock.unlock();
    bool cancel = false;
    try {
      cancel = cancelled && cancelled();
    } catch (...) {
      job->abandoned.store(true);
      lock.lock();
      return Error(500, "object_cancel_callback_error",
                   "Object cancellation callback failed",
                   job->mutation && job->started);
    }
    lock.lock();
    if (job->complete) return std::move(job->reply);
    if (cancel || Clock::now() >= job->deadline) {
      job->abandoned.store(true);
      state_->changed.notify_all();
      return Error(cancel ? 499 : 504,
                   cancel ? "object_cancelled" : "object_timeout",
                   "Object operation has no confirmed receipt",
                   job->mutation && job->started);
    }
    state_->changed.wait_until(lock,
                               std::min(job->deadline, Clock::now() + 10ms));
  }
}

ObjectStoreHealth ObjectStore::Health() const {
  const std::lock_guard lock(state_->mutex);
  return {state_->health, state_->outstanding, state_->counters->started.load(),
          state_->counters->reaped.load()};
}

std::string NewObjectId(ObjectKind kind) {
  auto prefix = Kind(kind);
  if (prefix.empty()) return {};
  if (kind == ObjectKind::kResponse) prefix = "resp";
  if (kind == ObjectKind::kConversation) prefix = "conv";
  if (kind == ObjectKind::kChat) prefix = "chatcmpl";
  unsigned char bytes[16];
  if (getrandom(bytes, sizeof(bytes), GRND_NONBLOCK) != sizeof(bytes))
    return {};
  std::string result = prefix + "_";
  constexpr char hex[] = "0123456789abcdef";
  for (const auto byte : bytes) {
    result.push_back(hex[byte >> 4]);
    result.push_back(hex[byte & 15]);
  }
  return result;
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void ObjectWorkerEntryForTesting(std::string entry) {
  const std::lock_guard lock(test_mutex);
  test_entry = std::move(entry);
}
void DeferObjectSpawnForTesting(bool enabled) {
  test_defer_spawn.store(enabled);
}
#endif
}  // namespace q4t::server
