#include "q4t/server/model_assets.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <new>

#include "model_assets_protocol.h"
#include "model_asset_io.h"
#include "q4t/audio/vad.h"
#include "q4t/server/aligner_assets.h"
#include "q4t/server/asr_assets.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/server/speaker_assets.h"
#include "q4t/server/separator_assets.h"
#include "q4t/server/tts_assets.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
using asset_detail::kMagic;
using asset_detail::Reply;
using asset_detail::Request;
using detail::ModelAssetProfile;
using detail::ModelAssetSpec;
constexpr ModelAssetSpec kEmbedding[]{
    {"config.json", 727,
     "b5bf1f51fc45be473a54718cef92448d90a1be001bf9b9a44b8c7f10a19feaa9"},
    {"tokenizer.json", 11423705, kNativeEmbeddingTokenizerSha256},
    {"model.safetensors", kNativeEmbeddingFileBytes,
     kNativeEmbeddingWeightSha256}};
constexpr ModelAssetSpec kAsr[]{
    {"config.json", 6193,
     "76d3ae4601ce939830b2517f4a6cadb86cc51316c3900af6b020b051c21a478c"},
    {"preprocessor_config.json", 330,
     "45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d"},
    {"generation_config.json", 142,
     "1da527824d81e07118facff437e03f2e24a23311e3bdeb2368973fe77e5f275c"},
    {"vocab.json", 2776833,
     "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"},
    {"merges.txt", 1671853,
     "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"},
    {"tokenizer_config.json", 12487,
     "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c"},
    {"model.safetensors", kNativeAsrFileBytes, kNativeAsrWeightSha256}};
constexpr ModelAssetSpec kAligner[]{
    {"config.json", 5982,
     "d616c65d46c4b90bdc651b0a0963ea932732241140f337f9bb6b0335a9c8ef09"},
    {"preprocessor_config.json", 330,
     "45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d"},
    {"generation_config.json", 115,
     "948d089b23bca1d214e768d59c4438365665f52ec6d33678f4062206b3fbbb8c"},
    {"vocab.json", 2776833,
     "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"},
    {"merges.txt", 1671853,
     "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"},
    {"tokenizer_config.json", 12666,
     "3ab80063f8511deb9566e6ad438d17b7a6277fcffd52d92854112f19d36bd81c"},
    {"model.safetensors", kNativeAlignerFileBytes, kNativeAlignerWeightSha256}};
constexpr ModelAssetSpec kVad[]{{"silero_vad_16k.safetensors",
                                 audio::kVadFileBytes,
                                 audio::kVadWeightSha256}};
constexpr ModelAssetSpec kSpeaker[]{
    {"config.json", kNativeSpeakerConfigBytes, kNativeSpeakerConfigSha256},
    {"model.safetensors", audio::SpeakerConfig::kFileBytes,
     audio::SpeakerConfig::kWeightSha256}};
constexpr ModelAssetSpec kSeparator[]{
    {"config.json", kNativeSeparatorConfigBytes, kNativeSeparatorConfigSha256},
    {"model.safetensors", audio::SeparatorConfig::kFileBytes,
     audio::SeparatorConfig::kWeightSha256}};
constexpr ModelAssetSpec kTts[]{
    {"config.json", 4908,
     "17a07f527a1c25ea30b4e023a184482a23d3e279d697b1dc81b1bde498d29cf9"},
    {"preprocessor_config.json", 127,
     "efdde1022ea9d76928bf7a9cd53139138f5ba2e466e837f08f6105ab1af1c119"},
    {"generation_config.json", 245,
     "f1b90b4513f3b34c62851049e2492d7b4c5940daf1276f89c82b8ef04127f3aa"},
    {"vocab.json", 2776833,
     "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"},
    {"merges.txt", 1671839,
     "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3"},
    {"tokenizer_config.json", 7344,
     "dc3c31c3bdaedd5016382bb3cbe07323026775ad51f5a4fb564505992ae4a670"},
    {"model.safetensors", kNativeTtsFileBytes, kNativeTtsWeightSha256}};
constexpr ModelAssetSpec kTtsBase[]{
    {"config.json", 4494,
     "b4f01752d15a488abde3e1ab44723ae4f4b9e68a4037257b098b3737893cc1f9"},
    kTts[1],
    kTts[2],
    kTts[3],
    kTts[4],
    kTts[5],
    {"model.safetensors", kNativeTtsBaseFileBytes, kNativeTtsBaseWeightSha256}};
constexpr ModelAssetSpec kTtsCodec[]{
    {"config.json", 2336,
     "ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167"},
    {"preprocessor_config.json", 234,
     "fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb"},
    {"configuration.json", 76,
     "6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd"},
    {"model.safetensors", kNativeTtsCodecFileBytes,
     kNativeTtsCodecWeightSha256}};
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif

