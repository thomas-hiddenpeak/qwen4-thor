#include "q4t/server/embedding_assets.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <new>

#include "q4t/model/embedding_config.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
constexpr uint64_t kMagic = 0x31414d4542543451ULL;
constexpr size_t kConfigBytes = 727;
constexpr size_t kTokenizerBytes = 11423705;
constexpr char kConfigDigest[] =
    "b5bf1f51fc45be473a54718cef92448d90a1be001bf9b9a44b8c7f10a19feaa9";
constexpr char kTokenizerDigest[] =
    "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a";
struct Request {
  uint64_t magic = kMagic;
  uint64_t path_bytes = 0;
};
struct Reply {
  uint64_t magic = kMagic;
  uint64_t code = 1;
  uint64_t config_bytes = 0;
  uint64_t tokenizer_bytes = 0;
  uint64_t weight_bytes = 0;
};
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif

bool ValidDirectory(const std::string& path) {
  if (path.size() < 2 || path.size() > 4096 || path.front() != '/' ||
      path.back() == '/' || path.find('\0') != path.npos) return false;
  for (size_t start = 1; start < path.size();) {
    const auto end = path.find('/', start);
    const auto part = path.substr(start, end == path.npos ? end : end - start);
    if (part.empty() || part == "." || part == "..") return false;
    if (end == path.npos) break;
    start = end + 1;
  }
  return true;
}

std::string Digest(std::span<const uint8_t> value) {
  return trace::HexDigest(trace::Sha256(value));
}
std::span<const uint8_t> Bytes(const std::string& text) {
  return {reinterpret_cast<const uint8_t*>(text.data()), text.size()};
}

// Only used in the child. O_NONBLOCK avoids opening a FIFO forever before
// fstat rejects it; filesystem stalls remain in this independently owned PID.
int OpenFile(int directory, const char* name, size_t bytes) {
  const int fd = openat(directory, name,
                        O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  struct stat st {};
  if (fd < 0) return -1;
  if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
      static_cast<uint64_t>(st.st_size) != bytes) {
    close(fd);
    return -1;
  }
  return fd;
}
bool StreamFile(int fd, size_t bytes) {
  std::array<uint8_t, 256 * 1024> buffer;
  size_t offset = 0;
  while (offset < bytes) {
    const size_t count = std::min(bytes - offset, buffer.size());
    ssize_t result;
    do {
      result = pread(fd, buffer.data(), count, offset);
    } while (result < 0 && errno == EINTR);
    if (result <= 0 || static_cast<size_t>(result) > count ||
        !detail::WorkerTransfer(buffer.data(), result, true)) return false;
    offset += result;
  }
  // Clean file pages are not an unreported persistent host weight cache.
  // Eviction is advisory; admission still budgets these unified-RAM pages.
  (void)posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
  return true;
}
}  // namespace

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetEmbeddingAssetWorkerForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif

