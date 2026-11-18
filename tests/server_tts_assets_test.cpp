#include "q4t/server/tts_assets.h"

#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "q4t/server/asr_assets.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
constexpr auto kTalker = detail::ModelAssetProfile::kTtsCustomVoice17B;
constexpr auto kCodec = detail::ModelAssetProfile::kTtsCodec;

fs::path Assets() {
  const auto* env = std::getenv("Q4T_TTS_ASSETS");
  if (!env) throw std::runtime_error("Q4T_TTS_ASSETS required");
  return fs::absolute(env);
}
struct Fixture {
  fs::path directory;
  GrammarControl control;
  GrammarFailure failure = GrammarFailure::kNone;
  TtsHostAssets output;
  Fixture() {
    const auto* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const fs::path root = env ? env : ".q4t-work/tts-assets-tests";
    fs::create_directories(root);
    auto path = (fs::absolute(root) / "tts-XXXXXX").string();
    const auto* made = mkdtemp(path.data());
    if (!made) throw std::runtime_error("TTS fixture directory");
    directory = made;
    fs::create_directory(directory / "speech_tokenizer");
    control.counters = std::make_shared<GrammarWorkerCounters>();
    control.compile_deadline = Clock::now() + 20s;
    output.talker.bytes = {41};
    output.codec.bytes = {42};
  }
  ~Fixture() {
    detail::SetModelAssetWorkerForTesting({});
    const auto end = Clock::now() + 1s;
    while (detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets) &&
           Clock::now() < end)
      std::this_thread::sleep_for(1ms);
    fs::remove_all(directory);
  }
  void Metadata() {
    for (auto profile : {kTalker, kCodec}) {
      const auto sub = profile == kTalker ? fs::path{} : "speech_tokenizer";
      const auto specs = detail::ModelAssetCatalog(profile);
      for (const auto& spec : specs.first(specs.size() - 1))
        fs::copy_file(Assets() / sub / spec.name, directory / sub / spec.name);
    }
  }
  bool Rejected() {
    return !LoadTtsAssets(directory, &output, control, &failure, false) &&
           output.talker.bytes == std::vector<uint8_t>{41} &&
           output.codec.bytes == std::vector<uint8_t>{42} && !output.tokenizer;
  }
  bool Reaped() const {
    return control.counters->started == control.counters->reaped &&
           control.counters->active == 0;
  }
};
void Corrupt(const fs::path& path) {
  fs::permissions(path, fs::perms::owner_write, fs::perm_options::add);
  std::fstream file(path, std::ios::in | std::ios::out | std::ios::binary);
  char c;
  file.read(&c, 1);
  c ^= 1;
  file.seekp(0);
  file.write(&c, 1);
  if (!file) throw std::runtime_error("TTS fixture corruption failed");
}
}  // namespace

Q4T_TEST(tts_assets_metadata_publishes_both_profiles_without_opening_weights) {
  Fixture f;
  f.Metadata();
  for (auto sub : {"", "speech_tokenizer"})
    Q4T_CHECK(!mkfifo((f.directory / sub / "model.safetensors").c_str(), 0600));
  Q4T_CHECK(
      LoadTtsAssets(f.directory, &f.output, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.tokenizer);
  Q4T_CHECK(f.output.talker.bytes.empty() && f.output.codec.bytes.empty());
  Q4T_CHECK(f.output.tokenizer->VocabSize() == 151676 &&
            f.output.tokenizer->AddedTokenCount() == 33);
  std::vector<uint32_t> ids;
  Q4T_CHECK(f.output.tokenizer->Encode("<|audio_pad|>", &ids));
  Q4T_CHECK(ids == std::vector<uint32_t>{151675});
  Q4T_CHECK(f.Reaped() && f.control.counters->started == 2);
  const auto* saved = f.output.tokenizer.get();
  Q4T_CHECK(
      !LoadTtsAssets(f.directory, &f.output, f.control, &f.failure, true));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency &&
            f.output.tokenizer.get() == saved && f.output.talker.bytes.empty());
  Q4T_CHECK(f.Reaped());
  return true;
}

Q4T_TEST(tts_assets_each_metadata_digest_rejects_corruption_atomically) {
  Fixture f;
  f.Metadata();
  for (auto profile : {kTalker, kCodec}) {
    const auto sub = profile == kTalker ? fs::path{} : "speech_tokenizer";
    const auto specs = detail::ModelAssetCatalog(profile);
    for (const auto& spec : specs.first(specs.size() - 1)) {
      const auto path = f.directory / sub / spec.name;
      Corrupt(path);
      Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
      Corrupt(path);
      Q4T_CHECK(f.Reaped());
    }
  }
  Q4T_CHECK(f.control.counters->started == 12);
  return true;
}

