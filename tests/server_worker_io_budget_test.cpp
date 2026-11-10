#include "q4t/server/grammar_process.h"

#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <thread>

#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
std::filesystem::path Directory(int parent) {
  return std::filesystem::path(__FILE__).parent_path().parent_path() /
         ".q4t-work/public-host-fixtures/io-budget" / std::to_string(parent);
}
size_t Descriptors() {
  size_t count = 0;
  for ([[maybe_unused]] const auto& entry :
       std::filesystem::directory_iterator("/proc/self/fd"))
    ++count;
  return count;
}
int Listener() {
#if defined(__aarch64__)
  constexpr uint32_t architecture = AUDIT_ARCH_AARCH64;
#else
  constexpr uint32_t architecture = AUDIT_ARCH_X86_64;
#endif
  std::vector<sock_filter> filters{
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, architecture, 1, 0),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr))};
  for (uint32_t call : {SYS_write, SYS_pwrite64, SYS_openat, SYS_mkdirat,
                        SYS_clone, SYS_ftruncate}) {
    filters.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, call, 0, 1));
    filters.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF));
  }
  filters.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1));
  filters.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS));
  // This test filter exercises admission, not the full codec sandbox.
  filters.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
  sock_fprog program{static_cast<unsigned short>(filters.size()),
                     filters.data()};
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) return -1;
  return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                 SECCOMP_FILTER_FLAG_NEW_LISTENER, &program);
}
struct Fixture {
  std::filesystem::path directory = Directory(getpid());
  GrammarControl control;
  std::unique_ptr<GrammarProcess> process;
  Fixture() {
    std::filesystem::create_directories(directory);
    control.domain = IsolatedWorkerDomain::kFileParse;
    control.counters = std::make_shared<GrammarWorkerCounters>();
  }
  ~Fixture() {
    process.reset();
    if (control.counters->active == 0) std::filesystem::remove_all(directory);
  }
  bool Start(const char* mode, WorkerIoBudget budget = {}) {
    return GrammarProcess::Start(mode, control, &process) &&
           process->ReceiveIoBudget(budget, Clock::now() + 2s);
  }
  bool Clean() {
    return control.counters->active == 0 && control.counters->started == 1 &&
           control.counters->reaped == 1;
  }
  std::string Content() {
    std::ifstream input(directory / "budget.bin", std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(input), {});
  }
};
}  // namespace

