#include "q4t/server/session_cache.h"

#include <fcntl.h>
#include <linux/magic.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <thread>

namespace q4t::server {
namespace {
constexpr size_t kAlignment = 4096;
size_t Aligned(size_t n) { return (n + kAlignment - 1) / kAlignment * kAlignment; }
Status IoError(const char* action) {
  return Status::Fail(std::string("session cache ") + action + ": " +
                       std::strerror(errno));
}

// One writer and one outstanding block. The producer owns the other buffer.
// Destruction drains the write before the caller can free either buffer or fd.
class SnapshotWriter {
 public:
  SnapshotWriter(int fd, SessionSaveTiming* timing)
      : fd_(fd), timing_(timing), thread_([this] { Run(); }) {}
  ~SnapshotWriter() { Finish(); }
  Status Wait(double prepare_begin = 0, double prepare_end = 0) {
    SessionSaveTiming::Scope span(timing_, SessionSaveTiming::kPipelineWait);
    std::unique_lock lock(mutex_);
    cv_.wait(lock, [this] { return !busy_; });
    overlap_ms_ += std::max(0.0, std::min(prepare_end, write_end_) -
                                   std::max(prepare_begin, write_begin_));
    return status_;
  }
  void Submit(void* data, size_t bytes, size_t disk_bytes, size_t offset) {
    const std::lock_guard lock(mutex_);
    data_ = data;
    bytes_ = bytes;
    disk_bytes_ = disk_bytes;
    offset_ = offset;
    busy_ = true;
    cv_.notify_one();
  }
  void Finish() {
    if (!thread_.joinable()) return;
    SessionSaveTiming::Scope span(timing_, SessionSaveTiming::kPipelineWait);
    {
      const std::lock_guard lock(mutex_);
      stop_ = true;
    }
    cv_.notify_one();
    thread_.join();
    if (timing_)
      timing_->MergeWriter(writer_timing_, writer_cpu_ms_, overlap_ms_);
  }

