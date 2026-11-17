#include "q4t/server/vad_assets.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
struct Fixture {
  fs::path directory;
  GrammarControl control;
  GrammarFailure failure{};
  q4t::audio::SileroVad model;
  Fixture() {
    auto root = fs::absolute(".q4t-work/vad-asset-tests");
    fs::create_directories(root);
    auto name = (root / "fixture-XXXXXX").string();
    if (!mkdtemp(name.data()))
      throw std::runtime_error("VAD fixture directory");
    directory = name;
    control.counters = std::make_shared<GrammarWorkerCounters>();
  }
  ~Fixture() {
    detail::SetModelAssetWorkerForTesting({});
    fs::remove_all(directory);
  }
  fs::path Source() const {
    const char* env = std::getenv("Q4T_VAD_ASSETS");
    return fs::absolute(env ? env
                            : ".q4t-work/serve-audio-aligner-20261117/vad-01");
  }
  fs::path File() const { return directory / "silero_vad_16k.safetensors"; }
  bool Load(const fs::path& dir) {
    return LoadVadAssets(dir.string(), &model, control, &failure).ok();
  }
  bool Clean() const {
    return control.counters->active == 0 &&
           control.counters->started == control.counters->reaped &&
           !detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets);
  }
};
}  // namespace

Q4T_TEST(vad_assets_load_fixed_cpu_model_through_verified_worker) {
  Fixture f;
  Q4T_CHECK(f.Load(f.Source()) && f.model.Ready() && f.Clean());
  Q4T_CHECK(f.failure == GrammarFailure::kNone &&
            f.control.counters->started == 1);
  const auto spec = detail::ModelAssetCatalog(detail::ModelAssetProfile::kVad);
  Q4T_CHECK(spec.size() == 1 && spec[0].bytes == q4t::audio::kVadFileBytes &&
            std::string_view(spec[0].sha256) == q4t::audio::kVadWeightSha256);
  std::array<float, 512> silence{};
  q4t::audio::VadState state;
  float probability = -1;
  Q4T_CHECK(f.model.Step(silence, &state, &probability));
  Q4T_CHECK(probability >= 0 && probability < .1);
  return true;
}

Q4T_TEST(vad_assets_bad_identity_keeps_loaded_cpu_owner_and_recovers) {
  Fixture f;
  Q4T_CHECK(f.Load(f.Source()));
  fs::copy_file(f.Source() / f.File().filename(), f.File());
  fs::permissions(f.File(), fs::perms::owner_write, fs::perm_options::add);
  {
    std::fstream file(f.File(),
                      std::ios::in | std::ios::out | std::ios::binary);
    char byte;
    file.read(&byte, 1);
    byte ^= 1;
    file.seekp(0);
    file.write(&byte, 1);
  }
  Q4T_CHECK(!f.Load(f.directory) && f.model.Ready() && f.Clean());
  Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  fs::remove(f.File());
  fs::create_hard_link(f.Source() / f.File().filename(), f.File());
  Q4T_CHECK(f.Load(f.directory) && f.model.Ready() && f.Clean());
  return true;
}

Q4T_TEST(vad_assets_missing_fifo_and_symlink_are_request_local) {
  Fixture f;
  Q4T_CHECK(!f.Load(f.directory) && !f.model.Ready() && f.Clean());
  Q4T_CHECK(!mkfifo(f.File().c_str(), 0600));
  Q4T_CHECK(!f.Load(f.directory) && f.Clean());
  fs::remove(f.File());
  fs::create_symlink(f.Source() / f.File().filename(), f.File());
  Q4T_CHECK(!f.Load(f.directory) && f.Clean());
  Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  return true;
}

Q4T_TEST(vad_assets_preflight_cancel_deadline_and_budget_start_no_worker) {
  Fixture f;
  f.control.cancelled = [] { return true; };
  Q4T_CHECK(!f.Load(f.Source()) && f.failure == GrammarFailure::kCancelled);
  f.control.cancelled = {};
  f.control.compile_deadline = Clock::now();
  Q4T_CHECK(!f.Load(f.Source()) && f.failure == GrammarFailure::kTimeout);
  f.control.compile_deadline = Clock::now() + std::chrono::seconds(10);
  Q4T_CHECK(!LoadVadAssets(f.Source(), &f.model, f.control, &f.failure, 12));
  Q4T_CHECK(f.failure == GrammarFailure::kResource && !f.model.Ready());
  Q4T_CHECK(f.control.counters->started == 0 && f.Clean());
  return true;
}

Q4T_TEST(vad_assets_dead_or_wrong_profile_worker_never_publishes_weights) {
  Fixture f;
  for (const char* entry :
       {"--model-asset-fixture-crash", "--model-asset-fixture-profile"}) {
    detail::SetModelAssetWorkerForTesting(entry);
    Q4T_CHECK(!f.Load(f.Source()) && !f.model.Ready() && f.Clean());
    Q4T_CHECK(f.failure != GrammarFailure::kNone);
  }
  detail::SetModelAssetWorkerForTesting({});
  Q4T_CHECK(f.Load(f.Source()) && f.model.Ready() && f.Clean());
  return true;
}
