#include "q4t/server/cache_io.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <thread>
#include <vector>

#include "../src/server/cache_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::cache_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  CacheIoOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(output ? output : ".q4t-work/cache-io-host");
    fs::create_directories(root);
    auto path = (root / "cache-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("cache fixture");
    options.directory = path;
    options.backoff_ms = 0;
  }
};
struct Injection {
  ~Injection() {
    DeferCacheSpawnForTesting(false);
    CacheWorkerEntryForTesting({});
    detail::DeferGrammarReapingForTesting(false);
    for (auto domain :
         {IsolatedWorkerDomain::kSessionCache,
          IsolatedWorkerDomain::kPrefixCache, IsolatedWorkerDomain::kGrammar,
          IsolatedWorkerDomain::kObjects})
      detail::PollIsolatedWorkers(domain);
  }
};
bool Ready(CacheIo& io) {
  const auto end = Clock::now() + 1s;
  while (Clock::now() < end) {
    if (io.Initialize()) return true;
    std::this_thread::sleep_for(10ms);
  }
  return false;
}
bool DomainContinues(IsolatedWorkerDomain domain) {
  GrammarControl control;
  control.domain = domain;
  std::unique_ptr<detail::GrammarProcess> child;
  if (!detail::GrammarProcess::Start("--cache-fixture-descriptors", control,
                                     &child))
    return false;
  char byte = 0;
  return child->Transfer(&byte, 1, false, Clock::now() + 1s) && byte == 'x';
}
}  // namespace

Q4T_TEST(cache_io_creates_private_default_directory_only_in_worker) {
  Injection cleanup;
  Fixture fixture;
  const auto root = fixture.options.directory;
  fixture.options.directory += "/native/cache";
  fixture.options.create_directory = true;
  Q4T_CHECK(!fs::exists(fixture.options.directory));
  CacheIo io(fixture.options);
  Q4T_CHECK(Ready(io));
  struct stat info {};
  Q4T_CHECK(stat(fixture.options.directory.c_str(), &info) == 0 &&
            (info.st_mode & 0777) == 0700 && info.st_uid == geteuid());
  CacheFile file;
  Q4T_CHECK(io.Create(&file));
  std::vector<uint8_t> input(CacheIo::kAlignment, 51), actual(input.size());
  Q4T_CHECK(io.Write(file, 0, input) && io.Read(file, 0, actual));
  Q4T_CHECK(input == actual && fs::is_empty(fixture.options.directory));
  io.Close(file);
  return true;
}

Q4T_TEST(cache_io_default_directory_rejects_symlinks_and_public_permissions) {
  Injection cleanup;
  Fixture fixture;
  const auto root = fixture.options.directory;
  fs::create_directory(root + "/public");
  Q4T_CHECK(chmod((root + "/public").c_str(), 0755) == 0);
  fs::create_directory_symlink(root, root + "/alias");
  for (const auto& suffix : {"/alias/created", "/public", "/../escaped"}) {
    auto options = fixture.options;
    options.directory += suffix;
    options.create_directory = true;
    CacheIo io(options);
    Q4T_CHECK(!io.Initialize());
  }
  Q4T_CHECK(!fs::exists(root + "/created"));
  struct stat info {};
  Q4T_CHECK(stat((root + "/public").c_str(), &info) == 0 &&
            (info.st_mode & 0777) == 0755);
  return true;
}

int CacheIoFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const std::string_view entry = argv[1];
  if (entry != "--cache-fixture-descriptors" &&
      entry != "--cache-fixture-bad-reply")
    return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  if (entry == "--cache-fixture-descriptors") {
    if (std::getenv("Q4T_CACHE_TEST_SECRET")) return 3;
    for (int fd = 4; fd < 1024; ++fd)
      if (fcntl(fd, F_GETFD) != -1) return 4;
    char byte = 'x';
    return detail::WorkerTransfer(&byte, 1, true) ? 0 : 5;
  }
  Frame request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.bytes > 4096)
    return 6;
  std::vector<char> path(request.bytes);
  if (!detail::WorkerTransfer(path.data(), path.size(), false)) return 7;
  Reply reply;
  reply.magic = 0;
  reply.sequence = request.sequence;
  reply.generation = request.generation;
  reply.code = kOk;
  return detail::WorkerTransfer(&reply, sizeof(reply), true) ? 0 : 8;
}

Q4T_TEST(cache_io_exec_inherits_only_owned_channel) {
  int pipe_fds[2];
  Q4T_CHECK(pipe(pipe_fds) == 0);  // Deliberately not CLOEXEC.
  struct Fds {
    int a, b;
    ~Fds() {
      close(a);
      close(b);
      unsetenv("Q4T_CACHE_TEST_SECRET");
    }
  } fds{pipe_fds[0], pipe_fds[1]};
  setenv("Q4T_CACHE_TEST_SECRET", "not-in-child", 1);
  Q4T_CHECK(DomainContinues(IsolatedWorkerDomain::kSessionCache));
  return true;
}

