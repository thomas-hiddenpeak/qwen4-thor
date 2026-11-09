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
#include <set>
#include <utility>
#include <vector>

#include "object_protocol.h"
#include "q4t/server/grammar_process.h"
#include "q4t/server/object_retention.h"

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
ObjectReply Times(const Json& request, int64_t now, int64_t existing_created,
                   int64_t max_ttl, int64_t* created, int64_t* expires) {
  const auto requested = request.GetInt("created_at", 0);
  const auto absolute = request.GetInt("expires_at", -1);
  const auto ttl = request.GetInt("ttl", -1);
  if (requested < 0 || requested > now || absolute < -1 || ttl < 0 ||
      ttl > max_ttl || (existing_created && requested &&
                        existing_created != requested))
    return Error(400, "invalid_object_request", "Invalid object timestamps");
  *created = existing_created ? existing_created : requested ? requested : now;
  const auto base = std::max(now, *created);
  *expires = absolute >= 0 ? absolute : ttl == 0 ? 0 : base + ttl;
  if (*expires && (*expires <= *created || *expires > base + max_ttl))
    return Error(400, "invalid_object_request", "Invalid object expiration");
  if (*expires && *expires <= now)
    return Error(404, "object_not_found", "Object retention has expired");
  return {};
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
bool ValidFilename(std::string_view name) {
  if (name.size() < 72 || !name.ends_with(".json") || name[64] != '.')
    return false;
  const auto split = name.find('.', 65);
  return split != name.npos && Hex(name.substr(0, 64), 64) &&
         ValidKind(name.substr(65, split - 65)) &&
         ValidId(name.substr(split + 1, name.size() - split - 6));
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
  ObjectRetentionInfo retention;
};
struct Usage {
  uint64_t bytes = 0, count = 0;
};

class DiskStore {
 public:
  explicit DiskStore(int pause_after = 0) : pause_after_(pause_after) {}
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
    if (!Recover()) return IoError();
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
    // Retention inspect must see expired records before proposing collection.
    // Ordinary operations still invoke bounded automatic pruning below.
    return {};
  }

  ObjectReply Run(const Json& request) {
    if (!initialized_) return IoError();
    const auto operation = request.GetString("operation");
    if (operation == "retention") return Retention(request);
    if (operation == "scan_responses") {
      auto pruned = Prune();
      return pruned.ok() ? ScanResponses(request) : pruned;
    }
    if (operation == "batch") {
      auto pruned = Prune();
      return pruned.ok() ? Batch(request) : pruned;
    }
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
      if (request.GetInt("mutate_before", 0))
        return Batch(Object({{"mutations", Array({request})}}));
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
  // The durable journal is the commit decision. No reader is admitted until
  // all its already-committed file replacements finish. Recovery applies only
  // storage bytes, never model generation or tool side effects.
  bool Publish(const std::string& name, std::string_view text,
               bool* published) {
    *published = false;
    const auto random = NewObjectId(ObjectKind::kResponse);
    if (random.empty()) return false;
    const auto temporary = ".q4t-tmp-" + random.substr(random.size() - 32);
    Fd file(openat(root_.Get(), temporary.c_str(),
                   O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (!file) return false;
    if (!WriteAll(file.Get(), text) || fsync(file.Get()) ||
        renameat(root_.Get(), temporary.c_str(), root_.Get(), name.c_str())) {
      unlinkat(root_.Get(), temporary.c_str(), 0);
      return false;
    }
    *published = true;
    return true;
  }
  bool Apply(const Json& transaction, bool recovery) {
    const auto* changes = transaction.GetArray("changes");
    const auto owner = transaction.GetString("owner");
    if (transaction.GetInt("format") != 1 || !Hex(owner, 64) ||
        !ValidId(transaction.GetString("id")) || !changes ||
        changes->array.empty() ||
        changes->array.size() > kMaxTransactionObjects)
      return false;
    std::set<std::string> targets;
    std::vector<std::string> encoded;
    for (const auto& change : changes->array) {
      const auto name = change.GetString("name");
      const auto old = change.GetString("old_sha256");
      const auto* record = change.Find("record");
      if (!ValidFilename(name) || name.substr(0, 64) != owner ||
          !targets.insert(name).second || (!old.empty() && !Hex(old, 64)) ||
          !record || (!record->IsObject() && !record->IsNull()))
        return false;
      if (record->IsNull()) {
        if (old.empty()) return false;
        encoded.emplace_back();
      } else {
        auto text = OutputJson(
            Object({{"record", *record},
                    {"sha256", String(Digest(OutputJson(*record)))}}));
        Json verified;
        Metadata ignored;
        if (!DecodeRecord(name, text, &verified, &ignored)) return false;
        encoded.push_back(std::move(text));
      }
    }
    for (size_t i = 0; i < changes->array.size(); ++i) {
      const auto& change = changes->array[i];
      const auto name = change.GetString("name");
      const auto old = change.GetString("old_sha256");
      const auto* record = change.Find("record");
      struct stat info {};
      const bool exists =
          !fstatat(root_.Get(), name.c_str(), &info, AT_SYMLINK_NOFOLLOW);
      if (!exists && errno != ENOENT) return false;
      Json current;
      Metadata metadata;
      if (exists && !Load(name, &current, &metadata)) return false;
      if (record->IsNull()) {
        if (exists &&
            (metadata.digest != old || unlinkat(root_.Get(), name.c_str(), 0)))
          return false;
      } else {
        const auto digest = Digest(OutputJson(*record));
        if (!exists || metadata.digest != digest) {
          if ((exists && (old.empty() || metadata.digest != old)) ||
              (!exists && !old.empty()))
            return false;
          bool published;
          if (!Publish(name, encoded[i], &published)) return false;
        }
      }
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
      if (!recovery && pause_after_ == static_cast<int>(i + 1)) {
        for (;;) pause();
      }
#else
      (void)recovery;
#endif
    }
    return !fsync(root_.Get()) &&
           !unlinkat(root_.Get(), ".q4t-transaction", 0) && !fsync(root_.Get());
  }
  bool Recover() {
    Fd file(openat(root_.Get(), ".q4t-transaction",
                   O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    if (!file) return errno == ENOENT;
    std::string text;
    Json wrapper;
    if (!ReadAll(file.Get(), kMaxFrame, &text) || !Parse(text, &wrapper))
      return false;
    const auto* transaction = wrapper.Find("transaction");
    return transaction &&
           wrapper.GetString("sha256") == Digest(OutputJson(*transaction)) &&
           Apply(*transaction, true);
  }
  ObjectReply Batch(const Json& request) {
    const auto* mutations = request.GetArray("mutations");
    if (!mutations || mutations->array.empty() ||
        mutations->array.size() > kMaxTransactionObjects)
      return Error(400, "invalid_object_request", "Invalid transaction size");
    const auto owner = mutations->array.front().GetString("owner");
    if (!Hex(owner, 64))
      return Error(400, "invalid_object_request", "Invalid transaction owner");
    auto projected = total_;
    const auto client = clients_.find(owner);
    auto client_projected = client == clients_.end() ? Usage{} : client->second;
    const auto client_original = client_projected;
    std::set<std::string> targets;
    Json changes = Array(), results = Array();
    const auto now = Now();
    for (const auto& mutation : mutations->array) {
      const auto operation = mutation.GetString("operation");
      const auto kind = mutation.GetString("kind"),
                 id = mutation.GetString("id");
      const auto revision = mutation.GetInt("revision", -1);
      if (mutation.GetString("owner") != owner || !ValidKind(kind) ||
          !ValidId(id) || revision < 0 || revision >= (1LL << 53) - 1 ||
          (operation != "put" && operation != "erase"))
        return Error(400, "invalid_object_request",
                     "Invalid transaction mutation");
      const auto name = owner + "." + kind + "." + id + ".json";
      if (!targets.insert(name).second)
        return Error(400, "invalid_object_request",
                     "Duplicate transaction target");
      const auto found = records_.find(name);
      const bool exists = found != records_.end();
      if (operation == "erase" && !exists)
        return Error(404, "object_not_found", "Object not found");
      if ((operation == "put" && revision == 0 && exists) ||
          (revision > 0 && (!exists || found->second.revision != revision)))
        return Error(409, "object_revision_conflict",
                     "Object revision changed");
      Json previous;
      if (exists && !ReadKnown(name, found->second, &previous))
        return IoError();
      struct stat info {};
      if (!exists &&
          (!fstatat(root_.Get(), name.c_str(), &info, AT_SYMLINK_NOFOLLOW) ||
           errno != ENOENT))
        return IoError();
      if (exists) {
        projected.bytes -= found->second.bytes;
        client_projected.bytes -= found->second.bytes;
        --projected.count;
        --client_projected.count;
      }
      Json record;
      if (operation == "put") {
        const auto ttl = mutation.GetInt("ttl", -1);
        const auto* data = mutation.Find("data");
        if (!data || ttl < 0 || ttl > options_.max_ttl_seconds)
          return Error(400, "invalid_object_request", "Invalid object write");
        int64_t created = 0, expires = 0;
        auto timing = Times(mutation, now, exists ? found->second.created : 0,
                             options_.max_ttl_seconds, &created, &expires);
        if (!timing.ok()) return timing;
        record = Object({{"format", Number(1)},
                         {"owner", String(owner)},
                         {"kind", String(kind)},
                         {"id", String(id)},
                         {"revision", Number(revision + 1)},
                         {"created_at", Number(created)},
                         {"expires_at", Number(expires)},
                         {"data", *data}});
        const auto bytes =
            OutputJson(Object({{"record", record},
                               {"sha256", String(Digest(OutputJson(record)))}}))
                .size();
        if (bytes > options_.max_object_bytes)
          return Error(413, "object_too_large",
                       "Stored object exceeds byte limit");
        projected.bytes += bytes;
        client_projected.bytes += bytes;
        ++projected.count;
        ++client_projected.count;
        results.array.push_back(Public(record));
      } else {
        results.array.push_back(
            Object({{"id", String(id)}, {"deleted", Boolean(true)}}));
      }
      changes.array.push_back(
          Object({{"name", String(name)},
                  {"old_sha256", String(exists ? found->second.digest : "")},
                  {"record", std::move(record)}}));
    }
    if ((projected.bytes > options_.max_total_bytes &&
         projected.bytes > total_.bytes) ||
        (projected.count > options_.max_total_objects &&
         projected.count > total_.count) ||
        (client_projected.bytes > options_.max_client_bytes &&
         client_projected.bytes > client_original.bytes) ||
        (client_projected.count > options_.max_client_objects &&
         client_projected.count > client_original.count))
      return Error(429, "object_quota_exceeded",
                   "Transaction exceeds storage quota");
    const auto id = NewObjectId(ObjectKind::kToolRun);
    if (id.empty()) return IoError();
    auto transaction = Object({{"format", Number(1)},
                               {"id", String(id)},
                               {"owner", String(owner)},
                               {"changes", std::move(changes)}});
    const auto journal = OutputJson(
        Object({{"transaction", transaction},
                {"sha256", String(Digest(OutputJson(transaction)))}}));
    if (journal.size() > kMaxFrame)
      return Error(413, "object_too_large",
                   "Transaction journal exceeds byte limit");
    struct stat pending {};
    if (!fstatat(root_.Get(), ".q4t-transaction", &pending,
                 AT_SYMLINK_NOFOLLOW) ||
        errno != ENOENT)
      return IoError();
    for (const auto& mutation : mutations->array) {
      const auto cutoff = mutation.GetInt("mutate_before", 0);
      if (cutoff < 0 || (cutoff && Now() >= cutoff))
        return Error(409, "object_mutation_expired",
                     "Resource mutation deadline expired");
    }
    bool published;
    if (!Publish(".q4t-transaction", journal, &published))
      return IoError(published);
    if (fsync(root_.Get())) return IoError(true);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    if (pause_after_ == -1) {
      for (;;) pause();
    }
#endif
    if (!Apply(transaction, false)) return IoError(true);
    for (const auto& change : transaction.GetArray("changes")->array) {
      const auto name = change.GetString("name");
      const auto old = records_.find(name);
      if (old != records_.end()) Remove(old);
      if (!change.Find("record")->IsNull()) {
        Json record;
        Metadata metadata;
        if (!Load(name, &record, &metadata)) return IoError(true);
        Add(name, std::move(metadata));
      }
    }
    ObjectReply result;
    result.value =
        Object({{"commit_id", String(id)}, {"data", std::move(results)}});
    return result;
  }
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
    return DecodeRecord(name, text, record, metadata);
  }
  bool DecodeRecord(const std::string& name, const std::string& text,
                    Json* record, Metadata* metadata) {
    if (text.size() > kMaxFrame / 2) return false;
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
        (content->GetInt("expires_at", -1) != 0 &&
         content->GetInt("expires_at") <= content->GetInt("created_at")) ||
        Filename(*content) != name)
      return false;
    const auto digest = Digest(OutputJson(*content));
    if (wrapper.GetString("sha256") != digest) return false;
    *metadata = {content->GetString("owner"),
                 content->GetString("kind"),
                 content->GetString("id"),
                 digest,
                 content->GetInt("revision"),
                 content->GetInt("created_at"),
                 content->GetInt("expires_at"),
                 text.size(),
                 InspectObjectRetention(content->GetString("kind"),
                                        *content->Find("data"))};
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
    if (metadata.retention.Bytes() > 64 * 1024 * 1024 - retention_bytes_) {
      metadata.retention.uncertain = true;
      std::vector<std::string>().swap(metadata.retention.references);
    }
    retention_bytes_ += metadata.retention.Bytes();
    auto& client = clients_[metadata.owner];
    client.bytes += metadata.bytes;
    ++client.count;
    total_.bytes += metadata.bytes;
    ++total_.count;
    records_.emplace(name, std::move(metadata));
  }
  using Iterator = std::map<std::string, Metadata>::iterator;
  void Remove(Iterator found) {
    retention_bytes_ -= found->second.retention.Bytes();
    auto client = clients_.find(found->second.owner);
    client->second.bytes -= found->second.bytes;
    if (--client->second.count == 0) clients_.erase(client);
    total_.bytes -= found->second.bytes;
    --total_.count;
    records_.erase(found);
  }
  ObjectReply PlanRetention(int64_t cutoff, size_t limit,
                            ObjectRetentionPlan* plan,
                            std::vector<std::string>* selected) {
    std::vector<ObjectRetentionNode> nodes;
    nodes.reserve(records_.size());
    for (const auto& [name, m] : records_) {
      (void)name;
      nodes.push_back({m.owner, m.kind, m.id, m.digest, m.created, m.expires,
                       m.bytes, &m.retention});
    }
    auto status = PlanObjectRetention(nodes, Now(), cutoff, limit, plan);
    if (!status)
      return Error(503, "retention_plan_unavailable", status.message());
    size_t index = 0, selection = 0;
    for (const auto& [name, m] : records_) {
      (void)m;
      if (selection == plan->selected.size()) break;
      if (plan->selected[selection] == index) {
        selected->push_back(name);
        ++selection;
      }
      ++index;
    }
    return {};
  }
  ObjectReply CollectRetention(const std::vector<std::string>& selected) {
    if (selected.empty()) return {};
    auto mutations = Array();
    for (const auto& name : selected) {
      const auto& m = records_.at(name);
      mutations.array.push_back(Object({{"operation", String("erase")},
                                        {"owner", String(m.owner)},
                                        {"kind", String(m.kind)},
                                        {"id", String(m.id)},
                                        {"revision", Number(m.revision)}}));
    }
    return Batch(Object({{"mutations", std::move(mutations)}}));
  }
  ObjectReply Retention(const Json& request) {
    const auto cutoff = request.GetInt("item_created_before", -1);
    const auto limit = request.GetInt("limit", 0);
    const auto expected = request.GetString("expected_plan");
    if (cutoff < 0 || cutoff > Now() || limit < 1 || limit > 128 ||
        (!expected.empty() && !Hex(expected, 64)))
      return Error(400, "invalid_retention_command",
                   "Invalid retention cutoff, limit or plan digest");
    ObjectRetentionPlan plan;
    std::vector<std::string> selected;
    auto planned = PlanRetention(cutoff, limit, &plan, &selected);
    if (!planned.ok()) return planned;
    if (!expected.empty() && expected != plan.sha256)
      return Error(
          409, "retention_plan_changed",
          "Stored objects or retention eligibility changed; inspect again");
    auto candidates = Array();
    for (const auto& name : selected) {
      const auto& m = records_.at(name);
      candidates.array.push_back(Object({{"owner_sha256", String(m.owner)},
                                         {"kind", String(m.kind)},
                                         {"id", String(m.id)},
                                         {"revision", Number(m.revision)},
                                         {"created_at", Number(m.created)},
                                         {"expires_at", Number(m.expires)},
                                         {"bytes", Number(m.bytes)}}));
    }
    if (!expected.empty()) {
      auto collected = CollectRetention(selected);
      if (!collected.ok()) return collected;
    }
    ObjectReply reply;
    reply.value = Object(
        {{"object", String("q4t.state_retention")},
         {"plan_sha256", String(plan.sha256)},
         {"applied", Boolean(!expected.empty())},
         {"item_created_before", Number(cutoff)},
         {"limit", Number(limit)},
         {"eligible_objects", Number(plan.eligible)},
         {"selected_bytes", Number(plan.bytes)},
         {"protected_active", Number(plan.active)},
         {"protected_references", Number(plan.referenced)},
         {"protected_uncertain", Number(plan.uncertain)},
         {"deleted_objects", Number(expected.empty() ? 0 : selected.size())},
         {"has_more", Boolean(plan.eligible > selected.size())},
         {"candidates", std::move(candidates)}});
    return reply;
  }
  ObjectReply Prune() {
    const auto now = Now();
    if (std::none_of(records_.begin(), records_.end(), [&](const auto& row) {
          return row.second.retention.collect_when_unreferenced ||
                 (row.second.expires > 0 && row.second.expires <= now);
        }))
      return {};
    ObjectRetentionPlan plan;
    std::vector<std::string> selected;
    auto planned = PlanRetention(0, 128, &plan, &selected);
    // A bounded maintenance planner cannot deny an otherwise usable read or
    // write. Its failure is visible through explicit operator inspection;
    // retained bytes still count toward quotas, with no false reclamation.
    if (!planned.ok()) return {};
    return CollectRetention(selected);
  }
  ObjectReply Put(const std::string& name, const Json& request) {
    // Conditioned writes use the same final journal check as transactions.
    if (request.GetInt("mutate_before", 0))
      return Batch(Object({{"mutations", Array({request})}}));
    const auto revision = request.GetInt("revision", -1);
    const auto ttl = request.GetInt("ttl");
    const auto* data = request.Find("data");
    if (revision < 0 || revision >= (1LL << 53) - 1 || !data || ttl < 0 ||
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
    int64_t created = 0, expires = 0;
    auto timing = Times(request, now, exists ? found->second.created : 0,
                         options_.max_ttl_seconds, &created, &expires);
    if (!timing.ok()) return timing;
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
               digest, revision + 1, created, expires, text.size(),
               InspectObjectRetention(request.GetString("kind"), *data)});
    if (fsync(root_.Get())) return IoError(true);
    ObjectReply result;
    result.value = Public(record);
    return result;
  }
  ObjectReply ScanResponses(const Json& request) {
    const auto after = request.GetString("after");
    if (!after.empty() && !ValidFilename(after))
      return Error(400, "invalid_object_cursor", "Invalid recovery cursor");
    auto current = records_.upper_bound(after);
    auto rows = Array();
    std::string cursor = after;
    size_t scanned = 0, responses = 0;
    for (; current != records_.end() && scanned < 32 && responses < 8;
         ++current, ++scanned) {
      cursor = current->first;
      const auto& metadata = current->second;
      if (metadata.kind != "response") continue;
      ++responses;
      Json record;
      if (!ReadKnown(current->first, metadata, &record)) return IoError();
      const auto* data = record.Find("data");
      const auto* response = data ? data->Find("response") : nullptr;
      if (!response) continue;
      const auto status = response->GetString("status");
      if (status != "queued" && status != "in_progress") continue;
      const auto* continuation = data->Find("continuation");
      auto scope = continuation ? continuation->GetString("scope") : "";
      if (scope.size() > 256 || Digest(scope) != metadata.owner) scope.clear();
      const auto* owner = continuation ? continuation->Find("owner") : nullptr;
      rows.array.push_back(Object(
          {{"id", String(metadata.id)},
           {"scope", String(scope)},
           {"owner_digest", String(metadata.owner)},
           {"owner", owner ? *owner : Json()},
           {"status", String(status)},
           {"background", Boolean(response->GetBool("background"))},
           {"execution_deadline_at",
            Number(continuation ? continuation->GetInt("execution_deadline_at")
                                : 0)}}));
    }
    ObjectReply result;
    result.value = Object({{"data", std::move(rows)},
                           {"cursor", String(cursor)},
                           {"scanned", Number(scanned)},
                           {"has_more", Boolean(current != records_.end())}});
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
      if (request.GetBool("file_metadata_only")) {
        if (request.GetString("kind") != "file")
          return Error(400, "invalid_object_request", "Invalid file projection");
        const auto* data = record.Find("data");
        const auto* blob = data ? data->Find("blob") : nullptr;
        auto metadata_only = Object();
        if (data) {
          Set(&metadata_only, "format", String(data->GetString("format")));
          if (const auto* file = data->Find("file"))
            Set(&metadata_only, "file", *file);
        }
        Set(&metadata_only, "binary_bytes",
            Number(blob ? blob->GetInt("bytes", -1) : -1));
        Set(&record, "data", std::move(metadata_only));
      }
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
  size_t retention_bytes_ = 0;
  int pause_after_ = 0;
};
}  // namespace

int MaybeRunObjectWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool matched = std::strcmp(argv[1], "--q4t-object-worker") == 0;
  int pause_after = 0;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  const bool lose_ack =
      std::strcmp(argv[1], "--q4t-object-worker-lost-ack") == 0;
  const bool lose_second_ack =
      std::strcmp(argv[1], "--q4t-object-worker-second-put-lost-ack") == 0;
  int successful_puts = 0;
  matched = matched || lose_ack || lose_second_ack;
  if (std::strcmp(argv[1], "--q4t-object-worker-commit-pause") == 0) {
    matched = true;
    pause_after = -1;
  }
  if (std::strcmp(argv[1], "--q4t-object-worker-apply-pause") == 0) {
    matched = true;
    pause_after = 1;
  }
#endif
  if (!matched) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    DiskStore store(pause_after);
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
      if (reply.ok() && request.GetString("operation") == "put") {
        ++successful_puts;
        if (lose_ack || (lose_second_ack && successful_puts == 2))
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
