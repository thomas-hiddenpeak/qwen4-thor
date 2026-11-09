#include "q4t/server/cache_io.h"

#include <fcntl.h>
#include <linux/magic.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string_view>

#include "cache_protocol.h"

namespace q4t::server {
namespace {
using namespace cache_detail;
struct File {
  uint64_t id = 0;
  int fd = -1;
};
struct Storage {
  int directory = -1;
  void* buffer = nullptr;
  std::array<File, 9> files{};  // Eight entries and one bounded replacement.
  uint64_t next_id = 0;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  bool short_write = false;
#endif
  ~Storage() {
    for (auto& file : files)
      if (file.fd >= 0) close(file.fd);
    if (directory >= 0) close(directory);
    std::free(buffer);
  }
  bool Open(const std::string& path) {
    if (path.empty() || path.find('\0') != std::string::npos) return false;
    directory =
        open(path.c_str(), O_DIRECTORY | O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    struct stat st {};
    struct statfs fs {};
    if (directory < 0 || fstat(directory, &st) || fstatfs(directory, &fs) ||
        st.st_uid != geteuid() || (st.st_mode & 077) ||
        fs.f_type == TMPFS_MAGIC ||
        posix_memalign(&buffer, CacheIo::kAlignment, CacheIo::kBlockBytes))
      return false;
    const int probe = NewFile();
    if (probe < 0) return false;
    std::memset(buffer, 0, CacheIo::kAlignment);
    const bool ok = Io(probe, true, CacheIo::kAlignment, 0) &&
                    Io(probe, false, CacheIo::kAlignment, 0);
    const bool closed = close(probe) == 0;
    return ok && closed;
  }
  int NewFile() const {
    return openat(directory, ".", O_TMPFILE | O_DIRECT | O_RDWR | O_CLOEXEC,
                  0600);
  }
  bool Io(int fd, bool write, size_t bytes, uint64_t offset) {
    ssize_t count;
    do {
      count = write ? pwrite(fd, buffer, bytes, offset)
                    : pread(fd, buffer, bytes, offset);
    } while (count < 0 && errno == EINTR);
    return count == static_cast<ssize_t>(bytes);
  }
  bool Execute(const Frame& frame, Reply* reply) {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    if (frame.operation == kTestShortWrite) {
      short_write = true;
      reply->code = kOk;
      return true;
    }
#endif
    if (frame.operation == kCreate) {
      for (auto& file : files) {
        if (file.fd >= 0) continue;
        file.fd = NewFile();
        if (file.fd < 0) return false;
        file.id = ++next_id;
        reply->file = file.id;
        reply->code = kOk;
        return true;
      }
      reply->code = kFull;
      return true;
    }
    File* file = nullptr;
    for (auto& item : files)
      if (item.fd >= 0 && item.id == frame.file) file = &item;
    if (!file) {
      reply->code = kInvalid;
      return false;
    }
    reply->file = file->id;
    if (frame.operation == kClose) {
      const int fd = file->fd;
      file->fd = -1;
      if (close(fd)) return false;
    } else if (frame.operation == kStat) {
      struct stat st {};
      if (fstat(file->fd, &st) || st.st_blocks < 0 || st.st_size < 0)
        return false;
      reply->allocated = static_cast<uint64_t>(st.st_blocks) * 512;
      reply->span = st.st_size;
    } else if (frame.operation == kRead || frame.operation == kWrite) {
      if (frame.operation == kWrite &&
          !detail::WorkerTransfer(buffer, frame.bytes, false))
        return false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      if (frame.operation == kWrite && short_write) return false;
#endif
      if (!Io(file->fd, frame.operation == kWrite, frame.bytes, frame.offset))
        return false;
      if (frame.operation == kRead) reply->bytes = frame.bytes;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    } else if (frame.operation == kTestTruncate) {
      if (ftruncate(file->fd, 0)) return false;
#endif
    } else {
      reply->code = kInvalid;
      return false;
    }
    reply->code = kOk;
    return true;
  }
};

uint64_t Rss() {
  FILE* file = std::fopen("/proc/self/statm", "r");
  unsigned long pages = 0, resident = 0;
  const bool ok = file && std::fscanf(file, "%lu %lu", &pages, &resident) == 2;
  if (file) std::fclose(file);
  const long page_size = sysconf(_SC_PAGESIZE);
  return ok && page_size > 0 ? resident * static_cast<uint64_t>(page_size) : 0;
}
bool Shape(const Frame& f, bool initialized) {
  if (f.reserved || f.magic != kMagic || !f.generation) return false;
  if (!initialized)
    return f.operation == kInit && !f.file && !f.offset && f.bytes > 0 &&
           f.bytes <= 4096;
  if (f.operation == kCreate) return !f.file && !f.offset && !f.bytes;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (f.operation == kTestShortWrite) return !f.file && !f.offset && !f.bytes;
#endif
  if (!f.file) return false;
  if (f.operation == kRead || f.operation == kWrite)
    return f.bytes > 0 && f.bytes <= CacheIo::kBlockBytes &&
           f.bytes % CacheIo::kAlignment == 0 &&
           f.offset % CacheIo::kAlignment == 0 &&
           f.offset <= CacheIo::kMaxFileSpan - f.bytes;
  if (f.operation == kStat || f.operation == kClose
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      || f.operation == kTestTruncate
#endif
  )
    return !f.offset && !f.bytes;
  return false;
}
}  // namespace

int MaybeRunCacheWorker(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--q4t-cache-worker") return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    Storage storage;
    uint64_t sequence = 0, generation = 0;
    for (;;) {
      Frame frame;
      if (!detail::WorkerTransfer(&frame, sizeof(frame), false)) return 0;
      if (frame.sequence != ++sequence || !Shape(frame, generation != 0) ||
          (generation && frame.generation != generation))
        return 3;
      Reply reply;
      reply.sequence = frame.sequence;
      reply.generation = frame.generation;
      bool ok;
      if (!generation) {
        std::string path(frame.bytes, '\0');
        if (!detail::WorkerTransfer(path.data(), path.size(), false)) return 3;
        ok = storage.Open(path);
        if (ok) {
          generation = frame.generation;
          reply.code = kOk;
        }
      } else {
        ok = storage.Execute(frame, &reply);
      }
      reply.rss = Rss();
      if (!detail::WorkerTransfer(&reply, sizeof(reply), true) ||
          (reply.bytes &&
           !detail::WorkerTransfer(storage.buffer, reply.bytes, true)))
        return 3;
      if (!ok) return 4;
    }
  } catch (...) {
    return 5;
  }
}
}  // namespace q4t::server