 private:
  void Run() {
    const double begin_cpu =
        SessionSaveTiming::Enabled() ? writer_timing_.Cpu() : 0;
    std::unique_lock lock(mutex_);
    for (;;) {
      cv_.wait(lock, [this] { return busy_ || stop_; });
      if (!busy_) break;
      lock.unlock();
      ssize_t wrote;
      if (SessionSaveTiming::Enabled()) write_begin_ = SessionSaveTiming::Wall();
      {
        SessionSaveTiming::Scope span(&writer_timing_,
                                       SessionSaveTiming::kWrite);
        do {
          wrote = pwrite(fd_, data_, disk_bytes_, offset_);
        } while (wrote < 0 && errno == EINTR);
      }
      if (SessionSaveTiming::Enabled()) write_end_ = SessionSaveTiming::Wall();
      const Status result = wrote == static_cast<ssize_t>(disk_bytes_)
                                ? Status() : IoError("write/short write");
      if (result.ok()) {
        writer_timing_.data_bytes += bytes_;
        writer_timing_.disk_bytes += disk_bytes_;
      }
      lock.lock();
      status_ = result;
      busy_ = false;
      cv_.notify_one();
    }
    if (SessionSaveTiming::Enabled())
      writer_cpu_ms_ = writer_timing_.Cpu() - begin_cpu;
  }
  int fd_;
  SessionSaveTiming* timing_;
  std::mutex mutex_;
  std::condition_variable cv_;
  bool busy_ = false, stop_ = false;
  void* data_ = nullptr;
  size_t bytes_ = 0, disk_bytes_ = 0, offset_ = 0;
  Status status_;
  SessionSaveTiming writer_timing_{"writer", "private"};
  double writer_cpu_ms_ = 0;
  double write_begin_ = 0, write_end_ = 0, overlap_ms_ = 0;
  std::thread thread_;
};
}  // namespace

SessionCache::Entry::~Entry() { if (fd >= 0) close(fd); }
SessionCache::~SessionCache() {
  entries_.clear();
  if (directory_fd_ >= 0) close(directory_fd_);
  std::free(staging_);
}

Status SessionCache::Open(const std::string& directory, int min_prefix,
                           size_t disk_budget) {
  if (Enabled()) return Status::Fail("session cache already open");
  if (min_prefix < 1 || min_prefix > kMaxPrefix || !disk_budget ||
      disk_budget > kDiskBudget)
    return Status::Fail("session cache limits");
  int fd = open(directory.c_str(), O_DIRECTORY | O_RDONLY | O_CLOEXEC |
                                    O_NOFOLLOW);
  if (fd < 0) return IoError("directory");
  struct stat st{};
  struct statfs fs{};
  if (fstat(fd, &st) || fstatfs(fd, &fs) || st.st_uid != geteuid() ||
      (st.st_mode & 077) || fs.f_type == TMPFS_MAGIC) {
    close(fd);
    return Status::Fail("session cache requires owned private disk directory");
  }
  const int probe = openat(fd, ".", O_TMPFILE | O_DIRECT | O_RDWR | O_CLOEXEC,
                            0600);
  if (probe < 0) {
    const Status s = IoError("O_TMPFILE/O_DIRECT");
    close(fd);
    return s;
  }
  void* buffer = nullptr;
  if (posix_memalign(&buffer, kAlignment, kStagingBytes)) {
    close(probe);
    close(fd);
    return Status::Fail("session cache staging allocation");
  }
  std::memset(buffer, 0, kAlignment);
  const bool ok = pwrite(probe, buffer, kAlignment, 0) == kAlignment &&
                  pread(probe, buffer, kAlignment, 0) == kAlignment;
  close(probe);
  if (!ok) {
    const Status s = IoError("direct IO preflight");
    std::free(buffer);
    close(fd);
    return s;
  }
  staging_ = buffer;
  directory_fd_ = fd;
  min_prefix_ = min_prefix;
  disk_budget_ = disk_budget;
  return Status();
}

bool SessionCache::Acquire(const std::string& key) {
  const std::lock_guard lock(mutex_);
  return Enabled() && busy_.size() < 32 && busy_.insert(key).second;
}
bool SessionCache::AcquireNew(const std::string& key) {
  const std::lock_guard lock(mutex_);
  if (!Enabled() || busy_.size() >= 32 || busy_.contains(key)) return false;
  const auto old = entries_.find(key);
  if (old != entries_.end()) {
    if (std::chrono::steady_clock::now() - old->second->used <=
        std::chrono::minutes(30)) return false;
    Erase(old);
  }
  return busy_.insert(key).second;
}

int SessionCache::MatchShared(const std::string& scope,
                               std::span<const int32_t> prompt,
                               std::string* key) {
  const std::lock_guard lock(mutex_);
  key->clear();
  if (!Enabled() || busy_.size() >= 32) return 0;
  auto best = entries_.end();
  const auto now = std::chrono::steady_clock::now();
  const std::string group = scope + ':';
  for (auto it = entries_.begin(); it != entries_.end();) {
    if (busy_.contains(it->first)) { ++it; continue; }
    const auto& e = *it->second;
    if (now - e.used > std::chrono::minutes(30)) {
      auto expired = it++;
      Erase(expired);
      continue;
    }
    if (it->first.starts_with(group) && e.published &&
        e.prefix.size() <= prompt.size() &&
        (best == entries_.end() ||
         e.prefix.size() > best->second->prefix.size()) &&
        std::equal(e.prefix.begin(), e.prefix.end() - 1, prompt.begin()))
      best = it;
    ++it;
  }
  if (best == entries_.end()) return 0;
  best->second->used = now;
  *key = best->first;
  busy_.insert(*key);
  return static_cast<int>(best->second->prefix.size()) - 1;
}
void SessionCache::Erase(Entries::iterator it) {
  bytes_ -= it->second->bytes;
  entries_.erase(it);
}
void SessionCache::Release(const std::string& key, bool success) {
  const std::lock_guard lock(mutex_);
  auto it = entries_.find(key);
  if (it != entries_.end() && !it->second->published) {
    if (success) it->second->published = true;
    else Erase(it);
  }
  busy_.erase(key);
}
size_t SessionCache::Bytes() const {
  const std::lock_guard lock(mutex_);
  return bytes_;
}
int SessionCache::Match(const std::string& key,
                        std::span<const int32_t> prompt) {
  const std::lock_guard lock(mutex_);
  auto it = entries_.find(key);
  if (!busy_.contains(key) || it == entries_.end()) return 0;
  auto& entry = *it->second;
  if (std::chrono::steady_clock::now() - entry.used >
      std::chrono::minutes(30)) {
    Erase(it);
    return 0;
  }
  if (!entry.published || entry.prefix.size() > prompt.size() ||
      !std::equal(entry.prefix.begin(), entry.prefix.end() - 1, prompt.begin()))
    return 0;
  entry.used = std::chrono::steady_clock::now();
  return static_cast<int>(entry.prefix.size()) - 1;
}

Status SessionCache::Transfer(Entry* entry,
                              std::span<const SessionRange> ranges,
                              const Copy& copy, const Cancel& cancel,
                              bool restore, SessionSaveTiming* timing) {
  const char* pipeline = std::getenv("Q4T_SESSION_PIPELINE");
  if (!restore && pipeline && std::strcmp(pipeline, "1") == 0)
    return SavePipelined(entry, ranges, copy, cancel, timing);
  size_t offset = 0;
  size_t block = 0;
  for (const auto& range : ranges) {
    for (size_t start = 0; start < range.bytes;) {
      if (cancel()) return Status::Fail("session cache cancelled");
      const size_t size = std::min(kStagingBytes, range.bytes - start);
      const size_t disk_size = Aligned(size);
      auto* state = static_cast<uint8_t*>(range.data) + start;
      if (restore) {
        ssize_t got;
        do {
          got = pread(entry->fd, staging_, disk_size, offset);
        } while (got < 0 && errno == EINTR);
        if (got != static_cast<ssize_t>(disk_size))
          return IoError("read/short read");
        if (block >= entry->digests.size() ||
            trace::Sha256({static_cast<uint8_t*>(staging_), disk_size}) !=
                entry->digests[block])
          return Status::Fail("session cache checksum mismatch");
        const Status s = copy(staging_, state, size, true);
        if (!s.ok()) return s;
      } else {
        Status s;
        {
          SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCopy);
          s = copy(staging_, state, size, false);
        }
        if (!s.ok()) return s;
        {
          SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPadding);
          std::memset(static_cast<uint8_t*>(staging_) + size, 0,
                      disk_size - size);
        }
        {
          SessionSaveTiming::Scope span(timing, SessionSaveTiming::kHash);
          entry->digests.push_back(
              trace::Sha256({static_cast<uint8_t*>(staging_), disk_size}));
        }
        ssize_t wrote;
        {
          SessionSaveTiming::Scope span(timing, SessionSaveTiming::kWrite);
          do {
            wrote = pwrite(entry->fd, staging_, disk_size, offset);
          } while (wrote < 0 && errno == EINTR);
        }
        if (wrote != static_cast<ssize_t>(disk_size))
          return IoError("write/short write");
        if (timing) {
          timing->data_bytes += size;
          timing->disk_bytes += disk_size;
        }
      }
      start += size;
      offset += disk_size;
      ++block;
    }
  }
  if (offset != entry->bytes || block != entry->digests.size())
    return Status::Fail("session cache layout mismatch");
  return Status();
}

