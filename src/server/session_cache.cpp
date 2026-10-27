#include "q4t/server/session_cache.h"

#include <fcntl.h>
#include <linux/magic.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <memory>

namespace q4t::server {
namespace {
constexpr size_t kAlignment = 4096;
size_t Aligned(size_t n) { return (n + kAlignment - 1) / kAlignment * kAlignment; }
Status IoError(const char* action) {
  return Status::Fail(std::string("session cache ") + action + ": " +
                       std::strerror(errno));
}
}  // namespace

SessionCache::Entry::~Entry() { if (fd >= 0) close(fd); }
SessionCache::~SessionCache() {
  entries_.clear();
  if (directory_fd_ >= 0) close(directory_fd_);
  std::free(staging_);
}

Status SessionCache::Open(const std::string& directory) {
  if (Enabled()) return Status::Fail("session cache already open");
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
  return Status();
}

bool SessionCache::Acquire(const std::string& key) {
  const std::lock_guard lock(mutex_);
  return Enabled() && busy_.size() < 32 && busy_.insert(key).second;
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
                               bool restore) {
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
        do { got = pread(entry->fd, staging_, disk_size, offset); }
        while (got < 0 && errno == EINTR);
        if (got != static_cast<ssize_t>(disk_size))
          return IoError("read/short read");
        if (block >= entry->digests.size() ||
            trace::Sha256({static_cast<uint8_t*>(staging_), disk_size}) !=
                entry->digests[block])
          return Status::Fail("session cache checksum mismatch");
        const Status s = copy(staging_, state, size, true);
        if (!s.ok()) return s;
      } else {
        const Status s = copy(staging_, state, size, false);
        if (!s.ok()) return s;
        std::memset(static_cast<uint8_t*>(staging_) + size, 0, disk_size - size);
        entry->digests.push_back(
            trace::Sha256({static_cast<uint8_t*>(staging_), disk_size}));
        ssize_t wrote;
        do { wrote = pwrite(entry->fd, staging_, disk_size, offset); }
        while (wrote < 0 && errno == EINTR);
        if (wrote != static_cast<ssize_t>(disk_size))
          return IoError("write/short write");
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
                           const Copy& copy, const Cancel& cancel,
                           int32_t d0) {
  const std::lock_guard lock(mutex_);
  if (!busy_.contains(key) || prefix.size() <= kMinPrefix ||
      prefix.size() > kMaxPrefix + 1 || ranges.empty())
    return Status::Fail("session cache save admission");
  size_t bytes = 0;
  for (const auto& range : ranges) {
    if (!range.data || !range.bytes || range.bytes > kDiskBudget ||
        Aligned(range.bytes) > kDiskBudget - bytes)
      return Status::Fail("session cache save range/budget");
    bytes += Aligned(range.bytes);
  }
  // The old prefix is no longer needed by this request after restoration.
  // Losing a cache entry on a failed replacement is a miss, never stale reuse.
  if (auto old = entries_.find(key); old != entries_.end()) Erase(old);
  while (entries_.size() >= kMaxEntries || bytes > kDiskBudget - bytes_) {
    auto victim = entries_.end();
    for (auto it = entries_.begin(); it != entries_.end(); ++it) {
      if (busy_.contains(it->first)) continue;
      if (victim == entries_.end() || it->second->used < victim->second->used)
        victim = it;
    }
    if (victim == entries_.end()) return Status::Fail("session cache full");
    Erase(victim);
  }
  auto entry = std::make_unique<Entry>();
  entry->fd = openat(directory_fd_, ".",
                      O_TMPFILE | O_DIRECT | O_RDWR | O_CLOEXEC, 0600);
  if (entry->fd < 0) return IoError("snapshot file");
  entry->bytes = bytes;
  entry->d0 = d0;
  entry->prefix.assign(prefix.begin(), prefix.end());
  entry->used = std::chrono::steady_clock::now();
  for (const auto& range : ranges) entry->sizes.push_back(range.bytes);
  const Status s = Transfer(entry.get(), ranges, copy, cancel, false);
  if (!s.ok()) return s;
  bytes_ += bytes;
  entries_.emplace(key, std::move(entry));
  return Status();
}

}  // namespace q4t::server
