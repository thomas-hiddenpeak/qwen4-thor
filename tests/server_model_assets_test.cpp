#include "q4t/server/asr_assets.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/model_asset_io.h"
#include "../src/server/model_assets_protocol.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/test.h"
#include "q4t/trace/sha256.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
constexpr auto kProfile = detail::ModelAssetProfile::kAsr;

fs::path Assets() {
  const char* env = std::getenv("Q4T_ASR_ASSETS");
  return fs::absolute(env ? env
                          : ".q4t-work/serve-audio-native-20261117/assets-01");
}
struct Fixture {
  fs::path directory;
  GrammarControl control;
  GrammarFailure failure = GrammarFailure::kNone;
  AsrHostAssets output;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const fs::path root = env ? env : ".q4t-work/model-asset-tests";
    fs::create_directories(root);
    auto path = (fs::absolute(root) / "asr-XXXXXX").string();
    const char* made = mkdtemp(path.data());
    if (!made) throw std::runtime_error("ASR fixture directory");
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
    return !LoadAsrAssets(directory, &output, control, &failure, weights) &&
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

int ModelAssetFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const std::string_view entry(argv[1]);
  if (!entry.starts_with("--model-asset-fixture-") &&
      entry != "--grammar-fixture-silent")
    return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  if (entry.ends_with("silent")) {
    for (;;) pause();
  }
  if (entry.ends_with("crash")) {
    raise(SIGKILL);
    return 3;
  }
  asset_detail::Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.path_bytes > 4096 || request.include_weights)
    return 4;
  std::string path(request.path_bytes, '\0');
  if (!detail::WorkerTransfer(path.data(), path.size(), false)) return 4;
  const auto specs = detail::ModelAssetCatalog(kProfile);
  size_t total = 0;
  for (const auto& spec : specs.first(specs.size() - 1)) total += spec.bytes;
  asset_detail::Reply reply{asset_detail::kMagic, request.profile, 0,
                            specs.size() - 1, total};
  if (entry.ends_with("magic")) reply.magic ^= 1;
  if (entry.ends_with("profile")) reply.profile = 1;
  if (entry.ends_with("count")) ++reply.file_count;
  if (entry.ends_with("size")) ++reply.total_bytes;
  if (!detail::WorkerTransfer(&reply, sizeof(reply), true)) return 5;
  if (entry.ends_with("magic") || entry.ends_with("profile") ||
      entry.ends_with("count") || entry.ends_with("size"))
    return 0;
  for (const auto& spec : specs.first(specs.size() - 1)) {
    std::ifstream source(fs::path(path) / spec.name, std::ios::binary);
    std::string bytes(spec.bytes, '\0');
    source.read(bytes.data(), bytes.size());
    if (!source) return 6;
    const size_t count = entry.ends_with("partial") ? 1 : bytes.size();
    if (!detail::WorkerTransfer(bytes.data(), count, true)) return 6;
    if (entry.ends_with("partial")) return 0;
  }
  if (entry.ends_with("trailing")) {
    char extra = 'x';
    (void)detail::WorkerTransfer(&extra, 1, true);
  }
  return entry.ends_with("nonzero") ? 7 : 0;
}

Q4T_TEST(asr_assets_metadata_only_uses_trained_tokenizer_and_ignores_weights) {
  Fixture f;
  f.Metadata();
  Q4T_CHECK(!mkfifo((f.directory / "model.safetensors").c_str(), 0600));
  Q4T_CHECK(
      LoadAsrAssets(f.directory, &f.output, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.tokenizer);
  Q4T_CHECK(f.output.file_bytes.empty() && f.output.WeightBytes().empty() &&
            f.output.layout.tensors.empty());
  std::vector<uint32_t> ids;
  Q4T_CHECK(f.output.tokenizer->Encode("!", &ids));
  Q4T_CHECK(ids == std::vector<uint32_t>{0});  // No implicit embedding EOS.
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1 && f.control.counters->active == 0);
  const auto* saved = f.output.tokenizer.get();
  Q4T_CHECK(
      !LoadAsrAssets(f.directory, &f.output, f.control, &f.failure, true));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(f.output.tokenizer.get() == saved && f.output.file_bytes.empty());
  return true;
}