Status LoadEmbeddingAssets(const std::string& directory,
                            EmbeddingHostAssets* out, GrammarControl control,
                            GrammarFailure* failure) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    if (failure) *failure = kind;
    return Status::Fail(message);
  };
  if (!out || !ValidDirectory(directory))
    return fail(GrammarFailure::kDependency, "invalid embedding asset directory");
  if (control.cancelled && control.cancelled())
    return fail(GrammarFailure::kCancelled, "embedding asset load cancelled");
  control.domain = IsolatedWorkerDomain::kEmbeddingAssets;
  control.compile_deadline = std::min(control.compile_deadline,
      Clock::now() + std::chrono::milliseconds(kEmbeddingAssetTimeoutMs));
  if (Clock::now() >= control.compile_deadline)
    return fail(GrammarFailure::kTimeout, "embedding asset load deadline");
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure cause;
  const char* entry = "--embedding-asset-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  const auto started = detail::GrammarProcess::Start(entry, control, &process,
                                                    &cause);
  if (!started) return fail(cause, "embedding asset worker unavailable");
  const auto transfer = [&](void* data, size_t bytes, bool write) {
    return process->Transfer(data, bytes, write, control.compile_deadline,
                              control.cancelled);
  };
  try {
    Request request{kMagic, directory.size()};
    Reply reply;
    if (!transfer(&request, sizeof(request), true) ||
        !transfer(const_cast<char*>(directory.data()), directory.size(), true) ||
        !transfer(&reply, sizeof(reply), false))
      return fail(process->Failure(), "embedding asset worker transfer failed");
    if (reply.magic != kMagic || reply.code != 0 ||
        reply.config_bytes != kConfigBytes ||
        reply.tokenizer_bytes != kTokenizerBytes ||
        reply.weight_bytes != kNativeEmbeddingFileBytes)
      return fail(GrammarFailure::kDependency, "embedding assets absent or invalid");
    std::string config(kConfigBytes, '\0'), tokenizer(kTokenizerBytes, '\0');
    if (!transfer(config.data(), config.size(), false) ||
        !transfer(tokenizer.data(), tokenizer.size(), false))
      return fail(process->Failure(), "embedding metadata transfer failed");
    if (Digest(Bytes(config)) != kConfigDigest ||
        Digest(Bytes(tokenizer)) != kTokenizerDigest)
      return fail(GrammarFailure::kDependency, "embedding metadata SHA256 differs");
    io::Json parsed;
    io::JsonParseLimits limits{.max_depth = 16, .max_values = 32768,
        .max_string_bytes = 1024 * 1024, .reject_duplicate_keys = true};
    auto status = io::ParseJson(config, &parsed, false, limits);
    if (status) status = model::ValidateEmbeddingConfig(parsed);
    if (!status) return fail(GrammarFailure::kDependency,
                            "embedding architecture differs");
    EmbeddingHostAssets next;
    status = text::Tokenizer::LoadJson(tokenizer, {}, &next.tokenizer,
                                       text::TokenizerProfile::kQwen3Embedding);
    if (!status) return fail(GrammarFailure::kDependency,
                            "embedding tokenizer contract differs");
    next.file_bytes.resize(kNativeEmbeddingFileBytes);
    if (!transfer(next.file_bytes.data(), next.file_bytes.size(), false))
      return fail(process->Failure(), "embedding weight transfer failed");
    std::string trailing;
    if (!process->CollectOutput(&trailing, 0, control.compile_deadline,
                                control.cancelled))
      return fail(process->Failure(), "embedding worker completion unconfirmed");
    if (Digest(next.file_bytes) != kNativeEmbeddingWeightSha256)
      return fail(GrammarFailure::kDependency, "embedding weights SHA256 differs");
    uint64_t header_bytes = 0;
    for (int i = 0; i < 8; ++i)
      header_bytes |= static_cast<uint64_t>(next.file_bytes[i]) << (8 * i);
    if (!header_bytes || header_bytes > 1024 * 1024 ||
        header_bytes + 8 >= next.file_bytes.size())
      return fail(GrammarFailure::kDependency, "embedding weight header invalid");
    next.data_offset = header_bytes + 8;
    const std::string header(
        reinterpret_cast<const char*>(next.file_bytes.data() + 8), header_bytes);
    status = io::ParseJson(header, &parsed, false, limits);
    if (status)
      status = model::ParseEmbeddingWeightLayout(parsed, next.WeightBytes().size(),
                                                 &next.layout);
    if (!status) return fail(GrammarFailure::kDependency,
                            "embedding tensor layout differs");
    if (control.cancelled && control.cancelled())
      return fail(GrammarFailure::kCancelled, "embedding asset load cancelled");
    if (Clock::now() >= control.compile_deadline)
      return fail(GrammarFailure::kTimeout, "embedding asset validation deadline");
    *out = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource, "embedding host staging exhausted");
  }
}

int MaybeRunEmbeddingAssetWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--embedding-asset-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    Request request;
    if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
        request.magic != kMagic || request.path_bytes > 4096 ||
        request.path_bytes < 2) return 3;
    std::string path(request.path_bytes, '\0');
    if (!detail::WorkerTransfer(path.data(), path.size(), false) ||
        !ValidDirectory(path)) return 3;
    const int directory = open(path.c_str(),
                               O_DIRECTORY | O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    std::array<int, 3> fds{-1, -1, -1};
    if (directory >= 0) {
      fds[0] = OpenFile(directory, "config.json", kConfigBytes);
      fds[1] = OpenFile(directory, "tokenizer.json", kTokenizerBytes);
      fds[2] = OpenFile(directory, "model.safetensors", kNativeEmbeddingFileBytes);
      close(directory);
    }
    const bool opened = std::all_of(fds.begin(), fds.end(),
                                    [](int fd) { return fd >= 0; });
    Reply reply;
    if (opened) reply = {kMagic, 0, kConfigBytes, kTokenizerBytes,
                         kNativeEmbeddingFileBytes};
    bool ok = detail::WorkerTransfer(&reply, sizeof(reply), true);
    if (ok && opened)
      ok = StreamFile(fds[0], kConfigBytes) &&
           StreamFile(fds[1], kTokenizerBytes) &&
           StreamFile(fds[2], kNativeEmbeddingFileBytes);
    for (const int fd : fds)
      if (fd >= 0) close(fd);
    return ok ? 0 : 4;
  } catch (...) {
    return 5;
  }
}
}  // namespace q4t::server
