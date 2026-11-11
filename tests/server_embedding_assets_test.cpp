#include "q4t/server/embedding_assets.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <thread>

#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;

struct Fixture {
  fs::path directory;
  GrammarControl control;
  EmbeddingHostAssets assets;
  GrammarFailure failure = GrammarFailure::kNone;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const fs::path root = env ? env : ".q4t-work/native-embedding-assets-tests";
    fs::create_directories(root);
    auto path = (fs::absolute(root) / "assets-XXXXXX").string();
    const char* made = mkdtemp(path.data());
    if (!made) throw std::runtime_error("embedding fixture directory");
    directory = made;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    control.compile_deadline = Clock::now() + 3s;
    assets.file_bytes = {42};
  }
  ~Fixture() {
    detail::SetEmbeddingAssetWorkerForTesting({});
    detail::DeferGrammarReapingForTesting(false);
    const auto end = Clock::now() + 1s;
    while (detail::IsolatedCleanupPending(IsolatedWorkerDomain::kEmbeddingAssets)
           && Clock::now() < end) std::this_thread::sleep_for(1ms);
    fs::remove_all(directory);
  }
  bool Rejected() {
    return !LoadEmbeddingAssets(directory, &assets, control, &failure) &&
           assets.file_bytes == std::vector<uint8_t>({42}) && !assets.tokenizer;
  }
  bool Size(const char* name, size_t bytes) {
    const int fd = open((directory / name).c_str(),
                        O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
    if (fd < 0) return false;
    const bool ok = !ftruncate(fd, bytes);
    close(fd);
    return ok;
  }
};
}  // namespace

Q4T_TEST(embedding_assets_invalid_path_cancel_and_deadline_start_no_child) {
  Fixture fixture;
  for (const char* path : {"", "relative", "/", "/tmp/../x", "/tmp//x"})
    Q4T_CHECK(!LoadEmbeddingAssets(path, &fixture.assets, fixture.control,
                                  &fixture.failure) &&
              fixture.failure == GrammarFailure::kDependency);
  fixture.control.cancelled = [] { return true; };
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kCancelled);
  fixture.control.cancelled = {};
  fixture.control.compile_deadline = Clock::now() - 1ms;
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kTimeout);
  Q4T_CHECK(fixture.control.counters->started == 0);
  return true;
}

Q4T_TEST(embedding_assets_actual_worker_rejects_missing_fifo_and_symlink) {
  Fixture fixture;
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kDependency);
  const auto config = fixture.directory / "config.json";
  Q4T_CHECK(!mkfifo(config.c_str(), 0600));
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kDependency);
  fs::remove(config);
  Q4T_CHECK(!symlink("/dev/zero", config.c_str()));
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kDependency);
  Q4T_CHECK(fixture.control.counters->started == 3 &&
            fixture.control.counters->active == 0 &&
            fixture.control.counters->reaped == 3);
  return true;
}

Q4T_TEST(embedding_assets_reject_hash_before_allocating_weight_staging) {
  Fixture fixture;
  Q4T_CHECK(fixture.Size("config.json", 727));
  Q4T_CHECK(fixture.Size("tokenizer.json", 11423705));
  // Sparse file has the correct external length. Bad metadata must fail
  // before the parent allocates/transfers the 1.19GB weight payload.
  Q4T_CHECK(fixture.Size("model.safetensors", kNativeEmbeddingFileBytes));
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kDependency);
  Q4T_CHECK(fixture.control.counters->started == 1 &&
            fixture.control.counters->reaped == 1);
  return true;
}

Q4T_TEST(embedding_assets_timeout_retains_only_its_domain_until_reaped) {
  Fixture fixture;
  detail::SetEmbeddingAssetWorkerForTesting("--grammar-fixture-silent");
  detail::DeferGrammarReapingForTesting(true);
  fixture.control.compile_deadline = Clock::now() + 80ms;
  const auto start = Clock::now();
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kTimeout);
  Q4T_CHECK(Clock::now() - start < 500ms);
  Q4T_CHECK(detail::IsolatedCleanupPending(IsolatedWorkerDomain::kEmbeddingAssets));
  fixture.control.compile_deadline = Clock::now() + 1s;
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kUnavailable);
  Q4T_CHECK(fixture.control.counters->started == 1);
  std::unique_ptr<detail::GrammarProcess> grammar;
  GrammarControl other;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", other,
                                         &grammar));
  detail::DeferGrammarReapingForTesting(false);
  grammar->Stop();
  const auto end = Clock::now() + 1s;
  while (detail::IsolatedCleanupPending(IsolatedWorkerDomain::kEmbeddingAssets)
         && Clock::now() < end) std::this_thread::sleep_for(1ms);
  Q4T_CHECK(!detail::IsolatedCleanupPending(IsolatedWorkerDomain::kEmbeddingAssets));
  Q4T_CHECK(fixture.control.counters->reaped == 1 &&
            fixture.control.counters->active == 0);
  detail::SetEmbeddingAssetWorkerForTesting({});
  Q4T_CHECK(fixture.Rejected() && fixture.failure == GrammarFailure::kDependency);
  Q4T_CHECK(fixture.control.counters->started == 2);
  return true;
}