int WorkerIoBudgetFixture(int argc, char** argv) {
  if (argc < 2 || !std::string_view(argv[1]).starts_with("--io-budget-"))
    return -1;
  if (!EnterGrammarWorker(argc, argv)) return 2;
  const std::string mode(argv[1]);
  const auto directory = Directory(getppid());
  const int file = open((directory / "budget.bin").c_str(),
                        O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (file < 0) return 3;
  if (mode == "--io-budget-false-listener") {
    return SendWorkerIoListener(file) ? 0 : 4;
  }
  const int listener = Listener();
  if (listener < 0 || !SendWorkerIoListener(listener)) return 4;
  close(listener);
  if (mode == "--io-budget-write" || mode == "--io-budget-write-limit") {
    if (write(file, "abc", 3) != 3 || pwrite(file, "defgh", 5, 3) != 5)
      return 5;
  } else if (mode == "--io-budget-truncate") {
    if (ftruncate(file, 4) || ftruncate(file, 8)) return 5;
  } else if (mode == "--io-budget-create") {
    for (int i = 0; i < 3; ++i) {
      const auto path = directory / ("created-" + std::to_string(i));
      const int fd = open(path.c_str(), O_CREAT | O_WRONLY | O_CLOEXEC, 0600);
      if (fd < 0) return 5;
      close(fd);
    }
  } else if (mode == "--io-budget-threads") {
    for (int i = 0; i < 3; ++i) {
      bool written = false;
      std::thread next([&] { written = write(file, "t", 1) == 1; });
      next.join();
      if (!written) return 5;
    }
  } else if (mode == "--io-budget-transfer") {
    if (write(file, "ab", 2) != 2) return 5;
    std::string input(1 << 20, '\0');
    if (!WorkerTransfer(input.data(), input.size(), false) ||
        input != std::string(input.size(), 'x'))
      return 6;
    std::string output(131073, 'y');
    if (!WorkerTransfer(output.data(), output.size(), true)) return 6;
    shutdown(3, SHUT_WR);
    if (write(file, "cdef", 4) != 4) return 5;
    return 0;
  } else if (mode == "--io-budget-loop") {
    for (;;) {
      if (pwrite(file, "x", 1, 0) != 1) return 5;
    }
  } else
    return 7;
  char ok = 'K';
  return WorkerTransfer(&ok, 1, true) ? 0 : 6;
}

Q4T_TEST(worker_io_budget_real_writes_and_scalar_offsets_preserve_bytes) {
  Fixture fixture;
  WorkerIoBudget budget;
  budget.write_bytes = 8;
  Q4T_CHECK(fixture.Start("--io-budget-write", budget));
  std::string result;
  Q4T_CHECK(fixture.process->CollectOutput(&result, 10, Clock::now() + 2s) &&
            result == "K" && fixture.Content() == "abcdefgh" &&
            fixture.Clean());
  return true;
}
Q4T_TEST(worker_io_budget_rejects_before_write_or_sparse_growth) {
  for (const auto* mode : {"--io-budget-write-limit", "--io-budget-truncate"}) {
    Fixture fixture;
    WorkerIoBudget budget;
    budget.write_bytes = 6;
    Q4T_CHECK(fixture.Start(mode, budget));
    std::string result = "stale";
    Q4T_CHECK(!fixture.process->CollectOutput(&result, 10, Clock::now() + 2s) &&
              fixture.process->Failure() == GrammarFailure::kResource &&
              result.empty() && fixture.Clean());
    if (std::string_view(mode).ends_with("limit"))
      Q4T_CHECK(fixture.Content() == "abc");
    else
      Q4T_CHECK(fixture.Content() == std::string(4, '\0'));
  }
  return true;
}
Q4T_TEST(worker_io_budget_bounds_actual_file_and_thread_creation) {
  {
    Fixture fixture;
    WorkerIoBudget budget;
    budget.file_creations = 2;
    Q4T_CHECK(fixture.Start("--io-budget-create", budget));
    std::string result;
    Q4T_CHECK(!fixture.process->CollectOutput(&result, 10, Clock::now() + 2s) &&
              fixture.process->Failure() == GrammarFailure::kResource &&
              fixture.Clean());
    Q4T_CHECK(std::filesystem::exists(fixture.directory / "created-0") &&
              std::filesystem::exists(fixture.directory / "created-1") &&
              !std::filesystem::exists(fixture.directory / "created-2"));
  }
  {
    Fixture fixture;
    WorkerIoBudget budget;
    budget.thread_creations = 2;
    Q4T_CHECK(fixture.Start("--io-budget-threads", budget));
    std::string result;
    Q4T_CHECK(!fixture.process->CollectOutput(&result, 10, Clock::now() + 2s) &&
              fixture.process->Failure() == GrammarFailure::kResource &&
              fixture.Content() == "tt" && fixture.Clean());
  }
  return true;
}
Q4T_TEST(worker_io_budget_pumps_during_input_output_and_post_eof_cleanup) {
  Fixture fixture;
  Q4T_CHECK(fixture.Start("--io-budget-transfer"));
  std::string input(1 << 20, 'x'), output;
  Q4T_CHECK(fixture.process->Transfer(input.data(), input.size(), true,
                                      Clock::now() + 2s));
  Q4T_CHECK(
      fixture.process->CollectOutput(&output, 131073, Clock::now() + 2s) &&
      output == std::string(131073, 'y') && fixture.Content() == "abcdef" &&
      fixture.Clean());
  return true;
}
Q4T_TEST(worker_io_budget_invalid_listener_and_duplicate_attach_do_not_leak) {
  const auto before = Descriptors();
  {
    Fixture fixture;
    Q4T_CHECK(!fixture.Start("--io-budget-false-listener") && fixture.Clean() &&
              fixture.process->Failure() == GrammarFailure::kWorker);
  }
  {
    Fixture fixture;
    Q4T_CHECK(fixture.Start("--io-budget-write"));
    Q4T_CHECK(!fixture.process->ReceiveIoBudget({}, Clock::now() + 1s) &&
              fixture.Clean());
  }
  Q4T_CHECK(Descriptors() == before);
  return true;
}
Q4T_TEST(worker_io_budget_deadline_and_cancel_interrupt_active_notifications) {
  for (bool cancel : {false, true}) {
    Fixture fixture;
    Q4T_CHECK(fixture.Start("--io-budget-loop"));
    const auto start = Clock::now();
    std::string result;
    Q4T_CHECK(!fixture.process->CollectOutput(
        &result, 10, start + (cancel ? 2s : 25ms),
        [&] { return cancel && Clock::now() - start >= 25ms; }));
    Q4T_CHECK(
        fixture.process->Failure() ==
            (cancel ? GrammarFailure::kCancelled : GrammarFailure::kTimeout) &&
        Clock::now() - start < 1s && result.empty() && fixture.Clean());
  }
  return true;
}
Q4T_TEST(worker_io_budget_listener_wait_and_bad_limits_are_bounded) {
  Fixture fixture;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-silent", fixture.control,
                                  &fixture.process));
  const auto start = Clock::now();
  Q4T_CHECK(!fixture.process->ReceiveIoBudget({}, start + 25ms) &&
            fixture.process->Failure() == GrammarFailure::kTimeout &&
            Clock::now() - start < 1s && fixture.Clean());
  fixture.process.reset();
  fixture.control.counters = std::make_shared<GrammarWorkerCounters>();
  WorkerIoBudget budget;
  budget.write_bytes = (1ULL << 30) + 1;
  Q4T_CHECK(!fixture.Start("--io-budget-write", budget) && fixture.Clean());
  return true;
}
