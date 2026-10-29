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

Status SessionCache::Open(const std::string& directory, int min_prefix,
                           size_t disk_budget, bool incremental) {
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
  incremental_ = incremental;
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
  bytes_ -= std::max(it->second->bytes, it->second->disk_charge);
  entries_.erase(it);
}
void SessionCache::Release(const std::string& key, bool success) {
  const std::lock_guard lock(mutex_);
  auto it = entries_.find(key);
  if (it != entries_.end() && !it->second->published) {
    if (success) it->second->published = true;
    else Erase(it);
  }
  // Reuse requires a complete Restore in this lease, never a previous one.
  it = entries_.find(key);
  if (it != entries_.end()) it->second->restored = false;
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
  size_t offset = 0;
  size_t block = 0;
  for (const auto& range : ranges) {
    for (size_t start = 0; start < range.bytes;) {
      if (cancel()) return Status::Fail("session cache cancelled");
      const size_t size = std::min(kStagingBytes, range.bytes - start);
      const size_t disk_size = Aligned(size);
      auto* state = static_cast<uint8_t*>(range.data) + start;
      if (restore) {
        if (!entry->offsets.empty() && block >= entry->offsets.size())
          return Status::Fail("session cache offset count");
        const size_t disk_offset =
            entry->offsets.empty() ? offset : entry->offsets[block];
        ssize_t got;
        do {
          got = pread(entry->fd, staging_, disk_size, disk_offset);
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

// Recheck every byte, including recurrent state and the shifted draft tail.
// Equality only avoids disk writes; no tensor is assumed immutable. A private
// lease plus the cache/model locks excludes observers during in-place updates.
// Any failure destroys the detached file, so no partially updated entry can
// become visible. A file owns all blocks: no ancestor chains or shared fds.
Status SessionCache::SaveIncremental(
    Entry* entry, std::span<const SessionRange> ranges, const Copy& copy,
    const Cancel& cancel, SessionSaveTiming* timing) {
  std::vector<size_t> offsets;
  std::vector<trace::Digest> digests;
  offsets.reserve(8192);
  digests.reserve(8192);
  size_t old_block = 0;
  if (timing) timing->incremental = true;
  for (size_t r = 0; r < ranges.size(); ++r) {
    const auto& range = ranges[r];
    const size_t old_size = r < entry->sizes.size() ? entry->sizes[r] : 0;
    const size_t old_count = (old_size + kStagingBytes - 1) / kStagingBytes;
    for (size_t start = 0, part = 0; start < range.bytes;
         start += kStagingBytes, ++part) {
      if (cancel()) return Status::Fail("session cache cancelled");
      const size_t size = std::min(kStagingBytes, range.bytes - start);
      const size_t disk_size = Aligned(size);
      Status status;
      {
        SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCopy);
        status = copy(staging_, static_cast<uint8_t*>(range.data) + start,
                        size, false);
      }
      if (!status.ok()) return status;
      {
        SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPadding);
        std::memset(static_cast<uint8_t*>(staging_) + size, 0,
                      disk_size - size);
      }
      trace::Digest digest;
      {
        SessionSaveTiming::Scope span(timing, SessionSaveTiming::kHash);
        digest = trace::Sha256({static_cast<uint8_t*>(staging_), disk_size});
      }
      const bool existing = part < old_count;
      const size_t index = old_block + part;
      if (existing && (index >= entry->offsets.size() ||
                       index >= entry->digests.size()))
        return Status::Fail("session cache incremental manifest");
      const size_t offset = existing ? entry->offsets[index]
                                     : entry->next_offset;
      if (!existing) entry->next_offset += kStagingBytes;
      const bool same = existing &&
          size == std::min(kStagingBytes, old_size - start) &&
          digest == entry->digests[index];
      if (same) {
        if (timing) {
          timing->reused_bytes += disk_size;
          ++timing->reused_blocks;
        }
      } else {
        if (cancel()) return Status::Fail("session cache cancelled");
        ssize_t wrote;
        {
          SessionSaveTiming::Scope span(timing, SessionSaveTiming::kWrite);
          do {
            wrote = pwrite(entry->fd, staging_, disk_size, offset);
          } while (wrote < 0 && errno == EINTR);
        }
        if (wrote != static_cast<ssize_t>(disk_size))
          return IoError("incremental write/short write");
        if (timing) {
          timing->data_bytes += size;
          timing->disk_bytes += disk_size;
        }
      }
      if (timing) timing->scanned_bytes += size;
      offsets.push_back(offset);
      digests.push_back(digest);
    }
    old_block += old_count;
  }
  if (cancel()) return Status::Fail("session cache cancelled");
  struct stat st{};
  if (fstat(entry->fd, &st)) return IoError("incremental stat");
  const size_t allocated = static_cast<size_t>(st.st_blocks) * 512;
  if (allocated > entry->bytes + (size_t{64} << 20))
    return Status::Fail("session cache filesystem allocation limit");
  entry->disk_charge = std::max(entry->bytes, allocated);
  entry->offsets = std::move(offsets);
  entry->digests = std::move(digests);
  entry->sizes.clear();
  for (const auto& range : ranges) entry->sizes.push_back(range.bytes);
  if (timing) {
    timing->file_span = st.st_size;
    timing->allocated_bytes = allocated;
  }
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
  if (s.ok()) entry.restored = true;
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
  std::unique_ptr<Entry> entry;
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kMetadata);
    if (!busy_.contains(key) || prefix.size() <= size_t(min_prefix_) ||
        prefix.size() > kMaxPrefix + 1 || ranges.empty())
      return Status::Fail("session cache save admission");
    if (incremental_ && ranges.size() > 1024)
      return Status::Fail("session cache incremental range limit");
    size_t blocks = 0;
    for (const auto& range : ranges) {
      if (!range.data || !range.bytes || range.bytes > disk_budget_ ||
          Aligned(range.bytes) > disk_budget_ - bytes)
        return Status::Fail("session cache save range/budget");
      bytes += Aligned(range.bytes);
      blocks += (range.bytes + kStagingBytes - 1) / kStagingBytes;
    }
    if (incremental_ && blocks > 8192)
      return Status::Fail("session cache incremental block limit");
    auto old = entries_.find(key);
    if (incremental_ && old != entries_.end()) {
      const auto& prior = *old->second;
      bool eligible = prior.published && prior.restored &&
          !prior.offsets.empty() && prior.sizes.size() == ranges.size() &&
          prior.prefix.size() <= prefix.size() &&
          std::equal(prior.prefix.begin(), prior.prefix.end() - 1,
                       prefix.begin());
      for (size_t i = 0; eligible && i < ranges.size(); ++i)
        eligible = ranges[i].bytes >= prior.sizes[i];
      if (eligible) {
        bytes_ -= std::max(prior.bytes, prior.disk_charge);
        entry = std::move(old->second);
        entries_.erase(old);
        entry->published = false;
        entry->restored = false;
      }
    }
    // The old prefix is no longer needed by this request after restoration.
    // Losing a cache entry on a failed replacement is a miss, never stale
    // reuse.
    if (auto old = entries_.find(key); old != entries_.end()) Erase(old);
    // Reserve bounded filesystem allocation overhead before writing. The
    // final charge uses fstat, including unwritten extents, never sparse EOF.
    const size_t reserve = incremental_ ? (size_t{64} << 20) : 0;
    if (bytes > disk_budget_ || reserve > disk_budget_ - bytes)
      return Status::Fail("session cache incremental disk reserve");
    while (entries_.size() >= kMaxEntries ||
           bytes + reserve > disk_budget_ - bytes_) {
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
  if (!entry) entry = std::make_unique<Entry>();
  if (entry->fd < 0) {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kOpen);
    entry->fd = openat(directory_fd_, ".",
                       O_TMPFILE | O_DIRECT | O_RDWR | O_CLOEXEC, 0600);
  }
  if (entry->fd < 0) return IoError("snapshot file");
  {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kMetadata);
    entry->bytes = bytes;
    entry->d0 = d0;
    entry->prefix.assign(prefix.begin(), prefix.end());
    entry->used = std::chrono::steady_clock::now();
    if (!incremental_)
      for (const auto& range : ranges) entry->sizes.push_back(range.bytes);
  }
  const Status s = incremental_
      ? SaveIncremental(entry.get(), ranges, copy, cancel, timing)
      : Transfer(entry.get(), ranges, copy, cancel, false, timing);
  if (!s.ok()) {
    SessionSaveTiming::Scope span(timing, SessionSaveTiming::kCleanup);
    entry.reset();
    return s;
  }
  SessionSaveTiming::Scope span(timing, SessionSaveTiming::kPublish);
  bytes_ += std::max(bytes, entry->disk_charge);
  entries_.emplace(key, std::move(entry));
  return Status();
}

}  // namespace q4t::server