Q4T_TEST(asr_assets_each_metadata_identity_rejects_same_size_corruption) {
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

Q4T_TEST(asr_assets_bad_metadata_prevents_large_weight_staging) {
  Fixture f;
  for (const auto& spec : detail::ModelAssetCatalog(kProfile))
    Q4T_CHECK(f.Size(spec.name, spec.bytes));
  Q4T_CHECK(f.Rejected(true) && f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1);
  return true;
}

Q4T_TEST(asr_assets_preflight_cancel_deadline_and_capacity_start_no_child) {
  Fixture f;
  for (const auto& path : std::vector<std::string>{
           "", "relative", "/", "/tmp/../x", "/tmp//x", "/tmp/x/",
           std::string("/tmp/x\0z", 8), "/" + std::string(4096, 'x')}) {
    Q4T_CHECK(!LoadAsrAssets(path, &f.output, f.control, &f.failure, false));
    Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  }
  Q4T_CHECK(!LoadAsrAssets(f.directory, nullptr, f.control, &f.failure));
  Q4T_CHECK(
      !LoadAsrAssets(f.directory, &f.output, f.control, &f.failure, false, 1));
  Q4T_CHECK(f.failure == GrammarFailure::kResource);
  Q4T_CHECK(!LoadAsrAssets(f.directory, &f.output, f.control, &f.failure, true,
                           kNativeAsrFileBytes));
  Q4T_CHECK(f.failure == GrammarFailure::kResource);
  f.control.cancelled = [] { return true; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  f.control.cancelled = {};
  f.control.compile_deadline = Clock::now() - 1ms;
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  Q4T_CHECK(f.control.counters->started == 0);
  return true;
}

Q4T_TEST(asr_assets_file_types_lengths_and_parent_symlinks_rejected) {
  Fixture f;
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  const auto config = f.directory / "config.json";
  Q4T_CHECK(!mkfifo(config.c_str(), 0600));
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  fs::remove(config);
  fs::create_symlink(Assets() / "config.json", config);
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  fs::remove(config);
  Q4T_CHECK(f.Size("config.json", 6192));
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  fs::remove(config);
  fs::create_directory(config);
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kDependency);
  const auto link = f.directory / "linked";
  fs::create_directory_symlink(Assets().parent_path(), link);
  Q4T_CHECK(!LoadAsrAssets((link / Assets().filename()).string(), &f.output,
                           f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kDependency);
  Q4T_CHECK(f.control.counters->started == 6 &&
            f.control.counters->reaped == 6);
  return true;
}

Q4T_TEST(asr_assets_protocol_profile_sizes_and_truncation_fail_closed) {
  Fixture f;
  f.Metadata();
  for (const char* suffix : {"magic", "profile", "count", "size", "partial",
                             "nonzero", "trailing", "crash"}) {
    detail::SetModelAssetWorkerForTesting(
        std::string("--model-asset-fixture-") + suffix);
    Q4T_CHECK(f.Rejected());
    Q4T_CHECK(f.failure != GrammarFailure::kNone &&
              f.failure != GrammarFailure::kTimeout);
    Q4T_CHECK(f.control.counters->active == 0);
  }
  Q4T_CHECK(f.control.counters->started == 8 &&
            f.control.counters->reaped == 8);
  return true;
}

Q4T_TEST(model_assets_wait_for_live_reader_then_load_verified_metadata) {
  Fixture f;
  f.Metadata();
  GrammarControl holder;
  holder.domain = IsolatedWorkerDomain::kModelAssets;
  std::unique_ptr<detail::GrammarProcess> active;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", holder,
                                          &active));
  std::jthread release([worker = std::move(active)]() mutable {
    std::this_thread::sleep_for(80ms);
    worker.reset();
  });
  Q4T_CHECK(LoadAsrAssets(f.directory, &f.output, f.control, &f.failure,
                          false));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.tokenizer);
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1);
  return true;
}

Q4T_TEST(model_assets_wait_obeys_cancel_and_deadline_without_spawning) {
  Fixture f;
  GrammarControl holder;
  holder.domain = IsolatedWorkerDomain::kModelAssets;
  std::unique_ptr<detail::GrammarProcess> active;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", holder,
                                          &active));
  f.control.compile_deadline = Clock::now() + 80ms;
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  f.control.compile_deadline = Clock::now() + 2s;
  const auto cancel_at = Clock::now() + 30ms;
  f.control.cancelled = [cancel_at] { return Clock::now() >= cancel_at; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  Q4T_CHECK(f.control.counters->started == 0 &&
            f.control.counters->active == 0);
  return true;
}

Q4T_TEST(asr_assets_timeout_shares_embedding_slot_until_reaped) {
  Fixture f;
  detail::SetModelAssetWorkerForTesting("--model-asset-fixture-silent");
  detail::DeferGrammarReapingForTesting(true);
  f.control.compile_deadline = Clock::now() + 80ms;
  const auto start = Clock::now();
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kTimeout);
  Q4T_CHECK(Clock::now() - start < 500ms);
  Q4T_CHECK(detail::IsolatedCleanupPending(IsolatedWorkerDomain::kModelAssets));
  f.control.compile_deadline = Clock::now() + 2s;
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kUnavailable);
  EmbeddingHostAssets embedding;
  Q4T_CHECK(!LoadEmbeddingAssets(f.directory, &embedding, f.control, &f.failure,
                                 false));
  Q4T_CHECK(f.failure == GrammarFailure::kUnavailable);
  Q4T_CHECK(f.control.counters->started == 1);
  GrammarControl ordinary;
  std::unique_ptr<detail::GrammarProcess> other;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", ordinary,
                                          &other));
  detail::DeferGrammarReapingForTesting(false);
  other->Stop();
  return true;
}