Q4T_TEST(cache_io_private_disk_directory_contract) {
  Fixture f;
  {
    CacheIo io(f.options);
    Q4T_CHECK(io.Initialize());
    Q4T_CHECK(io.Health().available && io.Health().worker_pid > 0);
  }
  chmod(f.options.directory.c_str(), 0755);
  {
    CacheIo io(f.options);
    Q4T_CHECK(!io.Initialize() && !io.Health().available);
  }
  chmod(f.options.directory.c_str(), 0700);
  {
    auto relative = f.options;
    relative.directory = fs::relative(relative.directory).string();
    CacheIo io(relative);
    Q4T_CHECK(io.Initialize());
  }
  auto link = f.options.directory + "-link";
  fs::create_directory_symlink(f.options.directory, link);
  f.options.directory = link;
  {
    CacheIo io(f.options);
    Q4T_CHECK(!io.Initialize());
  }
  f.options.directory += "-missing";
  CacheIo missing(f.options);
  Q4T_CHECK(!missing.Initialize());
  return true;
}

Q4T_TEST(cache_io_multiblock_bytes_and_actual_sparse_allocation) {
  Fixture f;
  CacheIo io(f.options);
  CacheFile file;
  Q4T_CHECK(io.Create(&file));
  std::vector<uint8_t> data(CacheIo::kBlockBytes), read(data.size());
  for (size_t i = 0; i < data.size(); ++i) data[i] = i * 19 + i / 7;
  Q4T_CHECK(io.Write(file, 0, data));
  Q4T_CHECK(io.Write(file, CacheIo::kBlockBytes,
                     std::span<const uint8_t>(data).first(4096)));
  Q4T_CHECK(io.Read(file, 0, read) && data == read);
  std::vector<uint8_t> tail(4096);
  Q4T_CHECK(io.Read(file, CacheIo::kBlockBytes, tail));
  Q4T_CHECK(std::equal(tail.begin(), tail.end(), data.begin()));
  Q4T_CHECK(io.Write(file, CacheIo::kMaxFileSpan - 4096, tail));
  CacheFileStat stat;
  Q4T_CHECK(io.Stat(file, &stat));
  Q4T_CHECK(stat.span_bytes == CacheIo::kMaxFileSpan &&
            stat.allocated_bytes >= CacheIo::kBlockBytes + 8192 &&
            stat.allocated_bytes < (64ULL << 20));
  Q4T_CHECK(io.Health().worker_rss_bytes > 0);
  io.Close(file);
  return true;
}

Q4T_TEST(cache_io_bad_frames_generations_and_bounds_rejected) {
  Injection cleanup;
  Fixture f;
  {
    CacheIo io(f.options);
    CacheFile file;
    Q4T_CHECK(io.Create(&file));
    std::vector<uint8_t> data(4096);
    Q4T_CHECK(!io.Write(file, CacheIo::kMaxFileSpan, data));
    Q4T_CHECK(!io.Write(file, 1, data));
    auto old = file;
    ++old.generation;
    Q4T_CHECK(!io.Read(old, 0, data));
    Q4T_CHECK(io.Write(file, 0, data));  // Local rejection kept valid state.
  }
  // Send a forged-generation command on the actual child protocol.
  {
    GrammarControl control;
    control.domain = IsolatedWorkerDomain::kSessionCache;
    std::unique_ptr<detail::GrammarProcess> child;
    Q4T_CHECK(
        detail::GrammarProcess::Start("--q4t-cache-worker", control, &child));
    Frame init;
    init.sequence = init.generation = 1;
    init.operation = kInit;
    init.bytes = f.options.directory.size();
    const auto deadline = Clock::now() + 2s;
    Q4T_CHECK(child->Transfer(&init, sizeof(init), true, deadline));
    Q4T_CHECK(child->Transfer(f.options.directory.data(), init.bytes, true,
                              deadline));
    Reply reply;
    Q4T_CHECK(child->Transfer(&reply, sizeof(reply), false, deadline) &&
              reply.code == kOk);
    init.operation = kCreate;
    init.bytes = 0;
    init.sequence = init.generation = 2;
    Q4T_CHECK(child->Transfer(&init, sizeof(init), true, deadline));
    Q4T_CHECK(!child->Transfer(&reply, sizeof(reply), false, deadline));
  }
  CacheWorkerEntryForTesting("--cache-fixture-bad-reply");
  CacheIo bad(f.options);
  Q4T_CHECK(!bad.Initialize() && !bad.Health().available);
  return true;
}

