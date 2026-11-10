#include "file_legacy_workspace.h"

#include <dirent.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <string_view>
#include <utility>
#include <vector>

#include "q4t/server/lifetime.h"

namespace q4t::server::legacy_detail {
namespace {
constexpr std::string_view kMarker = "q4t-private-document-work-v1\n";
constexpr const char* kLease = ".q4t-codec-root";
bool Token(std::string_view value) {
  if (value.size() != 32) return false;
  for (char c : value)
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  return true;
}
bool Owned(int fd, bool directory) {
  struct stat info {};
  return fstat(fd, &info) == 0 && info.st_uid == getuid() &&
         (directory ? S_ISDIR(info.st_mode) && (info.st_mode & 0777) == 0700
                    : S_ISREG(info.st_mode) && info.st_nlink == 1 &&
                          (info.st_mode & 0777) == 0600);
}
int OpenRoot(std::string_view path) {
  if (path.empty() || path.front() != '/' || path.size() > 4096 ||
      path.find('\0') != path.npos)
    return -1;
  int current = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (current < 0) return -1;
  ScopeExit cleanup([&] {
    if (current >= 0) close(current);
  });
  size_t offset = 1, components = 0;
  while (offset < path.size()) {
    const auto end = path.find('/', offset);
    const std::string name(
        path.substr(offset, end == path.npos ? end : end - offset));
    if (name.empty() || name == "." || name == ".." || ++components > 256)
      return -1;
    int next = openat(current, name.c_str(),
                      O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (next < 0 && errno == ENOENT) {
      if (mkdirat(current, name.c_str(), 0700)) return -1;
      next = openat(current, name.c_str(),
                    O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    }
    if (next < 0) return -1;
    close(current);
    current = next;
    if (end == path.npos) break;
    offset = end + 1;
  }
  if (!components || !Owned(current, true)) return -1;
  return std::exchange(current, -1);
}
bool Names(int directory, std::vector<std::string>* output, size_t limit) {
  output->clear();
  const int fd = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (fd < 0) return false;
  DIR* entries = fdopendir(fd);
  if (!entries) {
    close(fd);
    return false;
  }
  ScopeExit cleanup([&] { closedir(entries); });
  for (;;) {
    errno = 0;
    const auto* entry = readdir(entries);
    if (!entry) return errno == 0;
    const std::string_view name(entry->d_name);
    if (name == "." || name == "..") continue;
    if (output->size() >= limit) return false;
    output->emplace_back(name);
  }
}
bool RemoveChildren(int directory, size_t* remaining) {
  struct Level {
    std::vector<std::string> names;
    size_t next = 0;
    std::string in_parent;
    dev_t device = 0;
    ino_t inode = 0;
  };
  int current = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (current < 0) return false;
  ScopeExit cleanup([&] { close(current); });
  std::vector<Level> levels;
  const auto enter = [&](std::string name) {
    struct stat info {};
    if (fstat(current, &info) || info.st_uid != getuid()) return false;
    Level level;
    level.in_parent = std::move(name);
    level.device = info.st_dev;
    level.inode = info.st_ino;
    if (!Names(current, &level.names, *remaining)) return false;
    levels.push_back(std::move(level));
    return true;
  };
  if (!enter("")) return false;
  while (!levels.empty()) {
    auto& level = levels.back();
    if (level.next == level.names.size()) {
      if (levels.size() == 1) return true;
      const auto name = level.in_parent;
      const auto& expected_parent = levels[levels.size() - 2];
      const int parent = openat(
          current, "..", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
      if (parent < 0) return false;
      struct stat info {};
      if (fstat(parent, &info) || info.st_dev != expected_parent.device ||
          info.st_ino != expected_parent.inode) {
        close(parent);
        return false;
      }
      close(current);
      current = parent;
      if (unlinkat(current, name.c_str(), AT_REMOVEDIR)) return false;
      levels.pop_back();
      continue;
    }
    const auto name = level.names[level.next++];
    if (!*remaining) return false;
    --*remaining;
    struct stat info {};
    if (fstatat(current, name.c_str(), &info, AT_SYMLINK_NOFOLLOW) ||
        info.st_uid != getuid())
      return false;
    if (S_ISDIR(info.st_mode)) {
      const int child = openat(current, name.c_str(),
                               O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      if (child < 0) return false;
      close(current);
      current = child;
      if (!enter(name)) return false;
    } else if (unlinkat(current, name.c_str(), 0))
      return false;
  }
  return true;
}
}  // namespace

Workspace::~Workspace() {
  if (directory_ >= 0) close(directory_);
  if (lease_ >= 0 && !keep_lease_) close(lease_);
  if (root_ >= 0) close(root_);
}
bool Workspace::Open(std::string root) {
  if (root_ >= 0 || lease_ >= 0) return false;
  root_ = OpenRoot(root);
  if (root_ < 0) return false;
  std::vector<std::string> names;
  if (!Names(root_, &names, 130)) return false;
  bool initialized = false;
  for (const auto& name : names) {
    if (name == kLease)
      initialized = true;
    else if (!name.starts_with("codec-") || !Token(name.substr(6)))
      return false;
  }
  if (!initialized && !names.empty()) return false;
  lease_ = openat(root_, kLease,
                  O_RDWR | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK |
                      (initialized ? 0 : O_CREAT | O_EXCL),
                  0600);
  if (lease_ < 0 || !Owned(lease_, false) || flock(lease_, LOCK_EX | LOCK_NB))
    return false;
  if (!initialized) {
    if (write(lease_, kMarker.data(), kMarker.size()) !=
            static_cast<ssize_t>(kMarker.size()) ||
        fsync(lease_) || fsync(root_))
      return false;
  }
  std::array<char, 128> marker{};
  auto size = pread(lease_, marker.data(), marker.size(), 0);
  // A killed first initializer may leave a short marker with no workspace.
  // Repair only a prefix of our own marker under the acquired exclusive lease.
  if (size >= 0 && size < static_cast<ssize_t>(kMarker.size()) &&
      names.size() == 1 && names[0] == kLease &&
      kMarker.starts_with(std::string_view(marker.data(), size))) {
    if (pwrite(lease_, kMarker.data(), kMarker.size(), 0) !=
            static_cast<ssize_t>(kMarker.size()) ||
        ftruncate(lease_, kMarker.size()) || fsync(lease_))
      return false;
    size = pread(lease_, marker.data(), marker.size(), 0);
  }
  if (size != static_cast<ssize_t>(kMarker.size()) ||
      std::string_view(marker.data(), size) != kMarker)
    return false;
  root_path_ = std::move(root);
  while (root_path_.ends_with('/')) root_path_.pop_back();
  return true;
}
bool Workspace::Clean() {
  if (root_ < 0 || lease_ < 0) return false;
  if (directory_ >= 0) close(std::exchange(directory_, -1));
  std::vector<std::string> names;
  if (!Names(root_, &names, 130)) return false;
  size_t remaining = 66000;
  for (const auto& name : names) {
    if (name == kLease) continue;
    if (!name.starts_with("codec-") || !Token(name.substr(6))) return false;
    const int child = openat(root_, name.c_str(),
                             O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (child < 0) return false;
    const bool ok = Owned(child, true) && RemoveChildren(child, &remaining);
    close(child);
    if (!ok || unlinkat(root_, name.c_str(), AT_REMOVEDIR)) return false;
  }
  path_.clear();
  return true;
}
bool Workspace::Create(const std::string& token) {
  if (root_ < 0 || lease_ < 0 || directory_ >= 0 || !Token(token)) return false;
  const auto name = "codec-" + token;
  if (mkdirat(root_, name.c_str(), 0700)) return false;
  directory_ = openat(root_, name.c_str(),
                      O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_ < 0 || !Owned(directory_, true)) return false;
  path_ = root_path_ + "/" + name;
  return true;
}
}  // namespace q4t::server::legacy_detail
