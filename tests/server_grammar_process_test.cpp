#include "q4t/server/grammar_process.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <thread>

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
  if (mode == "--grammar-fixture-short") {
    char byte = 'x';
    WorkerTransfer(&byte, 1, true);
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
