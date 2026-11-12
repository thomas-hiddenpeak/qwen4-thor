#include "q4t/server/grammar_process.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <thread>

#include "q4t/io/json.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/test.h"

using q4t::server::GrammarControl;
using q4t::server::GrammarFailure;
using q4t::server::GrammarWorkerReport;
using q4t::server::detail::GrammarProcess;
using q4t::server::detail::WorkerTransfer;
using namespace std::chrono_literals;

// Reachable only from the host test executable, never from q4t serve.
int GrammarProcessFixture(int argc, char** argv) {
  if (argc < 2 || !std::string(argv[1]).starts_with("--grammar-fixture-"))
    return -1;
  alarm(5);
  if (!q4t::server::detail::EnterGrammarWorker(argc, argv)) return 2;
  const std::string mode(argv[1]);
  if (mode == "--grammar-fixture-silent") {
    for (;;) pause();
  }
  if (mode == "--grammar-fixture-short") {
    char byte = 'x';
    WorkerTransfer(&byte, 1, true);
    return 0;
  }
  if (mode.starts_with("--grammar-fixture-output-")) {
    if (mode == "--grammar-fixture-output-empty") return 0;
    std::string payload(131073, 'x');
    payload[65536] = '\0';
    if (!WorkerTransfer(payload.data(), payload.size(), true)) return 4;
    if (mode == "--grammar-fixture-output-failed") return 7;
    if (mode == "--grammar-fixture-output-killed") raise(SIGKILL);
    if (mode == "--grammar-fixture-output-hang") {
      shutdown(3, SHUT_WR);
      for (;;) pause();
    }
    return 0;
  }
  rlimit limit{};
  if (getrlimit(RLIMIT_AS, &limit)) return 3;
  // An allocation beyond AS must fail before any pages can be touched.
  void* memory = mmap(nullptr, limit.rlim_cur + 4096, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  const bool rejected = memory == MAP_FAILED && errno == ENOMEM;
  if (memory != MAP_FAILED) munmap(memory, limit.rlim_cur + 4096);
  uint64_t report[] = {limit.rlim_cur, rejected,
                       static_cast<uint64_t>(fcntl(42, F_GETFD) == -1)};
  if (!WorkerTransfer(report, sizeof(report), true)) return 4;
  if (mode == "--grammar-fixture-idle") {
    for (;;) pause();
  }
  return 0;
}

namespace {
bool Reaped(const GrammarWorkerReport& report) {
  int status;
  return report.reaped && report.max_rss_bytes > 0 &&
         waitpid(-1, &status, WNOHANG) == -1 && errno == ECHILD;
}
size_t Descriptors() {
  size_t count = 0;
  for ([[maybe_unused]] const auto& entry :
       std::filesystem::directory_iterator("/proc/self/fd"))
    ++count;
  return count;
}
bool Ready(GrammarProcess& process) {
  uint64_t report[3]{};
  return process.Transfer(report, sizeof(report), false,
                          std::chrono::steady_clock::now() + 2s) &&
         report[0] == q4t::server::kGrammarAddressSpaceLimit &&
         report[1] == 1 && report[2] == 1;
}

class DeferredReapFixture {
 public:
  DeferredReapFixture() {
    q4t::server::detail::DeferGrammarReapingForTesting(true);
  }
  ~DeferredReapFixture() { Drain(); }
  bool Drain() {
    q4t::server::detail::DeferGrammarReapingForTesting(false);
    const auto end = std::chrono::steady_clock::now() + 1s;
    while ((q4t::server::detail::GrammarCleanupPending() ||
            q4t::server::detail::IsolatedCleanupPending(
                q4t::server::IsolatedWorkerDomain::kObjects)) &&
           std::chrono::steady_clock::now() < end)
      std::this_thread::sleep_for(1ms);
    return !q4t::server::detail::GrammarCleanupPending() &&
           !q4t::server::detail::IsolatedCleanupPending(
               q4t::server::IsolatedWorkerDomain::kObjects);
  }
};
}  // namespace

Q4T_TEST(grammar_process_memory_limit_and_descriptor_isolation) {
  const int original = open("/dev/null", O_RDONLY | O_CLOEXEC);
  Q4T_CHECK(original >= 0 && fcntl(42, F_GETFD) == -1);
  Q4T_CHECK(dup2(original, 42) == 42);  // Deliberately not CLOEXEC.
  close(original);
  GrammarControl control;
  GrammarWorkerReport report;
  int started = 0;
  control.worker_started = [&] { ++started; };
  control.worker_finished = [&](const auto& value) { report = value; };
  std::unique_ptr<GrammarProcess> process;
  const auto status =
      GrammarProcess::Start("--grammar-fixture-limits", control, &process);
  close(42);
  Q4T_CHECK(status && Ready(*process));
  process.reset();
  Q4T_CHECK(started == 1 && Reaped(report));
  return true;
}

Q4T_TEST(grammar_process_deadline_reaps_blocked_worker) {
  GrammarControl control;
  GrammarWorkerReport report;
  control.worker_finished = [&](const auto& value) { report = value; };
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-idle", control, &process));
  Q4T_CHECK(Ready(*process));
  char byte;
  const auto start = std::chrono::steady_clock::now();
  Q4T_CHECK(!process->Transfer(&byte, 1, false, start + 30ms));
  Q4T_CHECK(process->Failure() == GrammarFailure::kTimeout);
  Q4T_CHECK(std::chrono::steady_clock::now() - start < 2s && Reaped(report));
  Q4T_CHECK(WIFSIGNALED(report.exit_status) &&
            WTERMSIG(report.exit_status) == SIGKILL);
  return true;
}

Q4T_TEST(grammar_process_cancellation_interrupts_pending_io) {
  GrammarControl control;
  std::atomic<bool> cancelled{false};
  GrammarWorkerReport report;
  control.cancelled = [&] { return cancelled.load(); };
  control.worker_finished = [&](const auto& value) { report = value; };
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-idle", control, &process));
  Q4T_CHECK(Ready(*process));
  std::jthread cancel([&] {
    std::this_thread::sleep_for(20ms);
    cancelled.store(true);
  });
  char byte;
  Q4T_CHECK(!process->Transfer(&byte, 1, false,
                               std::chrono::steady_clock::now() + 2s));
  Q4T_CHECK(process->Failure() == GrammarFailure::kCancelled && Reaped(report));
  return true;
}

Q4T_TEST(grammar_process_short_read_and_cleanup_do_not_leak) {
  const auto descriptors = Descriptors();
  int started = 0, reaped = 0;
  GrammarControl control;
  control.worker_started = [&] { ++started; };
  control.worker_finished = [&](const auto& value) {
    if (value.reaped) ++reaped;
  };
  for (int i = 0; i < 3; ++i) {
    std::unique_ptr<GrammarProcess> process;
    Q4T_CHECK(
        GrammarProcess::Start("--grammar-fixture-short", control, &process));
    uint32_t response = 0;
    Q4T_CHECK(!process->Transfer(&response, sizeof(response), false,
                                 std::chrono::steady_clock::now() + 2s));
    Q4T_CHECK(process->Failure() == GrammarFailure::kWorker);
  }
  Q4T_CHECK(started == 3 && reaped == 3 && Descriptors() == descriptors);
  return true;
}

Q4T_TEST(grammar_process_closed_peer_write_has_no_sigpipe) {
  GrammarControl control;
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(
      GrammarProcess::Start("--grammar-fixture-limits", control, &process));
  Q4T_CHECK(Ready(*process));
  std::vector<char> payload(2 * 1024 * 1024, 'x');
  Q4T_CHECK(!process->Transfer(payload.data(), payload.size(), true,
                               std::chrono::steady_clock::now() + 2s));
  Q4T_CHECK(process->Failure() == GrammarFailure::kWorker);
  return true;
}

Q4T_TEST(grammar_process_cannot_raise_memory_ceiling) {
  GrammarControl control;
  std::unique_ptr<GrammarProcess> process;
  control.address_space_bytes = q4t::server::kGrammarAddressSpaceLimit + 1;
  Q4T_CHECK(
      !GrammarProcess::Start("--grammar-fixture-limits", control, &process));
  control.address_space_bytes = 0;
  Q4T_CHECK(
      !GrammarProcess::Start("--grammar-fixture-limits", control, &process));
  Q4T_CHECK(!process);
  return true;
}

Q4T_TEST(grammar_process_unconfirmed_reap_retains_capacity_without_callbacks) {
  const auto descriptors = Descriptors();
  auto counters = std::make_shared<q4t::server::GrammarWorkerCounters>();
  auto request = std::make_shared<int>(7);
  std::weak_ptr<int> request_lifetime = request;
  int completed = 0;
  GrammarWorkerReport report;
  DeferredReapFixture deferred;
  {
    GrammarControl control;
    control.counters = counters;
    control.worker_finished = [request, &completed,
                               &report](const auto& value) {
      ++completed;
      report = value;
    };
    request.reset();
    std::unique_ptr<GrammarProcess> process;
    Q4T_CHECK(
        GrammarProcess::Start("--grammar-fixture-idle", control, &process));
    Q4T_CHECK(Ready(*process));
    process->ReportRss(12345);
    const auto start = std::chrono::steady_clock::now();
    process->Stop();
    Q4T_CHECK(std::chrono::steady_clock::now() - start < 500ms);
    process->Stop();
    Q4T_CHECK(completed == 1 && !report.reaped);
  }
  Q4T_CHECK(request_lifetime.expired());
  Q4T_CHECK(counters->started == 1 && counters->active == 1 &&
            counters->reaped == 0);
  Q4T_CHECK(q4t::server::detail::GrammarCleanupPending());
  for (int i = 0; i < 32; ++i) {
    std::unique_ptr<GrammarProcess> blocked;
    GrammarFailure failure;
    Q4T_CHECK(!GrammarProcess::Start("--grammar-fixture-idle", {}, &blocked,
                                     &failure));
    Q4T_CHECK(!blocked && failure == GrammarFailure::kUnavailable);
  }
#ifdef Q4T_ENABLE_XGRAMMAR
  q4t::io::Json request_json;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"messages":[{"role":"user","content":"q"}],)"
      R"("enable_thinking":false,"response_format":{"type":"json_object"}})",
      &request_json));
  q4t::server::OutputConstraints plan;
  Q4T_CHECK(q4t::server::PlanOutputConstraints(request_json, &plan));
  std::unique_ptr<q4t::server::StructuredDecoder> decoder;
  GrammarFailure compile_failure;
  Q4T_CHECK(!q4t::server::StructuredDecoder::Create(
      plan, {"", "{}"}, 2, {0}, &decoder, {}, &compile_failure));
  Q4T_CHECK(!decoder && compile_failure == GrammarFailure::kUnavailable);