Q4T_TEST(asr_assets_inflight_cancel_reaps_then_next_load_recovers) {
  Fixture f;
  detail::SetModelAssetWorkerForTesting("--model-asset-fixture-silent");
  const auto cancel_at = Clock::now() + 50ms;
  f.control.cancelled = [=] { return Clock::now() >= cancel_at; };
  Q4T_CHECK(f.Rejected() && f.failure == GrammarFailure::kCancelled);
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1);
  f.control.cancelled = {};
  detail::SetModelAssetWorkerForTesting({});
  f.Metadata();
  Q4T_CHECK(
      LoadAsrAssets(f.directory, &f.output, f.control, &f.failure, false));
  Q4T_CHECK(f.output.tokenizer && f.failure == GrammarFailure::kNone);
  Q4T_CHECK(f.control.counters->started == 2 &&
            f.control.counters->reaped == 2);
  return true;
}

Q4T_TEST(
    model_assets_metadata_validation_and_late_cancel_preserve_destination) {
  Fixture f;
  f.Metadata();
  bool visited = false;
  std::vector<uint8_t> weights{42};
  const auto reject = [&](std::span<const std::string> metadata) {
    visited = metadata.size() == 6 && metadata[0].size() == 6193;
    return q4t::Status::Fail("injected architecture mismatch");
  };
  Q4T_CHECK(!detail::ReadVerifiedModelAssets(
      kProfile, f.directory, reject, &weights, f.control, &f.failure, false));
  Q4T_CHECK(visited && f.failure == GrammarFailure::kDependency &&
            weights == std::vector<uint8_t>{42});
  bool cancelled = false;
  f.control.cancelled = [&] { return cancelled; };
  const auto cancel = [&](std::span<const std::string>) {
    cancelled = true;
    return q4t::Status{};
  };
  Q4T_CHECK(!detail::ReadVerifiedModelAssets(
      kProfile, f.directory, cancel, &weights, f.control, &f.failure, false));
  Q4T_CHECK(f.failure == GrammarFailure::kCancelled &&
            weights == std::vector<uint8_t>{42});
  Q4T_CHECK(
      detail::ModelAssetCatalog(static_cast<detail::ModelAssetProfile>(99))
          .empty());
  return true;
}

Q4T_TEST(model_assets_chunked_hash_matches_and_interrupts_without_publication) {
  std::vector<uint8_t> bytes(3 * 65536 + 17);
  for (size_t i = 0; i < bytes.size(); ++i) bytes[i] = i * 13;
  q4t::trace::Digest digest{};
  Q4T_CHECK(q4t::trace::HashBytes(bytes, &digest));
  Q4T_CHECK(digest == q4t::trace::Sha256(bytes));
  const auto saved = digest;
  int checkpoints = 0;
  Q4T_CHECK(!q4t::trace::HashBytes(bytes, &digest,
                                   [&] { return ++checkpoints == 2; }));
  Q4T_CHECK(checkpoints == 2 && digest == saved);
  checkpoints = 0;
  Q4T_CHECK(!q4t::trace::HashBytes(bytes, &digest,
                                   [&] { return ++checkpoints == 5; }));
  Q4T_CHECK(checkpoints == 5 && digest == saved);
  Q4T_CHECK(!q4t::trace::HashBytes({}, &digest, [] { return true; }));
  Q4T_CHECK(digest == saved);
  Q4T_CHECK(q4t::trace::HashBytes({}, &digest));
  Q4T_CHECK(digest == q4t::trace::Sha256({}));
  return true;
}

Q4T_TEST(asr_assets_full_checkpoint_verified_layout_and_metadata_published) {
  Fixture f;
  f.control.compile_deadline = Clock::now() + 60s;
  Q4T_CHECK(LoadAsrAssets(Assets(), &f.output, f.control, &f.failure, true));
  Q4T_CHECK(f.failure == GrammarFailure::kNone && f.output.tokenizer);
  Q4T_CHECK(f.output.file_bytes.size() == kNativeAsrFileBytes);
  Q4T_CHECK(f.output.data_offset == 74552);
  Q4T_CHECK(f.output.WeightBytes().size() == q4t::audio::AsrWeightBytes());
  Q4T_CHECK(f.output.layout.tensors.size() == 612);
  Q4T_CHECK(q4t::audio::ValidateAsrWeightLayout(f.output.layout));
  Q4T_CHECK(f.control.counters->started == 1 &&
            f.control.counters->reaped == 1 && f.control.counters->active == 0);
  return true;
}

