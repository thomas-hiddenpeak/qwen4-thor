#include "q4t/server/asr_statistics.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#include <zlib.h>

#include <algorithm>
#include <cstring>
#include <vector>

#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using Clock = std::chrono::steady_clock;
constexpr uint64_t kMagic = 0x3154415352343451ULL;
constexpr uint64_t kTextLimit = 4ULL << 20;
struct Request {
  uint64_t magic = kMagic, bytes = 0;
};
struct Reply {
  uint64_t magic = kMagic, text_bytes = 0, compressed_bytes = 0;
  // 0 success, 1 resource, 2 dependency, 3 malformed/worker.
  uint32_t failure = 3;
  char version[64]{};
  char sha256[65]{};
};
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string worker_entry;
#endif

bool Load(bool missing, Reply* reply, void** library) {
  const int fd =
      open(missing ? "/q4t-missing-statistics-zlib" : Q4T_SYSTEM_ZLIB_LIBRARY,
           O_RDONLY | O_CLOEXEC | O_NONBLOCK);
  if (fd < 0) return false;
  ScopeExit close_fd([&] { close(fd); });
  struct stat info {};
  if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
      (info.st_mode & (S_IWGRP | S_IWOTH)) || info.st_size <= 0 ||
      info.st_size > (128LL << 20))
    return false;
  const auto pinned = "/proc/self/fd/" + std::to_string(fd);
  trace::Digest digest;
  if (!trace::HashFile(pinned, &digest)) return false;
  *library = dlopen(pinned.c_str(), RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!*library) return false;
  const auto hash = trace::HexDigest(digest);
  std::memcpy(reply->sha256, hash.c_str(), hash.size() + 1);
  return true;
}

int Worker(int argc, char** argv, bool missing) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{2, 2};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Reply reply;
  try {
    Request request;
    if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
        request.magic != kMagic || request.bytes > kTextLimit)
      return 3;
    std::string text(request.bytes, '\0');
    if (!detail::WorkerTransfer(text.data(), text.size(), false)) return 3;
    void* library = nullptr;
    if (!Load(missing, &reply, &library)) {
      reply.failure = 2;
    } else {
      const auto version = reinterpret_cast<decltype(&zlibVersion)>(
          dlsym(library, "zlibVersion"));
      const auto bound = reinterpret_cast<decltype(&compressBound)>(
          dlsym(library, "compressBound"));
      const auto compress =
          reinterpret_cast<decltype(&compress2)>(dlsym(library, "compress2"));
      if (!version || !bound || !compress) {
        reply.failure = 2;
      } else if (detail::EnterNativeDocumentSandbox()) {
        const char* name = version();
        if (!name ||
            strnlen(name, sizeof(reply.version)) >= sizeof(reply.version)) {
          reply.failure = 2;
        } else {
          std::memcpy(reply.version, name, std::strlen(name) + 1);
          uLongf count = bound(text.size());
          if (count > kTextLimit + 65536) return 3;
          std::vector<uint8_t> compressed(count);
          const int result = compress(
              compressed.data(), &count,
              reinterpret_cast<const Bytef*>(text.data()), text.size(), 6);
          reply.failure = result == Z_OK ? 0 : result == Z_MEM_ERROR ? 1 : 3;
          if (!reply.failure && count > 0 && count <= compressed.size()) {
            reply.text_bytes = text.size();
            reply.compressed_bytes = count;
          } else if (!reply.failure) {
            reply.failure = 3;
          }
        }
      }
    }
  } catch (const std::bad_alloc&) {
    reply.failure = 1;
  } catch (...) {
    reply.failure = 3;
  }
  return detail::WorkerTransfer(&reply, sizeof(reply), true) ? 0 : 3;
}
}  // namespace

Status MeasureAsrCompression(std::string_view text, AsrCompression* out,
                             GrammarControl control, GrammarFailure* failure) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* why) {
    if (failure) *failure = kind;
    return Status::Fail(why);
  };
  if (!out || text.size() > kTextLimit)
    return fail(GrammarFailure::kResource, "ASR statistics byte budget");
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline = std::min(
      control.compile_deadline, Clock::now() + std::chrono::milliseconds(2000));
  const char* entry = "--asr-statistics-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!worker_entry.empty()) entry = worker_entry.c_str();
#endif
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure cause;
  if (!detail::GrammarProcess::Start(entry, control, &process, &cause))
    return fail(cause, "ASR statistics worker unavailable");
  Request request{kMagic, text.size()};
  Reply reply;
  const auto transfer = [&](void* data, size_t bytes, bool write) {
    return process->Transfer(data, bytes, write, control.compile_deadline,
                             control.cancelled);
  };
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(text.data()), text.size(), true) ||
      !transfer(&reply, sizeof(reply), false))
    return fail(process->Failure(), "ASR statistics transfer failed");
  std::string trailing;
  if (!process->CollectOutput(&trailing, 0, control.compile_deadline,
                              control.cancelled))
    return fail(process->Failure(), "ASR statistics completion unconfirmed");
  if (reply.magic != kMagic || reply.failure > 3)
    return fail(GrammarFailure::kWorker, "ASR statistics protocol invalid");
  if (reply.failure)
    return fail(reply.failure == 1   ? GrammarFailure::kResource
                : reply.failure == 2 ? GrammarFailure::kDependency
                                     : GrammarFailure::kWorker,
                "ASR compression diagnostics unavailable");
  if (reply.text_bytes != text.size() || !reply.compressed_bytes ||
      reply.compressed_bytes > kTextLimit + 65536 || !reply.version[0] ||
      strnlen(reply.version, sizeof(reply.version)) >= sizeof(reply.version) ||
      strnlen(reply.sha256, sizeof(reply.sha256)) != 64 ||
      !std::all_of(reply.sha256, reply.sha256 + 64, [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
      }))
    return fail(GrammarFailure::kWorker, "ASR compression result invalid");
  if (control.cancelled && control.cancelled())
    return fail(GrammarFailure::kCancelled, "ASR statistics cancelled");
  if (Clock::now() >= control.compile_deadline)
    return fail(GrammarFailure::kTimeout, "ASR statistics deadline");
  *out = {reply.text_bytes, reply.compressed_bytes, reply.version,
          reply.sha256};
  return {};
}

int MaybeRunAsrStatisticsWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool missing = false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  missing = !std::strcmp(argv[1], "--asr-statistics-missing-worker");
#endif
  if (std::strcmp(argv[1], "--asr-statistics-worker") && !missing) return -1;
  try {
    return Worker(argc, argv, missing);
  } catch (...) {
    return 3;
  }
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetAsrStatisticsWorkerForTesting(std::string entry) {
  worker_entry = std::move(entry);
}
#endif
}  // namespace q4t::server