#endif
  q4t::io::Json capabilities;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("cpu", false, false), &capabilities));
  const auto* structured = capabilities.Find("structured_output");
  Q4T_CHECK(structured && !structured->GetBool("available", true) &&
            structured->GetBool("cleanup_pending"));
  Q4T_CHECK(deferred.Drain());
  Q4T_CHECK(completed == 1 && counters->active == 0 && counters->reaped == 1 &&
            counters->rss_reports == 1 &&
            counters->reported_rss_bytes == 12345);
  for (int i = 0; i < 32; ++i) q4t::server::detail::PollGrammarWorkers();
  Q4T_CHECK(counters->reaped == 1 && counters->rss_reports == 1);
  Q4T_CHECK(Descriptors() == descriptors);
  return true;
}

Q4T_TEST(grammar_process_deferred_counters_survive_request_then_release) {
  DeferredReapFixture deferred;
  std::weak_ptr<q4t::server::GrammarWorkerCounters> lifetime;
  {
    GrammarControl control;
    control.counters = std::make_shared<q4t::server::GrammarWorkerCounters>();
    lifetime = control.counters;
    std::unique_ptr<GrammarProcess> process;
    Q4T_CHECK(
        GrammarProcess::Start("--grammar-fixture-idle", control, &process));
    Q4T_CHECK(Ready(*process));
  }
  Q4T_CHECK(!lifetime.expired());
  Q4T_CHECK(deferred.Drain());
  Q4T_CHECK(lifetime.expired());
  return true;
}