namespace {
struct DirectAssetFixture {
  Fixture owner;
  std::vector<uint8_t> source;
  int fd = -1;
  explicit DirectAssetFixture(size_t bytes = 2 * detail::kModelReadBlockBytes +
                                             37)
      : source(bytes) {
    for (size_t i = 0; i < bytes; ++i) source[i] = uint8_t(i * 31 + 7);
    const auto path = owner.directory / "direct.bin";
    const int output = open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (output < 0) throw std::runtime_error("direct fixture creation");
    const bool written = write(output, source.data(), source.size()) ==
                         static_cast<ssize_t>(source.size());
    const bool synced = fsync(output) == 0;
    const bool dropped = posix_fadvise(output, 0, 0, POSIX_FADV_DONTNEED) == 0;
    close(output);
    if (!written || !synced || !dropped)
      throw std::runtime_error("direct fixture write/drop");
    fd = open(path.c_str(), O_RDONLY | O_DIRECT | O_CLOEXEC);
    if (fd < 0) throw std::runtime_error("direct fixture read open");
  }
  ~DirectAssetFixture() {
    if (fd >= 0) close(fd);
  }
  bool Read(std::vector<uint8_t>* target, size_t bytes = 0) {
    return detail::StreamDirectModelAsset(
        fd, bytes ? bytes : source.size(),
        [&](const uint8_t* data, size_t count) {
          target->insert(target->end(), data, data + count);
          return true;
        });
  }
};
}  // namespace

Q4T_TEST(model_assets_direct_io_preserves_blocks_and_unaligned_eof) {
  DirectAssetFixture fixture;
  Q4T_CHECK(detail::DirectModelAssetReady(fixture.fd));
  std::vector<uint8_t> observed;
  Q4T_CHECK(fixture.Read(&observed) && observed == fixture.source);
  struct stat st {};
  Q4T_CHECK(!fstat(fixture.fd, &st) &&
            st.st_size == static_cast<off_t>(fixture.source.size()));
  return true;
}

Q4T_TEST(model_assets_direct_io_refuses_buffered_descriptor) {
  DirectAssetFixture fixture;
  const int fd = open((fixture.owner.directory / "direct.bin").c_str(),
                      O_RDONLY | O_CLOEXEC);
  Q4T_CHECK(fd >= 0);
  size_t calls = 0;
  const bool read = detail::StreamDirectModelAsset(fd, fixture.source.size(),
                                                   [&](const uint8_t*, size_t) {
                                                     ++calls;
                                                     return true;
                                                   });
  close(fd);
  Q4T_CHECK(!read && !calls);
  return true;
}

Q4T_TEST(model_assets_direct_io_short_source_never_completes) {
  DirectAssetFixture fixture;
  std::vector<uint8_t> observed;
  Q4T_CHECK(!fixture.Read(&observed, fixture.source.size() + 1));
  Q4T_CHECK(observed.size() == 2 * detail::kModelReadBlockBytes);
  return true;
}

Q4T_TEST(model_assets_direct_io_sink_cancellation_stops_at_one_block) {
  DirectAssetFixture fixture;
  size_t bytes = 0;
  Q4T_CHECK(!detail::StreamDirectModelAsset(fixture.fd, fixture.source.size(),
                                            [&](const uint8_t*, size_t count) {
                                              bytes += count;
                                              return false;
                                            }));
  Q4T_CHECK(bytes == detail::kModelReadBlockBytes);
  return true;
}

Q4T_TEST(model_assets_direct_io_does_not_populate_cold_file_pages) {
  DirectAssetFixture fixture;
  const auto bytes = fixture.source.size();
  const long page = sysconf(_SC_PAGESIZE);
  Q4T_CHECK(page > 0);
  std::vector<uint8_t> pages((bytes + page - 1) / page);
  void* mapping = mmap(nullptr, bytes, PROT_NONE, MAP_PRIVATE, fixture.fd, 0);
  Q4T_CHECK(mapping != MAP_FAILED);
  const auto cold = [&] {
    if (mincore(mapping, bytes, pages.data())) return false;
    for (auto value : pages)
      if (value & 1) return false;
    return true;
  };
  const bool before = cold();
  std::vector<uint8_t> observed;
  const bool read = fixture.Read(&observed);
  const bool after = cold();
  munmap(mapping, bytes);
  Q4T_CHECK(before && read && after && observed == fixture.source);
  return true;
}