Status Fail(GrammarFailure kind, const char* message, GrammarFailure* failure) {
  if (failure) *failure = kind;
  return Status::Fail(message);
}

// Child only. Reject symlinks in every path component, not only the leaf.
int OpenDirectory(std::string_view path) {
  int fd = open("/", O_DIRECTORY | O_RDONLY | O_CLOEXEC);
  for (size_t start = 1; fd >= 0 && start < path.size();) {
    const auto end = path.find('/', start);
    const std::string part(
        path.substr(start, end == path.npos ? end : end - start));
    const int next =
        openat(fd, part.c_str(),
               O_DIRECTORY | O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    close(fd);
    fd = next;
    if (end == path.npos) break;
    start = end + 1;
  }
  return fd;
}
int OpenFile(int directory, const ModelAssetSpec& spec, bool direct) {
  const int fd = openat(
      directory, spec.name,
      O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK | (direct ? O_DIRECT : 0));
  struct stat st {};
  if (fd < 0) return -1;
  if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
      static_cast<uint64_t>(st.st_size) != spec.bytes ||
      (direct && !detail::DirectModelAssetReady(fd))) {
    close(fd);
    return -1;
  }
  return fd;
}
bool StreamFile(int fd, size_t bytes) {
  std::array<uint8_t, 256 * 1024> buffer;
  size_t offset = 0;
  while (offset < bytes) {
    const auto count = std::min(bytes - offset, buffer.size());
    ssize_t result;
    do {
      result = pread(fd, buffer.data(), count, offset);
    } while (result < 0 && errno == EINTR);
    if (result <= 0 || static_cast<size_t>(result) > count ||
        !detail::WorkerTransfer(buffer.data(), result, true))
      return false;
    offset += result;
  }
  // Advisory only. File pages remain part of unified-RAM admission estimates.
  (void)posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
  return true;
}
}  // namespace

namespace detail {
std::span<const ModelAssetSpec> ModelAssetCatalog(ModelAssetProfile profile) {
  switch (profile) {
    case ModelAssetProfile::kEmbedding:
      return kEmbedding;
    case ModelAssetProfile::kAsr:
      return kAsr;
    case ModelAssetProfile::kAligner:
      return kAligner;
    case ModelAssetProfile::kVad:
      return kVad;
    case ModelAssetProfile::kSpeaker:
      return kSpeaker;
    case ModelAssetProfile::kSeparator:
      return kSeparator;
    case ModelAssetProfile::kTtsCustomVoice17B:
      return kTts;
    case ModelAssetProfile::kTtsCodec:
      return kTtsCodec;
    case ModelAssetProfile::kTtsBase17B:
      return kTtsBase;
  }
  return {};
}
bool ValidModelAssetDirectory(std::string_view path) {
  if (path.size() < 2 || path.size() > 4096 || path.front() != '/' ||
      path.back() == '/' || path.find('\0') != path.npos)
    return false;
  for (size_t start = 1; start < path.size();) {
    const auto end = path.find('/', start);
    const auto part = path.substr(start, end == path.npos ? end : end - start);
    if (part.empty() || part == "." || part == "..") return false;
    if (end == path.npos) break;
    start = end + 1;
  }
  return true;
}
Status CheckModelAssetControl(const GrammarControl& control,
                              GrammarFailure* failure) {
  if (control.cancelled && control.cancelled())
    return Fail(GrammarFailure::kCancelled, "model asset load cancelled",
                failure);
  if (Clock::now() >= control.compile_deadline)
    return Fail(GrammarFailure::kTimeout, "model asset load deadline", failure);
  return {};
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetModelAssetWorkerForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif

Status ReadVerifiedModelAssets(ModelAssetProfile profile,
                               const std::string& directory,
                               const ModelMetadataValidator& validate,
                               std::vector<uint8_t>* weights,
                               GrammarControl control, GrammarFailure* failure,
                               bool include_weights, size_t max_staging_bytes) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    return Fail(kind, message, failure);
  };
  auto specs = ModelAssetCatalog(profile);
  if (!weights || !validate || specs.empty() ||
      !ValidModelAssetDirectory(directory))
    return fail(GrammarFailure::kDependency, "invalid model asset request");
  if (!include_weights) specs = specs.first(specs.size() - 1);
  size_t total = 0;
  for (const auto& spec : specs) {
    if (spec.bytes > max_staging_bytes - total)
      return fail(GrammarFailure::kResource, "model asset staging limit");
    total += spec.bytes;
  }
  control.domain = IsolatedWorkerDomain::kModelAssets;
  control.compile_deadline =
      std::min(control.compile_deadline,
               Clock::now() + std::chrono::milliseconds(kModelAssetTimeoutMs));
  if (auto s = CheckModelAssetControl(control, failure); !s) return s;
  std::unique_ptr<GrammarProcess> process;
  GrammarFailure cause;
  const char* entry = "--model-asset-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!GrammarProcess::Start(entry, control, &process, &cause))
    return fail(cause, "model asset worker unavailable");
  const auto transfer = [&](void* data, size_t bytes, bool write) {
    return process->Transfer(data, bytes, write, control.compile_deadline,
                             control.cancelled);
  };
  const auto verify = [&](std::span<const uint8_t> data, const char* expected) {
    trace::Digest digest;
    const auto checked = trace::HashBytes(data, &digest, [&] {
      return !CheckModelAssetControl(control, &cause);
    });
    if (!checked) return fail(cause, "model asset hashing interrupted");
    if (trace::HexDigest(digest) != expected)
      return fail(GrammarFailure::kDependency, "model asset SHA256 differs");
    return Status{};
  };
  try {
    Request request{kMagic, static_cast<uint64_t>(profile), directory.size(),
                    include_weights ? 1UL : 0UL};
    Reply reply;
    if (!transfer(&request, sizeof(request), true) ||
        !transfer(const_cast<char*>(directory.data()), directory.size(),
                  true) ||
        !transfer(&reply, sizeof(reply), false))
      return fail(process->Failure(), "model asset worker transfer failed");
    if (reply.magic != kMagic || reply.profile != request.profile ||
        reply.code != 0 || reply.file_count != specs.size() ||
        reply.total_bytes != total)
      return fail(GrammarFailure::kDependency,
                  "model assets absent or invalid");
    const size_t metadata_count = specs.size() - (include_weights ? 1 : 0);
    std::vector<std::string> metadata(metadata_count);
    for (size_t i = 0; i < metadata_count; ++i) {
      auto& bytes = metadata[i];
      bytes.resize(specs[i].bytes);
      if (!transfer(bytes.data(), bytes.size(), false))
        return fail(process->Failure(), "model metadata transfer failed");
      if (auto s = verify(
              {reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size()},
              specs[i].sha256);
          !s)
        return s;
    }
    const auto validated = validate(metadata);
    if (auto s = CheckModelAssetControl(control, failure); !s) return s;
    if (!validated && failure && *failure != GrammarFailure::kNone)
      return validated;
    if (!validated)
      return fail(GrammarFailure::kDependency,
                  "model metadata contract differs");
    std::vector<uint8_t> next;
    if (include_weights) {
      next.resize(specs.back().bytes);
      if (!transfer(next.data(), next.size(), false))
        return fail(process->Failure(), "model weight transfer failed");
    }
    std::string trailing;
    if (!process->CollectOutput(&trailing, 0, control.compile_deadline,
                                control.cancelled))
      return fail(process->Failure(), "model worker completion unconfirmed");
    if (include_weights)
      if (auto s = verify(next, specs.back().sha256); !s) return s;
    if (auto s = CheckModelAssetControl(control, failure); !s) return s;
    *weights = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource,
                "model asset host staging exhausted");
  }
}
}  // namespace detail

int MaybeRunModelAssetWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--model-asset-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    Request request;
    if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
        request.magic != kMagic || request.path_bytes < 2 ||
        request.path_bytes > 4096 || request.include_weights > 1)
      return 3;
    auto specs = detail::ModelAssetCatalog(
        static_cast<ModelAssetProfile>(request.profile));
    if (specs.empty()) return 3;
    if (!request.include_weights) specs = specs.first(specs.size() - 1);
    std::string path(request.path_bytes, '\0');
    if (!detail::WorkerTransfer(path.data(), path.size(), false) ||
        !detail::ValidModelAssetDirectory(path))
      return 3;
    const int directory = OpenDirectory(path);
    std::array<int, 7> fds;
    fds.fill(-1);
    if (specs.size() > fds.size()) {
      if (directory >= 0) close(directory);
      return 3;
    }
    bool opened = directory >= 0;
    size_t total = 0;
    for (size_t i = 0; i < specs.size(); ++i) {
      total += specs[i].bytes;
      if (directory >= 0)
        fds[i] = OpenFile(directory, specs[i],
                          request.include_weights && i + 1 == specs.size());
      opened = opened && fds[i] >= 0;
    }
    if (directory >= 0) close(directory);
    Reply reply{kMagic, request.profile, opened ? 0UL : 1UL, specs.size(),
                total};
    bool ok = detail::WorkerTransfer(&reply, sizeof(reply), true);
    if (ok && opened)
      for (size_t i = 0; ok && i < specs.size(); ++i) {
        if (request.include_weights && i + 1 == specs.size())
          ok = detail::StreamDirectModelAsset(
              fds[i], specs[i].bytes, [](uint8_t* data, size_t bytes) {
                return detail::WorkerTransfer(data, bytes, true);
              });
        else
          ok = StreamFile(fds[i], specs[i].bytes);
      }
    for (const int fd : fds)
      if (fd >= 0) close(fd);
    return ok ? 0 : 4;
  } catch (...) {
    return 5;
  }
}
}  // namespace q4t::server