Q4T_TEST(grammar_process_active_worker_and_pending_worker_share_one_slot) {
  GrammarControl control;
  auto counters = std::make_shared<q4t::server::GrammarWorkerCounters>();
  control.counters = counters;
  std::unique_ptr<GrammarProcess> first, second;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-idle", control, &first));
  Q4T_CHECK(Ready(*first));
  GrammarFailure failure;
  Q4T_CHECK(!GrammarProcess::Start("--grammar-fixture-idle", control, &second,
                                   &failure));
  Q4T_CHECK(failure == GrammarFailure::kUnavailable && counters->started == 1);
  first.reset();
  Q4T_CHECK(
      GrammarProcess::Start("--grammar-fixture-limits", control, &second));
  Q4T_CHECK(Ready(*second));
  second.reset();
  Q4T_CHECK(counters->started == 2 && counters->reaped == 2 &&
            counters->active == 0);
  return true;
}

Q4T_TEST(grammar_process_object_domain_keeps_grammar_capacity) {
  GrammarControl objects;
  objects.domain = q4t::server::IsolatedWorkerDomain::kObjects;
  objects.counters = std::make_shared<q4t::server::GrammarWorkerCounters>();
  std::unique_ptr<GrammarProcess> object, grammar;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-idle", objects, &object));
  Q4T_CHECK(Ready(*object));
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-idle", {}, &grammar));
  Q4T_CHECK(Ready(*grammar));
  DeferredReapFixture deferred;
  object.reset();
  Q4T_CHECK(q4t::server::detail::IsolatedCleanupPending(objects.domain));
  Q4T_CHECK(!q4t::server::detail::GrammarCleanupPending());
  GrammarFailure failure;
  Q4T_CHECK(!GrammarProcess::Start("--grammar-fixture-idle", objects, &object, &failure));
  Q4T_CHECK(failure == GrammarFailure::kUnavailable);
  Q4T_CHECK(deferred.Drain());
  grammar.reset();
  Q4T_CHECK(objects.counters->started == 1 && objects.counters->reaped == 1);
  return true;
}

