#include "q4t/server/separator_assets.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/model_assets_protocol.h"
#include "q4t/server/asr_assets.h"
#include "q4t/test.h"
#include "q4t/trace/sha256.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
constexpr auto kProfile = detail::ModelAssetProfile::kSeparator;

fs::path Assets() {
  const char* env = std::getenv("Q4T_SEPARATOR_ASSETS");
  return fs::absolute(
      env ? env : ".q4t-work/serve-overlap-20261118/native-assets-01");
}
struct Fixture {
  fs::path directory;
  GrammarControl control;
  GrammarFailure failure = GrammarFailure::kNone;
  SeparatorHostAssets output;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const fs::path root = env ? env : ".q4t-work/model-asset-tests";
    fs::create_directories(root);
    auto path = (fs::absolute(root) / "separator-XXXXXX").string();
    const char* made = mkdtemp(path.data());
    if (!made) throw std::runtime_error("separator fixture directory");
    directory = made;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    control.compile_deadline = Clock::now() + 10s;
    output.file_bytes = {42};
  }
  ~Fixture() {
    detail::SetModelAssetWorkerForTesting({});
    detail::DeferGrammarReapingForTesting(false);
    const auto end = Clock::now() + 1s;
    while (detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets) &&
           Clock::now() < end)
      std::this_thread::sleep_for(1ms);
    fs::remove_all(directory);
  }
  void Metadata() {
    const auto specs = detail::ModelAssetCatalog(kProfile);
    for (const auto& spec : specs.first(specs.size() - 1))
      fs::copy_file(Assets() / spec.name, directory / spec.name);
  }
  bool Size(const char* name, size_t size) {
    const int fd = open((directory / name).c_str(),
                        O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
    if (fd < 0) return false;
    const bool ok = !ftruncate(fd, size);
    close(fd);
    return ok;
  }
  bool Rejected(bool weights = false) {
    return !LoadSeparatorAssets(directory, &output, control, &failure,
                                weights) &&
           output.file_bytes == std::vector<uint8_t>({42}) &&
           output.data_offset == 0;
  }
};

bool Corrupt(const fs::path& path) {
  fs::permissions(path, fs::perms::owner_write, fs::perm_options::add);
  std::fstream file(path, std::ios::in | std::ios::out | std::ios::binary);
  char c = 0;
  file.read(&c, 1);
  c ^= 1;
  file.seekp(0);
  file.write(&c, 1);
  return file.good();
}
}  // namespace

Q4T_TEST(separator_assets_metadata_verifies_config_and_never_opens_weights) {
  Fixture f;
  f.Metadata();
  Q4T_CHECK(!mkfifo((f.directory / "model.safetensors").c_str(), 0600));
  Q4T_CHECK(LoadSeparatorAssets(f.directory, &f.output, f.control, &f.failure,
                                false));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.file_bytes.empty());
  Q4T_CHECK(f.output.data_offset == 0);
  Q4T_CHECK(
      !LoadSeparatorAssets(f.directory, &f.output, f.control, &f.failure));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency &&
            f.output.file_bytes.empty() && f.output.data_offset == 0);
  Q4T_CHECK(f.control.counters->started == 2 &&
            f.control.counters->reaped == 2 && f.control.counters->active == 0);
  return true;
}
Q4T_TEST(separator_assets_same_size_metadata_corruption_keeps_prior_owner) {
  Fixture f;
  f.Metadata();
  Q4T_CHECK(Corrupt(f.directory / "config.json"));
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(Corrupt(f.directory / "config.json"));
  Q4T_CHECK(LoadSeparatorAssets(f.directory, &f.output, f.control, &f.failure,
                                false));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.file_bytes.empty());
  Q4T_CHECK(f.control.counters->started == 2 &&
            f.control.counters->reaped == 2 && f.control.counters->active == 0);
  return true;
}
Q4T_TEST(separator_assets_wrong_backend_and_symlink_paths_are_rejected) {
  Fixture f;
  f.Metadata();
  AsrHostAssets asr;
  Q4T_CHECK(!LoadAsrAssets(f.directory, &asr, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency && !asr.tokenizer);
  fs::remove(f.directory / "config.json");
  fs::create_symlink(Assets() / "config.json", f.directory / "config.json");
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  fs::create_directory_symlink(Assets().parent_path(), f.directory / "linked");
  Q4T_CHECK(!LoadSeparatorAssets((f.directory / "linked" / Assets().filename()),
                                 &f.output, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency &&
            f.output.file_bytes == std::vector<uint8_t>{42});
  Q4T_CHECK(f.control.counters->started == 3 &&
            f.control.counters->reaped == 3 && f.control.counters->active == 0);
  return true;
}
Q4T_TEST(separator_assets_cancel_deadline_and_staging_start_no_worker) {
  Fixture f;
  f.control.cancelled = [] { return true; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  f.control.cancelled = {};
  f.control.compile_deadline = Clock::now();
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  f.control.compile_deadline = Clock::now() + 10s;
  Q4T_CHECK(!LoadSeparatorAssets(f.directory, &f.output, f.control, &f.failure,
                                 true,
                                 q4t::audio::SeparatorConfig::kFileBytes));
  Q4T_CHECK(f.failure == GrammarFailure::kResource);
  Q4T_CHECK(!LoadSeparatorAssets("relative", &f.output, f.control, &f.failure));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(f.output.file_bytes == std::vector<uint8_t>{42} &&
            f.output.data_offset == 0 && f.control.counters->started == 0);
  return true;
}
Q4T_TEST(separator_assets_worker_fault_and_unreaped_slot_do_not_spread) {
  Fixture f;
  f.Metadata();
  for (const char* entry :
       {"--model-asset-fixture-crash", "--model-asset-fixture-profile"}) {
    detail::SetModelAssetWorkerForTesting(entry);
    Q4T_CHECK(f.Rejected() && f.failure != GrammarFailure::kNone);
  }
  detail::SetModelAssetWorkerForTesting("--model-asset-fixture-silent");
  detail::DeferGrammarReapingForTesting(true);
  f.control.compile_deadline = Clock::now() + 80ms;
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  Q4T_CHECK(detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets));
  f.control.compile_deadline = Clock::now() + 2s;
  AsrHostAssets asr;
  Q4T_CHECK(!LoadAsrAssets(f.directory, &asr, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kUnavailable && !asr.tokenizer);
  Q4T_CHECK(f.control.counters->started == 3);
  GrammarControl ordinary;
  std::unique_ptr<detail::GrammarProcess> other;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", ordinary,
                                          &other));
  detail::DeferGrammarReapingForTesting(false);
  other->Stop();
  return true;
}
Q4T_TEST(separator_assets_full_fixed_checkpoint_is_verified_without_cuda) {
  Fixture f;
  Q4T_CHECK(LoadSeparatorAssets(Assets(), &f.output, f.control, &f.failure));
  Q4T_CHECK(f.failure == GrammarFailure::kNone);
  Q4T_CHECK(f.output.file_bytes.size() ==
            q4t::audio::SeparatorConfig::kFileBytes);
  Q4T_CHECK(f.output.data_offset ==
            q4t::audio::SeparatorConfig::kHeaderBytes + 8);
  Q4T_CHECK(f.output.WeightBytes().size() ==
            q4t::audio::SeparatorConfig::kWeightBytes);
  Q4T_CHECK(q4t::trace::HexDigest(q4t::trace::Sha256(f.output.file_bytes)) ==
            q4t::audio::SeparatorConfig::kWeightSha256);
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1 && f.control.counters->active == 0);
  return true;
}
