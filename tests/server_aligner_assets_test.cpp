#include "q4t/server/aligner_assets.h"

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
#include "q4t/audio/aligner_config.h"
#include "q4t/test.h"
#include "q4t/trace/sha256.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
constexpr auto kProfile = detail::ModelAssetProfile::kAligner;

fs::path Assets() {
  const char* env = std::getenv("Q4T_ALIGNER_ASSETS");
  return fs::absolute(env ? env
                          : ".q4t-work/serve-audio-aligner-20261117/assets-01");
}
struct Fixture {
  fs::path directory;
  GrammarControl control;
  GrammarFailure failure = GrammarFailure::kNone;
  AlignerHostAssets output;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const fs::path root = env ? env : ".q4t-work/model-asset-tests";
    fs::create_directories(root);
    auto path = (fs::absolute(root) / "aligner-XXXXXX").string();
    const char* made = mkdtemp(path.data());
    if (!made) throw std::runtime_error("alignment fixture directory");
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
    return !LoadAlignerAssets(directory, &output, control, &failure, weights) &&
           output.file_bytes == std::vector<uint8_t>({42}) && !output.tokenizer;
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

Q4T_TEST(aligner_assets_metadata_has_timestamp_and_never_opens_weights) {
  Fixture f;
  f.Metadata();
  Q4T_CHECK(!mkfifo((f.directory / "model.safetensors").c_str(), 0600));
  Q4T_CHECK(
      LoadAlignerAssets(f.directory, &f.output, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.tokenizer);
  Q4T_CHECK(f.output.file_bytes.empty() && f.output.layout.tensors.empty());
  std::vector<uint32_t> ids;
  Q4T_CHECK(f.output.tokenizer->Encode("<timestamp>", &ids));
  Q4T_CHECK(ids == std::vector<uint32_t>{151705});
  const auto* owner = f.output.tokenizer.get();
  Q4T_CHECK(
      !LoadAlignerAssets(f.directory, &f.output, f.control, &f.failure, true));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(f.output.tokenizer.get() == owner && f.output.file_bytes.empty());
  Q4T_CHECK(f.control.counters->started == 2 &&
            f.control.counters->reaped == 2 && f.control.counters->active == 0);
  return true;
}

Q4T_TEST(aligner_assets_each_metadata_rejects_same_size_corruption) {
  Fixture f;
  f.Metadata();
  const auto specs = detail::ModelAssetCatalog(kProfile);
  for (const auto& spec : specs.first(specs.size() - 1)) {
    Q4T_CHECK(Corrupt(f.directory / spec.name));
    Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
    Q4T_CHECK(Corrupt(f.directory / spec.name));
  }
  Q4T_CHECK(f.control.counters->started == 6 &&
            f.control.counters->reaped == 6 && f.control.counters->active == 0);
  return true;
}

Q4T_TEST(aligner_assets_wrong_profile_and_symlink_are_isolated) {
  Fixture f;
  f.Metadata();
  AsrHostAssets asr;
  Q4T_CHECK(!LoadAsrAssets(f.directory, &asr, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency && !asr.tokenizer);
  const auto source = Assets() / "config.json";
  fs::remove(f.directory / "config.json");
  fs::create_symlink(source, f.directory / "config.json");
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(
      detail::ModelAssetCatalog(static_cast<detail::ModelAssetProfile>(999))
          .empty());
  Q4T_CHECK(f.control.counters->started == 2 &&
            f.control.counters->reaped == 2);
  return true;
}

Q4T_TEST(aligner_assets_cancel_deadline_and_staging_start_no_worker) {
  Fixture f;
  f.control.cancelled = [] { return true; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  f.control.cancelled = {};
  f.control.compile_deadline = Clock::now();
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  f.control.compile_deadline = Clock::now() + 10s;
  Q4T_CHECK(!LoadAlignerAssets(f.directory, &f.output, f.control, &f.failure,
                               true, kNativeAlignerFileBytes - 1));
  Q4T_CHECK(f.failure == GrammarFailure::kResource);
  Q4T_CHECK(f.output.file_bytes == std::vector<uint8_t>{42});
  Q4T_CHECK(f.control.counters->started == 0);
  return true;
}

Q4T_TEST(aligner_assets_dead_worker_and_bad_reply_do_not_publish) {
  Fixture f;
  f.Metadata();
  for (const char* entry :
       {"--model-asset-fixture-crash", "--model-asset-fixture-profile"}) {
    detail::SetModelAssetWorkerForTesting(entry);
    Q4T_CHECK(f.Rejected());
    Q4T_CHECK(f.failure != GrammarFailure::kNone);
  }
  Q4T_CHECK(f.control.counters->started == 2 &&
            f.control.counters->reaped == 2 && f.control.counters->active == 0);
  return true;
}

// This separate full-weight check is run only under a frozen local lease.
Q4T_TEST(aligner_assets_full_checkpoint_verified_without_cuda) {
  Fixture f;
  f.control.compile_deadline = Clock::now() + 120s;
  Q4T_CHECK(LoadAlignerAssets(Assets(), &f.output, f.control, &f.failure));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.tokenizer);
  Q4T_CHECK(f.output.file_bytes.size() == kNativeAlignerFileBytes);
  Q4T_CHECK(f.output.data_offset == 86752);
  Q4T_CHECK(f.output.WeightBytes().size() == q4t::audio::AlignerWeightBytes());
  Q4T_CHECK(f.output.layout.tensors.size() == 708);
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1 && f.control.counters->active == 0);
  return true;
}