Q4T_TEST(tts_assets_combined_budget_cancel_and_deadline_start_no_child) {
  Fixture f;
  size_t total = 0, metadata = 0;
  for (auto profile : {kTalker, kCodec}) {
    const auto specs = detail::ModelAssetCatalog(profile);
    for (const auto& spec : specs) total += spec.bytes;
    for (const auto& spec : specs.first(specs.size() - 1))
      metadata += spec.bytes;
  }
  Q4T_CHECK(total > (4ULL << 30) && total < kTtsAssetStagingBytes);
  for (bool weights : {false, true}) {
    Q4T_CHECK(!LoadTtsAssets(f.directory, &f.output, f.control, &f.failure,
                             weights, (weights ? total : metadata) - 1));
    Q4T_CHECK(f.failure == GrammarFailure::kResource);
  }
  for (const auto& path : {"relative", "/a/../b", "/a//b", "/a/"})
    Q4T_CHECK(!LoadTtsAssets(path, &f.output, f.control, &f.failure, false));
  Q4T_CHECK(!LoadTtsAssets(f.directory, nullptr, f.control, &f.failure, false));
  f.control.cancelled = [] { return true; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  f.control.cancelled = {};
  f.control.compile_deadline = Clock::now() - 1s;
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  Q4T_CHECK(f.control.counters->started == 0 && f.Reaped());
  return true;
}

Q4T_TEST(tts_assets_codec_symlink_and_cross_profile_cannot_publish_talker) {
  Fixture f;
  f.Metadata();
  fs::remove_all(f.directory / "speech_tokenizer");
  fs::create_directory_symlink(Assets() / "speech_tokenizer",
                               f.directory / "speech_tokenizer");
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(f.control.counters->started == 2 && f.Reaped());
  AsrHostAssets asr;
  asr.file_bytes = {43};
  Q4T_CHECK(!LoadAsrAssets(f.directory, &asr, f.control, &f.failure, false));
  Q4T_CHECK(asr.file_bytes == std::vector<uint8_t>{43} && !asr.tokenizer);
  Q4T_CHECK(f.Reaped());
  return true;
}

Q4T_TEST(tts_assets_crash_hang_cancel_reap_and_allow_next_metadata_load) {
  Fixture f;
  f.Metadata();
  for (const auto* entry :
       {"--model-asset-fixture-crash", "--grammar-fixture-silent"}) {
    detail::SetModelAssetWorkerForTesting(entry);
    f.control.compile_deadline = Clock::now() + 100ms;
    Q4T_CHECK(f.Rejected() && f.Reaped());
    Q4T_CHECK(f.failure == (std::string_view(entry).ends_with("silent")
                                ? GrammarFailure::kTimeout
                                : GrammarFailure::kWorker));
  }
  f.control.compile_deadline = Clock::now() + 10s;
  const auto before = f.control.counters->started.load();
  f.control.cancelled = [&] { return f.control.counters->started > before; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  Q4T_CHECK(f.Reaped());
  f.control.cancelled = {};
  detail::SetModelAssetWorkerForTesting({});
  Q4T_CHECK(
      LoadTtsAssets(f.directory, &f.output, f.control, &f.failure, false));
  Q4T_CHECK(f.output.tokenizer && f.Reaped());
  return true;
}

// Explicit separate lease only, never included in the metadata CPU list.
Q4T_TEST(tts_assets_full_checkpoints_bind_trained_layouts) {
  Fixture f;
  f.control.compile_deadline = Clock::now() + 60s;
  Q4T_CHECK(LoadTtsAssets(Assets(), &f.output, f.control, &f.failure, true));
  Q4T_CHECK(f.output.talker.bytes.size() == kNativeTtsFileBytes &&
            f.output.codec.bytes.size() == kNativeTtsCodecFileBytes);
  Q4T_CHECK(f.output.talker.layout.bytes * 2 == kNativeTtsWeightBytes &&
            f.output.codec.layout.bytes ==
                q4t::audio::TtsConfig::kCodecDecoderBytes);
  Q4T_CHECK(q4t::audio::ValidateTtsWeightLayout(
      f.output.talker.layout, q4t::audio::TtsVariant::kCustomVoice17B));
  Q4T_CHECK(q4t::audio::ValidateTtsWeightLayout(
      f.output.codec.layout, q4t::audio::TtsVariant::kCustomVoice17B, true));
  Q4T_CHECK(f.output.talker.layout.tensors.size() == 404 &&
            f.output.codec.layout.tensors.size() == 271);
  Q4T_CHECK(f.Reaped() && f.control.counters->started == 2);
  return true;
}