Status SessionCache::SavePipelined(Entry* entry,
                                  std::span<const SessionRange> ranges,
                                  const Copy& copy, const Cancel& cancel,
                                  SessionSaveTiming* timing) {
  std::unique_ptr<void, decltype(&std::free)> extra(nullptr, &std::free);
  std::unique_ptr<SnapshotWriter> writer;
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPipelineSetup);
    void* buffer = nullptr;
    if (posix_memalign(&buffer, kAlignment, kStagingBytes))
      return Status::Fail("session cache pipeline allocation");
    extra.reset(buffer);
    try {
      writer = std::make_unique<SnapshotWriter>(entry->fd, timing);
    } catch (const std::system_error&) {
      return Status::Fail("session cache writer creation");
    }
  }
  void* current = staging_;
  void* other = extra.get();
  size_t offset = 0, block = 0;
  for (const auto& range : ranges) {
    for (size_t start = 0; start < range.bytes;) {
      if (cancel()) return Status::Fail("session cache cancelled");
      const size_t size = std::min(kStagingBytes, range.bytes - start);
      const size_t disk_size = Aligned(size);
      auto* state = static_cast<uint8_t*>(range.data) + start;
      Status status;
      const double prepare_begin =
          SessionSaveTiming::Enabled() ? SessionSaveTiming::Wall() : 0;
      {
        SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCopy);
        status = copy(current, state, size, false);
      }
      if (!status.ok()) return status;
      {
        SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPadding);
        std::memset(static_cast<uint8_t*>(current) + size, 0, disk_size - size);
      }
      {
        SessionSaveTiming::Scope span(timing, SessionSaveTiming::kHash);
        entry->digests.push_back(
            trace::Sha256({static_cast<uint8_t*>(current), disk_size}));
      }
      const double prepare_end =
          SessionSaveTiming::Enabled() ? SessionSaveTiming::Wall() : 0;
      status = writer->Wait(prepare_begin, prepare_end);
      if (!status.ok()) return status;
      if (cancel()) return Status::Fail("session cache cancelled");
      writer->Submit(current, size, disk_size, offset);
      std::swap(current, other);
      start += size;
      offset += disk_size;
      ++block;
    }
  }
  const Status status = writer->Wait();
  writer->Finish();
  if (!status.ok()) return status;
  if (offset != entry->bytes || block != entry->digests.size())
    return Status::Fail("session cache layout mismatch");
  return Status();
}

