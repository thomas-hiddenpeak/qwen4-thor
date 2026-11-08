#include "q4t/server/object_store.h"

#include <dirent.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <map>
#include <utility>
#include <vector>

#include "object_protocol.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
namespace {
using namespace object_detail;
class Fd {
 public:
  explicit Fd(int value = -1) : value_(value) {}
  ~Fd() {
    if (value_ >= 0) close(value_);
  }
  Fd(const Fd&) = delete;
  Fd& operator=(const Fd&) = delete;
  Fd(Fd&& other) noexcept : value_(std::exchange(other.value_, -1)) {}
  Fd& operator=(Fd&& other) noexcept {
    if (value_ >= 0) close(value_);
    value_ = std::exchange(other.value_, -1);
    return *this;
  }
  int Get() const { return value_; }
  explicit operator bool() const { return value_ >= 0; }

 private:
  int value_;
};
int64_t Now() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
ObjectReply IoError(bool uncertain = false) {
  return Error(503, "object_io_error",
               "Durable object storage is unavailable or inconsistent",
               uncertain);
}
bool PrivateFile(int fd, struct stat* info) {
  return !fstat(fd, info) && S_ISREG(info->st_mode) &&
         info->st_uid == getuid() && info->st_nlink == 1 &&
         (info->st_mode & 0777) == 0600;
}
bool WriteAll(int fd, std::string_view text) {
  while (!text.empty()) {
    const auto count = write(fd, text.data(), text.size());
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    text.remove_prefix(count);
  }
  return true;
}
bool ReadAll(int fd, uint64_t limit, std::string* text) {
  struct stat info {};
  if (!PrivateFile(fd, &info) || info.st_size <= 0 ||
      static_cast<uint64_t>(info.st_size) > limit)
    return false;
  text->resize(info.st_size);
  size_t offset = 0;
  while (offset < text->size()) {
    const auto count = read(fd, text->data() + offset, text->size() - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    offset += count;
  }
  char extra;
  return read(fd, &extra, 1) == 0;
}
bool Hex(std::string_view text, size_t size) {
  return text.size() == size &&
         std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
std::string Filename(const Json& record) {
  return record.GetString("owner") + "." + record.GetString("kind") + "." +
         record.GetString("id") + ".json";
}
Json Public(const Json& record) {
  auto result = record;
  std::erase_if(result.object, [](const auto& field) {
    return field.first == "owner" || field.first == "format";
  });
  return result;
}
struct Metadata {
  std::string owner, kind, id, digest;
  int64_t revision = 0, created = 0, expires = 0;
  uint64_t bytes = 0;
};
struct Usage {
  uint64_t bytes = 0, count = 0;
};

class DiskStore {
 public:
  ObjectReply Init(const Json& config) {
    options_.directory = config.GetString("directory");
    options_.max_object_bytes = config.GetInt("max_object_bytes");
    options_.max_client_bytes = config.GetInt("max_client_bytes");
    options_.max_total_bytes = config.GetInt("max_total_bytes");
    options_.max_client_objects = config.GetInt("max_client_objects");
    options_.max_total_objects = config.GetInt("max_total_objects");
    options_.max_ttl_seconds = config.GetInt("max_ttl_seconds");
    if (options_.max_object_bytes < 1024 ||
        options_.max_object_bytes > kMaxFrame / 2 ||
        options_.max_client_bytes < options_.max_object_bytes ||
        options_.max_total_bytes < options_.max_client_bytes ||
        options_.max_total_bytes > (1ULL << 40) ||
        !options_.max_client_objects ||
        options_.max_total_objects < options_.max_client_objects ||
        options_.max_total_objects > 100000 || options_.max_ttl_seconds <= 0 ||
        options_.max_ttl_seconds > 366 * 24 * 3600 || !OpenRoot())
      return IoError();
    lock_ = Fd(openat(root_.Get(), ".q4t-lock",
                      O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK,
                      0600));
    struct stat info {};
    if (!lock_ || !PrivateFile(lock_.Get(), &info)) return IoError();
    if (flock(lock_.Get(), LOCK_EX | LOCK_NB))
      return Error(503, "object_store_owned", "Object directory already owned");
    constexpr std::string_view marker = "q4t-native-object-store-v1\n";
    Fd created(openat(root_.Get(), ".q4t-format",
                      O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
                      0600));
    if (created) {
      if (!WriteAll(created.Get(), marker) || fsync(created.Get()) ||
          fsync(root_.Get()))
        return IoError();
    } else if (errno != EEXIST) {
      return IoError();
    }
    Fd version(openat(root_.Get(), ".q4t-format",
                      O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    std::string format;
    if (!version || !ReadAll(version.Get(), 128, &format) || format != marker)
      return IoError();
    Fd scan(openat(root_.Get(), ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (!scan) return IoError();
    // fdopendir owns its descriptor; root's independent directory offset stays
    // untouched. The scan and all parsing occur only in the isolated worker.
    DIR* directory = fdopendir(dup(scan.Get()));
    if (!directory) return IoError();
    bool valid = true;
    size_t scanned = 0;
    while (true) {
      errno = 0;
      const auto* entry = readdir(directory);
      if (!entry) {
        valid = errno == 0;
        break;
      }
      const std::string name(entry->d_name);
      if (name == "." || name == ".." || name == ".q4t-lock" ||
          name == ".q4t-format")
        continue;
      if (++scanned > 101024) {
        valid = false;
        break;
      }
      if (name.starts_with(".q4t-tmp-") &&
          Hex(std::string_view(name).substr(9), 32)) {
        Fd temp(openat(root_.Get(), name.c_str(),
                       O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
        if (!temp || !PrivateFile(temp.Get(), &info) ||
            unlinkat(root_.Get(), name.c_str(), 0) || fsync(root_.Get())) {
          valid = false;
          break;
        }
        continue;
      }
      Json record;
      Metadata metadata;
      if (!Load(name, &record, &metadata)) {
        valid = false;
        break;
      }
      Add(name, std::move(metadata));
      if (total_.bytes > (1ULL << 40) || records_.size() > 100000) {
        valid = false;
        break;
      }
    }
    closedir(directory);
    if (!valid) return IoError();
    initialized_ = true;
    return Prune();
  }

  ObjectReply Run(const Json& request) {
    if (!initialized_) return IoError();
    const auto operation = request.GetString("operation");
    const auto owner = request.GetString("owner");
    const auto kind = request.GetString("kind");
    const auto id = request.GetString("id");
    if (!Hex(owner, 64) || !ValidKind(kind) ||
        (operation != "health" && operation != "list" && !ValidId(id)))
      return Error(400, "invalid_object_request", "Invalid object identity");
    auto pruned = Prune();
    if (!pruned.ok()) return pruned;
    if (operation == "health") {
      ObjectReply result;
      const auto usage = clients_.find(owner);
      result.value = Object(
          {{"bytes", Number(usage == clients_.end() ? 0 : usage->second.bytes)},
           {"objects",
            Number(usage == clients_.end() ? 0 : usage->second.count)}});
      return result;
    }
    if (operation == "list") return List(request);
    const auto name = owner + "." + kind + "." + id + ".json";
    if (operation == "put") return Put(name, request);
    const auto found = records_.find(name);
    if (found == records_.end())
      return Error(404, "object_not_found", "Object not found");
    Json record;
    if (!ReadKnown(name, found->second, &record)) return IoError();
    ObjectReply result;
    if (operation == "get") {
      result.value = Public(record);
    } else if (operation == "erase") {
      const auto revision = request.GetInt("revision", -1);
      if (revision < 0 || (revision && revision != found->second.revision))
        return Error(409, "object_revision_conflict",
                     "Object revision changed");
      if (unlinkat(root_.Get(), name.c_str(), 0)) return IoError();
      Remove(found);
      if (fsync(root_.Get())) return IoError(true);
      result.value = Object({{"id", String(id)}, {"deleted", Boolean(true)}});
    } else {
      return Error(400, "invalid_object_request", "Unknown object operation");
    }
    return result;
  }

 private:
  bool OpenRoot() {
    const auto& path = options_.directory;
    if (path.empty() || path.front() != '/' || path.size() > 4096 ||
        path.find('\0') != std::string::npos)
      return false;
    Fd current(open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (!current) return false;
    size_t offset = 1;
    bool component = false;
    while (offset < path.size()) {
      const auto end = path.find('/', offset);
      const auto name =
          path.substr(offset, end == std::string::npos ? end : end - offset);
      if (name == "." || name == "..") return false;
      if (!name.empty()) {
        component = true;
        Fd next(openat(current.Get(), name.c_str(),
                       O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (!next && errno == ENOENT) {
          if (mkdirat(current.Get(), name.c_str(), 0700) ||
              fsync(current.Get()))
            return false;
          next = Fd(openat(current.Get(), name.c_str(),
                           O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        }
        if (!next) return false;
        current = std::move(next);
      }
      if (end == std::string::npos) break;
      offset = end + 1;
    }
    struct stat info {};
    if (!component || fstat(current.Get(), &info) || info.st_uid != getuid() ||
        (info.st_mode & 0777) != 0700)
      return false;
    root_ = std::move(current);
    return true;
  }

  bool Load(const std::string& name, Json* record, Metadata* metadata) {
    Fd file(openat(root_.Get(), name.c_str(),
                   O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    std::string text;
    // Existing objects remain retrievable after a configured limit decreases.
    if (!file || !ReadAll(file.Get(), kMaxFrame / 2, &text)) return false;
    Json wrapper;
    if (!Parse(text, &wrapper)) return false;
    const auto* content = wrapper.Find("record");
    if (!content || !content->IsObject() || wrapper.object.size() != 2 ||
        content->object.size() != 8 || content->GetInt("format") != 1 ||
        !Hex(content->GetString("owner"), 64) ||
        !ValidKind(content->GetString("kind")) ||
        !ValidId(content->GetString("id")) || !content->Find("data") ||
        content->GetInt("revision") <= 0 ||
        content->GetInt("created_at") <= 0 ||
        content->GetInt("expires_at") <= content->GetInt("created_at") ||
        Filename(*content) != name)
      return false;
    const auto digest = Digest(OutputJson(*content));
    if (wrapper.GetString("sha256") != digest) return false;
    *metadata = {content->GetString("owner"),   content->GetString("kind"),
                 content->GetString("id"),      digest,
                 content->GetInt("revision"),   content->GetInt("created_at"),
                 content->GetInt("expires_at"), text.size()};
    *record = *content;
    return true;
  }
  bool ReadKnown(const std::string& name, const Metadata& expected,
                 Json* record) {
    Metadata actual;
    return Load(name, record, &actual) && actual.digest == expected.digest &&
           actual.bytes == expected.bytes;
  }
  void Add(const std::string& name, Metadata metadata) {
    auto& client = clients_[metadata.owner];
    client.bytes += metadata.bytes;
    ++client.count;
    total_.bytes += metadata.bytes;
    ++total_.count;
    records_.emplace(name, std::move(metadata));
  }
  using Iterator = std::map<std::string, Metadata>::iterator;
  void Remove(Iterator found) {
    auto client = clients_.find(found->second.owner);
    client->second.bytes -= found->second.bytes;
    if (--client->second.count == 0) clients_.erase(client);
    total_.bytes -= found->second.bytes;
    --total_.count;
    records_.erase(found);
  }
  ObjectReply Prune() {
    const auto now = Now();
    bool removed = false;
    for (auto it = records_.begin(); it != records_.end();) {
      if (it->second.expires > now) {
        ++it;
        continue;
      }
      Json record;
      if (!ReadKnown(it->first, it->second, &record) ||
          unlinkat(root_.Get(), it->first.c_str(), 0))
        return IoError();
      auto expired = it++;
      Remove(expired);
      removed = true;
    }
    if (removed && fsync(root_.Get())) return IoError();
    return {};
  }
  ObjectReply Put(const std::string& name, const Json& request) {
    const auto revision = request.GetInt("revision", -1);
    const auto ttl = request.GetInt("ttl");
    const auto* data = request.Find("data");
    if (revision < 0 || revision >= (1LL << 53) - 1 || !data || ttl <= 0 ||
        ttl > options_.max_ttl_seconds)
      return Error(400, "invalid_object_request", "Invalid object write");
    const auto found = records_.find(name);
    const bool exists = found != records_.end();
    if ((revision == 0 && exists) ||
        (revision > 0 && (!exists || found->second.revision != revision)))
      return Error(409, "object_revision_conflict", "Object revision changed");
    Json previous;
    if (exists && !ReadKnown(name, found->second, &previous)) return IoError();
    // No unindexed file may be overwritten, even if another process ignored
    // our advisory lock. Only a successfully read expected revision replaces.
    struct stat info {};
    if (!exists &&
        (!fstatat(root_.Get(), name.c_str(), &info, AT_SYMLINK_NOFOLLOW) ||
         errno != ENOENT))
      return IoError();
    const auto now = Now();
    const auto created = exists ? found->second.created : now;
    const auto expires = std::max(now, created) + ttl;
    const auto owner = request.GetString("owner");
    auto record = Object({{"format", Number(1)},
                          {"owner", String(owner)},
                          {"kind", String(request.GetString("kind"))},
                          {"id", String(request.GetString("id"))},
                          {"revision", Number(revision + 1)},
                          {"created_at", Number(created)},
                          {"expires_at", Number(expires)},
                          {"data", *data}});
    const auto digest = Digest(OutputJson(record));
    const auto text =
        OutputJson(Object({{"record", record}, {"sha256", String(digest)}}));
    if (text.size() > options_.max_object_bytes)
      return Error(413, "object_too_large", "Stored object exceeds byte limit");
    const auto client = clients_.find(owner);
    const auto usage = client == clients_.end() ? Usage{} : client->second;
    const uint64_t old_bytes = exists ? found->second.bytes : 0;
    const uint64_t increment = exists ? 0 : 1;
    if (usage.bytes - old_bytes + text.size() > options_.max_client_bytes ||
        total_.bytes - old_bytes + text.size() > options_.max_total_bytes ||
        usage.count + increment > options_.max_client_objects ||
        total_.count + increment > options_.max_total_objects)
      return Error(429, "object_quota_exceeded",
                   "Object storage quota exceeded");
    const auto random = NewObjectId(ObjectKind::kResponse);
    if (random.empty()) return IoError();
    const auto temporary = ".q4t-tmp-" + random.substr(random.size() - 32);
    Fd file(openat(root_.Get(), temporary.c_str(),
                   O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (!file) return IoError();
    if (!WriteAll(file.Get(), text) || fsync(file.Get())) {
      unlinkat(root_.Get(), temporary.c_str(), 0);
      return IoError();
    }
    if (renameat(root_.Get(), temporary.c_str(), root_.Get(), name.c_str())) {
      unlinkat(root_.Get(), temporary.c_str(), 0);
      return IoError();
    }
    if (exists) Remove(found);
    Add(name, {owner, request.GetString("kind"), request.GetString("id"),
               digest, revision + 1, created, expires, text.size()});
    if (fsync(root_.Get())) return IoError(true);
    ObjectReply result;
    result.value = Public(record);
    return result;
  }
  ObjectReply List(const Json& request) {
    const auto limit = request.GetInt("limit");
    const auto after = request.GetString("after"),
               before = request.GetString("before");
    if (limit < 1 || limit > 100 || (!after.empty() && !before.empty()) ||
        (!after.empty() && !ValidId(after)) ||
        (!before.empty() && !ValidId(before)))
      return Error(400, "invalid_object_request", "Invalid object cursor");
    std::vector<std::pair<std::string, Metadata>> selected;
    for (const auto& entry : records_)
      if (entry.second.owner == request.GetString("owner") &&
          entry.second.kind == request.GetString("kind"))
        selected.push_back(entry);
    const bool descending = request.GetBool("descending", true);
    std::sort(selected.begin(), selected.end(),
              [&](const auto& a, const auto& b) {
                const auto left = std::pair(a.second.created, a.second.id);
                const auto right = std::pair(b.second.created, b.second.id);
                return descending ? left > right : left < right;
              });
    size_t begin = 0, end = selected.size();
    if (!after.empty() || !before.empty()) {
      const auto& cursor = after.empty() ? before : after;
      auto position = std::find_if(
          selected.begin(), selected.end(),
          [&](const auto& value) { return value.second.id == cursor; });
      if (position == selected.end())
        return Error(400, "invalid_object_cursor",
                     "Object cursor not found in this list");
      const auto index = static_cast<size_t>(position - selected.begin());
      if (!after.empty())
        begin = index + 1;
      else
        end = index;
    }
    Json items = Array();
    size_t index = begin;
    uint64_t bytes = 0;
    for (; index < end && items.array.size() < static_cast<size_t>(limit);
         ++index) {
      const auto& [name, metadata] = selected[index];
      if (bytes + metadata.bytes > kMaxFrame / 2 && !items.array.empty()) break;
      Json record;
      if (!ReadKnown(name, metadata, &record)) return IoError();
      bytes += metadata.bytes;
      items.array.push_back(Public(record));
    }
    auto first = items.array.empty()
                     ? Json{}
                     : String(items.array.front().GetString("id"));
    auto last = items.array.empty()
                    ? Json{}
                    : String(items.array.back().GetString("id"));
    ObjectReply result;
    result.value = Object({{"object", String("list")},
                           {"data", std::move(items)},
                           {"first_id", std::move(first)},
                           {"last_id", std::move(last)},
                           {"has_more", Boolean(index < end)}});
    return result;
  }
  ObjectStoreOptions options_;
  Fd root_, lock_;
  bool initialized_ = false;
  std::map<std::string, Metadata> records_;
  std::map<std::string, Usage> clients_;
  Usage total_;
};
}  // namespace

int MaybeRunObjectWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool matched = std::strcmp(argv[1], "--q4t-object-worker") == 0;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  const bool lose_ack =
      std::strcmp(argv[1], "--q4t-object-worker-lost-ack") == 0;
  matched = matched || lose_ack;
#endif
  if (!matched) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    DiskStore store;
    uint64_t expected = 1;
    bool initialized = false;
    for (;;) {
      Frame frame;
      if (!detail::WorkerTransfer(&frame, sizeof(frame), false)) return 0;
      if (frame.version != kVersion || frame.sequence != expected++ ||
          !frame.bytes || frame.bytes > kMaxFrame)
        return 3;
      std::string body(frame.bytes, '\0');
      if (!detail::WorkerTransfer(body.data(), body.size(), false)) return 3;
      Json request;
      if (!Parse(body, &request) || !request.IsObject()) return 3;
      ObjectReply reply;
      if (!initialized) {
        if (request.GetString("operation") != "init") return 3;
        reply = store.Init(request);
        initialized = reply.ok();
      } else {
        reply = store.Run(request);
      }
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      if (lose_ack && reply.ok() && request.GetString("operation") == "put") {
        for (;;) pause();  // Durable commit succeeded; receipt is lost.
      }
#endif
      auto response = OutputJson(WireReply(reply));
      if (response.empty() || response.size() > kMaxFrame) return 3;
      Frame header{kVersion, frame.sequence, response.size()};
      if (!detail::WorkerTransfer(&header, sizeof(header), true) ||
          !detail::WorkerTransfer(response.data(), response.size(), true))
        return 0;
      if (reply.http_status >= 500) return 4;
    }
  } catch (...) {
    // Parent retains mutation uncertainty on a missing receipt.
    return 5;
  }
}
}  // namespace q4t::server