Q4T_TEST(grammar_process_transfer_cancellation_does_not_retain_request) {
  GrammarControl control;
  control.domain = q4t::server::IsolatedWorkerDomain::kObjects;
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-idle", control, &process));
  auto request = std::make_shared<int>(42);
  std::weak_ptr<int> lifetime = request;
  uint64_t report[3]{};
  Q4T_CHECK(process->Transfer(report, sizeof(report), false,
                              std::chrono::steady_clock::now() + 2s,
                              [request] { return false; }));
  request.reset();
  Q4T_CHECK(lifetime.expired());
  char byte;
  Q4T_CHECK(!process->Transfer(&byte, 1, false,
                               std::chrono::steady_clock::now() + 2s,
                               [] { return true; }));
  Q4T_CHECK(process->Failure() == GrammarFailure::kCancelled);
  return true;
}

Q4T_TEST(grammar_process_output_requires_eof_and_normal_exit) {
  const auto descriptors = Descriptors();
  GrammarControl control;
  control.counters = std::make_shared<q4t::server::GrammarWorkerCounters>();
  int callbacks = 0;
  GrammarWorkerReport report;
  control.worker_finished = [&](const auto& value) {
    report = value;
    ++callbacks;
  };
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(
      GrammarProcess::Start("--grammar-fixture-output-ok", control, &process));
  std::string output = "previous";
  Q4T_CHECK(process->CollectOutput(&output, 131073,
                                   std::chrono::steady_clock::now() + 2s));
  Q4T_CHECK(output.size() == 131073 && output[65536] == '\0' &&
            output.back() == 'x' && Reaped(report) &&
            WIFEXITED(report.exit_status) && !WEXITSTATUS(report.exit_status));
  process.reset();
  Q4T_CHECK(callbacks == 1 && control.counters->active == 0 &&
            control.counters->reaped == 1 && Descriptors() == descriptors);
  return true;
}
Q4T_TEST(grammar_process_output_empty_success_is_distinct_from_bad_exit) {
  for (const char* entry :
       {"--grammar-fixture-output-empty", "--grammar-fixture-output-failed",
        "--grammar-fixture-output-killed"}) {
    GrammarWorkerReport report;
    GrammarControl control;
    control.worker_finished = [&](const auto& value) { report = value; };
    std::unique_ptr<GrammarProcess> process;
    Q4T_CHECK(GrammarProcess::Start(entry, control, &process));
    std::string output = "old";
    const auto status = process->CollectOutput(
        &output, 131073, std::chrono::steady_clock::now() + 2s);
    const bool empty = std::string_view(entry).ends_with("empty");
    Q4T_CHECK(static_cast<bool>(status) == empty && output.empty() &&
              Reaped(report));
    if (!empty) Q4T_CHECK(process->Failure() == GrammarFailure::kWorker);
  }
  return true;
}
Q4T_TEST(grammar_process_output_overflow_discards_partial_and_reaps) {
  GrammarWorkerReport report;
  GrammarControl control;
  control.worker_finished = [&](const auto& value) { report = value; };
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(
      GrammarProcess::Start("--grammar-fixture-output-ok", control, &process));
  std::string output = "old";
  Q4T_CHECK(!process->CollectOutput(&output, 65536,
                                    std::chrono::steady_clock::now() + 2s));
  Q4T_CHECK(output.empty() && process->Failure() == GrammarFailure::kResource &&
            Reaped(report));
  return true;
}
Q4T_TEST(grammar_process_output_eof_does_not_hide_stalled_exit) {
  GrammarWorkerReport report;
  GrammarControl control;
  control.worker_finished = [&](const auto& value) { report = value; };
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-output-hang", control,
                                  &process));
  std::string output;
  const auto start = std::chrono::steady_clock::now();
  Q4T_CHECK(!process->CollectOutput(&output, 131073, start + 100ms));
  Q4T_CHECK(output.empty() && process->Failure() == GrammarFailure::kTimeout &&
            Reaped(report) && std::chrono::steady_clock::now() - start < 1s);
  return true;
}
Q4T_TEST(grammar_process_output_cancel_interrupts_read_and_exit_wait) {
  for (const char* entry :
       {"--grammar-fixture-silent", "--grammar-fixture-output-hang"}) {
    GrammarWorkerReport report;
    GrammarControl control;
    control.worker_finished = [&](const auto& value) { report = value; };
    std::unique_ptr<GrammarProcess> process;
    Q4T_CHECK(GrammarProcess::Start(entry, control, &process));
    std::atomic<bool> cancelled{false};
    std::jthread cancel([&] {
      std::this_thread::sleep_for(50ms);
      cancelled.store(true);
    });
    std::string output;
    Q4T_CHECK(!process->CollectOutput(&output, 131073,
                                      std::chrono::steady_clock::now() + 2s,
                                      [&] { return cancelled.load(); }));
    Q4T_CHECK(output.empty() &&
              process->Failure() == GrammarFailure::kCancelled &&
              Reaped(report));
  }
  return true;
}
Q4T_TEST(grammar_process_output_unknown_exit_retains_slot_and_counters) {
  GrammarControl control;
  control.counters = std::make_shared<q4t::server::GrammarWorkerCounters>();
  GrammarWorkerReport report;
  int callbacks = 0;
  control.worker_finished = [&](const auto& value) {
    report = value;
    ++callbacks;
  };
  DeferredReapFixture deferred;
  std::unique_ptr<GrammarProcess> process;
  Q4T_CHECK(
      GrammarProcess::Start("--grammar-fixture-output-ok", control, &process));
  std::string output;
  Q4T_CHECK(!process->CollectOutput(&output, 131073,
                                    std::chrono::steady_clock::now() + 50ms));
  Q4T_CHECK(output.empty() && callbacks == 1 && !report.reaped &&
            control.counters->active == 1);
  process.reset();
  GrammarFailure failure;
  Q4T_CHECK(!GrammarProcess::Start("--grammar-fixture-output-empty", {},
                                   &process, &failure));
  Q4T_CHECK(failure == GrammarFailure::kUnavailable && deferred.Drain());
  Q4T_CHECK(callbacks == 1 && control.counters->active == 0 &&
            control.counters->reaped == 1);
  return true;
}