Q4T_TEST(cache_io_stopped_worker_deadline_and_cancellation_are_bounded) {
  Fixture f;
  f.options.timeout_ms = 200;
  CacheIo io(f.options);
  CacheFile file;
  Q4T_CHECK(io.Create(&file));
  const auto generation = file.generation;
  Q4T_CHECK(kill(io.Health().worker_pid, SIGSTOP) == 0);
  std::vector<uint8_t> data(4096, 83);
  auto start = Clock::now();
  Q4T_CHECK(!io.Write(file, 0, data));
  Q4T_CHECK(Clock::now() - start < 400ms && !io.Health().available);
  Q4T_CHECK(Ready(io));
  Q4T_CHECK(!io.Read(file, 0, data));
  io.Close(file);
  Q4T_CHECK(io.Health().available);  // Old close cannot discard the new worker.
  Q4T_CHECK(io.Create(&file) && file.generation != generation);
  Q4T_CHECK(kill(io.Health().worker_pid, SIGSTOP) == 0);
  start = Clock::now();
  Q4T_CHECK(
      !io.Write(file, 0, data, [&] { return Clock::now() - start > 30ms; }));
  Q4T_CHECK(Clock::now() - start < 150ms && !io.Health().available);
  return true;
}

Q4T_TEST(cache_io_unreaped_worker_retains_only_its_own_domain) {
  Injection cleanup;
  Fixture f;
  f.options.timeout_ms = 100;
  CacheIo io(f.options);
  CacheFile file;
  Q4T_CHECK(io.Create(&file));
  detail::DeferGrammarReapingForTesting(true);
  Q4T_CHECK(kill(io.Health().worker_pid, SIGSTOP) == 0);
  std::vector<uint8_t> data(4096);
  Q4T_CHECK(!io.Write(file, 0, data));
  std::this_thread::sleep_for(150ms);
  Q4T_CHECK(!io.Initialize());
  Q4T_CHECK(io.Health().workers_started == 1 &&
            io.Health().workers_active == 1);
  Q4T_CHECK(io.Health().state == "cleanup_pending");
  f.options.domain = IsolatedWorkerDomain::kPrefixCache;
  {
    CacheIo prefix(f.options);
    Q4T_CHECK(prefix.Initialize());
    Q4T_CHECK(DomainContinues(IsolatedWorkerDomain::kGrammar));
    Q4T_CHECK(DomainContinues(IsolatedWorkerDomain::kObjects));
  }
  detail::DeferGrammarReapingForTesting(false);
  Q4T_CHECK(Ready(io));
  Q4T_CHECK(io.Health().workers_started == 2 &&
            io.Health().workers_reaped == 1);
  return true;
}

Q4T_TEST(cache_io_restart_budget_and_stop_intent) {
  Fixture f;
  f.options.timeout_ms = 100;
  {
    CacheIo io(f.options);
    for (int i = 0; i < 3; ++i) {
      Q4T_CHECK(Ready(io));
      CacheFile file;
      Q4T_CHECK(io.Create(&file));
      Q4T_CHECK(kill(io.Health().worker_pid, SIGKILL) == 0);
      CacheFileStat stat;
      Q4T_CHECK(!io.Stat(file, &stat));
    }
    Q4T_CHECK(!io.Initialize() && io.Health().workers_started == 3);
    Q4T_CHECK(io.Health().state == "backoff");
  }
  // The owning destructor confirms retirement; no background restart.
  Q4T_CHECK(
      !detail::IsolatedCleanupPending(IsolatedWorkerDomain::kSessionCache));
  Q4T_CHECK(DomainContinues(IsolatedWorkerDomain::kSessionCache));
  return true;
}

Q4T_TEST(cache_io_controller_exception_spawn_stall_and_thread_failure) {
  Injection cleanup;
  Fixture f;
  f.options.timeout_ms = 100;
  {
    FailCacheThreadStartForTesting();
    CacheIo io(f.options);
    Q4T_CHECK(Ready(io) && io.Health().controller_failures == 1);
    FailCacheControllerForTesting();
    Q4T_CHECK(!io.Initialize());
    Q4T_CHECK(Ready(io) && io.Health().controller_failures == 2);
    Q4T_CHECK(io.Health().workers_started == 2);
  }
  DeferCacheSpawnForTesting(true);
  {
    CacheIo io(f.options);
    auto start = Clock::now();
    Q4T_CHECK(!io.Initialize());
    Q4T_CHECK(Clock::now() - start < 300ms);
    Q4T_CHECK(io.Health().workers_started == 0);
  }  // Bounded destructor, stalled dispatcher still owns its capacity.
  {
    CacheIo replacement(f.options);
    Q4T_CHECK(!replacement.Initialize());
    Q4T_CHECK(replacement.Health().state == "controller_owned");
  }
  DeferCacheSpawnForTesting(false);
  std::this_thread::sleep_for(30ms);
  CacheIo recovered(f.options);
  Q4T_CHECK(Ready(recovered));
  return true;
}