Status SessionCache::Restore(const std::string& key,
                             std::span<const SessionRange> ranges,
                             const Copy& copy, const Cancel& cancel,
                             int32_t* d0, int32_t* pending) {
  const std::lock_guard lock(mutex_);
  auto it = entries_.find(key);
  if (!busy_.contains(key) || it == entries_.end() || !it->second->published)
    return Status::Fail("session cache missing lease/entry");
  auto& entry = *it->second;
  if (ranges.size() != entry.sizes.size())
    return Status::Fail("session cache range count");
  for (size_t i = 0; i < ranges.size(); ++i)
    if (!ranges[i].data || ranges[i].bytes != entry.sizes[i])
      return Status::Fail("session cache range size");
  const Status s = Transfer(&entry, ranges, copy, cancel, true);
  if (s.ok() && d0) *d0 = entry.d0;
  if (s.ok() && pending) *pending = entry.prefix.back();
  if (!s.ok()) Erase(it);
  return s;
}

Status SessionCache::Save(const std::string& key,
                          std::span<const int32_t> prefix,
                          std::span<const SessionRange> ranges,
                          const Copy& copy, const Cancel& cancel, int32_t d0,
                          SessionSaveTiming* timing) {
  std::unique_lock<std::mutex> lock;
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCacheLock);
    lock = std::unique_lock<std::mutex>(mutex_);
  }
  size_t bytes = 0;
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kMetadata);
    if (!busy_.contains(key) || prefix.size() <= size_t(min_prefix_) ||
        prefix.size() > kMaxPrefix + 1 || ranges.empty())
      return Status::Fail("session cache save admission");
    for (const auto& range : ranges) {
      if (!range.data || !range.bytes || range.bytes > disk_budget_ ||
          Aligned(range.bytes) > disk_budget_ - bytes)
        return Status::Fail("session cache save range/budget");
      bytes += Aligned(range.bytes);
    }
    // The old prefix is no longer needed by this request after restoration.
    // Losing a cache entry on a failed replacement is a miss, never stale
    // reuse.
    if (auto old = entries_.find(key); old != entries_.end()) Erase(old);
    while (entries_.size() >= kMaxEntries || bytes > disk_budget_ - bytes_) {
      auto victim = entries_.end();
      for (auto it = entries_.begin(); it != entries_.end(); ++it) {
        if (busy_.contains(it->first)) continue;
        if (victim == entries_.end() || it->second->used < victim->second->used)
          victim = it;
      }
      if (victim == entries_.end()) return Status::Fail("session cache full");
      Erase(victim);
    }
  }
  auto entry = std::make_unique<Entry>();
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kOpen);
    entry->fd = openat(directory_fd_, ".",
                       O_TMPFILE | O_DIRECT | O_RDWR | O_CLOEXEC, 0600);
  }
  if (entry->fd < 0) return IoError("snapshot file");
  // One bounded candidate; diagnostic control uses the identical executable.
  // Keep size unchanged until successful writes, and never publish failures.
  const char* preallocate = std::getenv("Q4T_SESSION_PREALLOCATE");
  if (!preallocate || std::strcmp(preallocate, "0") != 0) {
    int result;
    {
      SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPreallocate);
      do {
        result = fallocate(entry->fd, FALLOC_FL_KEEP_SIZE, 0, bytes);
      } while (result < 0 && errno == EINTR);
    }
    if (result < 0 && errno != EOPNOTSUPP && errno != ENOSYS) {
      const Status error = IoError("preallocate");
      SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCleanup);
      entry.reset();
      return error;
    }
    if (timing) {
      timing->preallocated = result == 0;
      timing->fallback = result < 0;
    }
    if (result < 0)
      std::fprintf(
          stderr, "[session-cache-preallocate] fallback=unsupported errno=%d\n",
          errno);
  }
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kMetadata);
    entry->bytes = bytes;
    entry->d0 = d0;
    entry->prefix.assign(prefix.begin(), prefix.end());
    entry->used = std::chrono::steady_clock::now();
    for (const auto& range : ranges) entry->sizes.push_back(range.bytes);
  }
  const Status s = Transfer(entry.get(), ranges, copy, cancel, false, timing);
  if (!s.ok()) {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCleanup);
    entry.reset();
    return s;
  }
  SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPublish);
  bytes_ += bytes;
  entries_.emplace(key, std::move(entry));
  return Status();
}

}  // namespace q4t::server
