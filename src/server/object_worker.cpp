#include "q4t/server/object_store.h"
#include "webhook_events.h"
#include "agent_session_internal.h"
#include "agent_environment_bindings.h"
#include "agent_automation_tool.h"
#include "q4t/server/batch_checkpoint.h"
#include "q4t/server/batch_quota.h"
#include "q4t/server/batch_state.h"

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

#include "agent_environment_journal.h"
#include "object_protocol.h"
#include "q4t/server/application_payload.h"
#include "q4t/server/grammar_process.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
Json RecoveryOwner(const Json* owner) {
  if (!owner || !ValidResponseOwner(*owner)) return {};
  return Object({{"pid", Number(owner->GetInt("pid"))},
                 {"start_ticks", Number(owner->GetInt("start_ticks"))},
                 {"boot_id", String(owner->GetString("boot_id"))},
                 {"pid_namespace", Number(owner->GetInt("pid_namespace"))}});
}
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
ObjectReply SnapshotConflict() {
  return Error(409, "input_snapshot_reference_conflict",
               "Private input snapshot is absent or its identity changed");
}
ObjectReply ImmutableSnapshot(const Json& data, const Json* previous) {
  if (!data.IsObject() || data.GetString("format") != "q4t.input_snapshot.v1" ||
      (previous && OutputJson(data) != OutputJson(*previous)))
    return Error(409, "input_snapshot_immutable",
                 "Changed input content requires a new immutable snapshot");
  return {};
}
ObjectReply Times(const Json& request, int64_t now, int64_t existing_created,
                   int64_t max_ttl, int64_t* created, int64_t* expires,
                   int64_t retained_active_expiry = 0) {
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
  // Retention deliberately keeps active work past its retention deadline.
  // Permit its CAS owner to reconcile/finish while preserving that exact
  // expired timestamp. This cannot resurrect a new or ordinary expired item.
  if (*expires && *expires <= now &&
      !(absolute > 0 && absolute == retained_active_expiry))
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
  if (webhook_detail::IsEventSource(result.GetString("kind")))
    std::erase_if(Field(result, "data").object, [](const auto& field) {
      return field.first == "q4t_webhook_deferred";
    });
  return result;
}
struct Metadata {
  std::string owner, kind, id, digest;
  int64_t revision = 0, created = 0, expires = 0;
  uint64_t bytes = 0;
  ObjectRetentionInfo retention;
  BatchQueueCharge batch_charge = {};
  uint32_t webhook_subscriptions = 0;
  uint32_t terminal_event = 0;
  bool agent_started = false;
  bool environment_pending = false;
  bool webhook_deferred = false;
  int64_t environment_wake = 0, agent_finished = 0;
  int64_t environment_sequence = 0;
  std::string last_response{};
  bool waiting_tool = false;
  int64_t cancelled_at = 0;
};
void IndexWebhookState(Metadata* metadata, const Json& data) {
  if (metadata->kind == "webhook_endpoint") {
    metadata->webhook_subscriptions = webhook_detail::Subscriptions(data);
  } else if (webhook_detail::IsEventSource(metadata->kind)) {
    metadata->agent_started = metadata->kind == "response" &&
                              data.GetInt("agent_turn_started_at") > 0;
    metadata->terminal_event = webhook_detail::EventBit(
        webhook_detail::Transition(metadata->kind, data, false));
    metadata->webhook_deferred = data.Has("q4t_webhook_deferred");
    if (metadata->kind == "environment") {
      const auto* resource = data.Find("resource");
      if (!resource || resource->GetString("type") != "openai_hosted") return;
      metadata->environment_pending = resource->GetString("status") == "pending";
      metadata->environment_sequence = data.GetInt("environment_sequence");
      const auto status = resource->GetString("status");
      if (status == "ready") {
        metadata->environment_wake = data.GetInt("warm_until");
      } else if (data.GetArray("attached_sessions")) {
        const auto last = data.GetInt("last_activity_at");
        if ((status == "connected" || status == "disconnected") && last > 0)
          metadata->environment_wake =
              last + environment_detail::kEnvironmentIdleSeconds;
        if (status == "suspended")
          metadata->environment_wake = data.GetInt("checkpoint_until");
      }
    } else if (metadata->kind == "agent_session") {
      const auto* turns = data.GetArray("turns");
      if (turns && !turns->array.empty()) {
        const auto& last = turns->array.back();
        metadata->last_response = last.GetString("response_id");
        metadata->cancelled_at = last.GetInt("cancel_requested_at");
      }
    } else if (metadata->kind == "response" &&
               !data.GetString("agent_session_id").empty()) {
      const auto* response = data.Find("response");
      const auto* output = response ? response->GetArray("output") : nullptr;
      metadata->agent_finished = data.GetInt("agent_turn_finished_at");
      metadata->waiting_tool =
          output && response->GetString("status") == "completed" &&
          std::ranges::any_of(output->array, [](const Json& item) {
            return item.GetString("type") == "function_call" &&
                   item.GetString("status") == "completed" &&
                   !automation_detail::IsToolItem(item);
          });
    }
  }
}
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
    options_.batch_model_queue_tokens =
        config.GetInt("batch_model_queue_tokens");
    options_.batch_client_model_queue_tokens =
        config.GetInt("batch_client_model_queue_tokens");
    if (options_.max_object_bytes < 1024 ||
        options_.max_object_bytes > kMaxFrame / 2 ||
        options_.max_client_bytes < options_.max_object_bytes ||
        options_.max_total_bytes < options_.max_client_bytes ||
        options_.max_total_bytes > (1ULL << 40) ||
        !options_.max_client_objects ||
        options_.max_total_objects < options_.max_client_objects ||
        options_.max_total_objects > 100000 || options_.max_ttl_seconds <= 0 ||
        options_.max_ttl_seconds > 366 * 24 * 3600 ||
        !options_.batch_client_model_queue_tokens ||
        options_.batch_client_model_queue_tokens >
            options_.batch_model_queue_tokens ||
        options_.batch_model_queue_tokens >= (1ULL << 53) || !OpenRoot())
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

  // Consult bounded in-memory indices. Reading 128 full session histories
  // from storage at one deadline would delay unrelated client requests.
  int64_t IdleDeadline(const std::string& owner, const Json& data,
                       int64_t now) {
    const auto* members = data.GetArray("attached_sessions");
    if (!members ||
        members->array.size() > environment_detail::kMaxAttachedSessions)
      return now + 30;
    auto last = data.GetInt("last_activity_at");
    for (const auto& id : members->array) {
      const auto s =
          records_.find(owner + ".agent_session." + id.str + ".json");
      if (s == records_.end()) return now + 30;
      if (s->second.last_response.empty()) continue;
      const auto r = records_.find(owner + ".response." +
                                   s->second.last_response + ".json");
      if (r == records_.end() || !r->second.terminal_event ||
          (r->second.waiting_tool && !s->second.cancelled_at))
        return now + 30;
      last = std::max({last, r->second.agent_finished, s->second.cancelled_at});
    }
    return last + environment_detail::kEnvironmentIdleSeconds;
  }

  int WaitForRequest() {
    if (environment_timers_.empty()) return 1;
    const auto now = Now();
    const auto [due, name] = *environment_timers_.begin();
    if (due > now) {
      // Re-evaluate wall time periodically if the system clock is adjusted.
      return detail::WaitWorkerInput(
          static_cast<int>(std::min<int64_t>(60, due - now) * 1000));
    }
    const auto it = records_.find(name);
    if (it == records_.end()) return -1;
    const auto reschedule = [&](int64_t when) {
      environment_timers_.erase({due, name});
      it->second.environment_wake = when;
      environment_timers_.emplace(when, name);
    };
    Json stored;
    if (!ReadKnown(name, it->second, &stored)) {
      ++environment_expiry_retries_;
      reschedule(now + 30);
      return detail::WaitWorkerInput(0);
    }
    auto& data = Field(stored, "data");
    auto& resource = Field(data, "resource");
    const auto status = resource.GetString("status");
    auto expires = it->second.expires;
    if (status == "connected" || status == "disconnected") {
      const auto idle = IdleDeadline(it->second.owner, data, now);
      if (idle > now) {
        reschedule(idle);
        return detail::WaitWorkerInput(0);
      }
      // The workspace is already durable and its encrypted definition is
      // authenticated under its owner and environment ID on resume.
      Set(&resource, "status", String("suspended"));
      environment_detail::AppendEnvironmentStatus(&data, "suspended");
      Set(&data, "suspended_at", Number(idle));
      Set(&data, "checkpoint_until",
          Number(idle + environment_detail::kCheckpointSeconds));
    } else if (status == "suspended") {
      // Rebuild the public receipt with no checkpoint content or private
      // snapshot references. Normal retention can reclaim unreferenced chunks.
      auto receipt = Object({{"id", String(it->second.id)},
                             {"object", String("agent.environment")},
                             {"type", String("openai_hosted")},
                             {"status", String("expired")},
                             {"files", Array()},
                             {"skills", Array()},
                             {"plugins", Array()}});
      const auto* members = data.GetArray("attached_sessions");
      if (members && members->array.empty())
        expires = now + std::min<int64_t>(86400, options_.max_ttl_seconds);
      environment_detail::AppendEnvironmentStatus(&data, "expired");
      data = Object({{"environment_sequence",
                      Number(data.GetInt("environment_sequence"))},
                     {"environment_events", Field(data, "environment_events")},
                     {"resource", std::move(receipt)},
                     {"warm_until", Number(data.GetInt("warm_until"))},
                     {"expired_at", Number(data.GetInt("checkpoint_until"))},
                     {"vault_ids", Array()},
                     {"attached_sessions", Field(data, "attached_sessions")}});
    } else {
      Set(&resource, "status", String("expired"));
    }
    auto mutation = Object({{"operation", String("put")},
                            {"owner", String(it->second.owner)},
                            {"id", String(it->second.id)},
                            {"kind", String("environment")},
                            {"revision", Number(it->second.revision)},
                            {"ttl", Number(0)},
                            {"expires_at", Number(expires)},
                            {"data", std::move(data)}});
    auto result =
        Batch(Object({{"mutations", Array({std::move(mutation)})}}), true);
    if (!result.ok()) {
      if (result.commit_unknown) return -1;
      // A source at its absolute byte limit can still report logical expiry
      // through GET. Let other tenants proceed and retry this commit later.
      ++environment_expiry_retries_;
      reschedule(now + 30);
    }
    // One expiry per iteration keeps client requests responsive under backlog.
    return detail::WaitWorkerInput(0);
  }

  ObjectReply Run(const Json& request) {
    if (!initialized_) return IoError();
    const auto operation = request.GetString("operation");
    if (operation == "retention") return Retention(request);
    if (operation == "webhook_reconcile") return ReconcileWebhooks(request);
    if (operation == "next_environment") {
      ObjectReply result;
      result.value = Object();
      auto owner = pending_environments_.upper_bound(request.GetString("after"));
      if (owner == pending_environments_.end()) owner = pending_environments_.begin();
      if (owner != pending_environments_.end()) {
        auto& group = owner->second;
        auto name = group.names.upper_bound(group.previous);
        if (name == group.names.end()) name = group.names.begin();
        group.previous = *name;
        result.value = Object({{"after", String(owner->first)},
                               {"owner", String(owner->first)},
                               {"id", String(records_.at(*name).id)}});
      }
      return result;
    }
    if (operation == "scan_responses") {
      auto pruned = Prune();
      return pruned.ok() ? ScanResponses(request) : pruned;
    }
    if (operation == "scan_batches") {
      auto pruned = Prune();
      return pruned.ok() ? ScanBatches(request) : pruned;
    }
    if (operation == "scan_vector_files") {
      auto pruned = Prune();
      return pruned.ok() ? ScanVectorFiles(request) : pruned;
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
    if (operation == "environment_events") {
      if (kind != "environment")
        return Error(400, "invalid_object_request", "Invalid event source");
      const auto* filter = request.Find("data");
      const auto since = filter ? filter->GetInt("after_sequence", -1) : -1;
      const auto latest = found->second.environment_sequence;
      if (since > latest || since < -1)
        return Error(409, "environment_event_cursor_invalid",
                     "Invalid environment event boundary");
      ObjectReply result;
      result.value =
          Object({{"sequence", Number(latest)}, {"events", Array()},
                  {"preparing", Boolean(found->second.environment_pending)}});
      // The usual live poll answers entirely from the owner-scoped index.
      if (since == -1 || since == latest) return result;
      Json stored;
      if (!ReadKnown(name, found->second, &stored)) return IoError();
      const auto* rows = Field(stored, "data").GetArray("environment_events");
      if (!rows || rows->array.empty() ||
          rows->array.size() > environment_detail::kEnvironmentEventLimit)
        return IoError();
      if (since + 1 < rows->array.front().GetInt("sequence"))
        return Error(409, "environment_event_cursor_expired",
                     "Reconnect to the current environment state");
      auto& events = Field(result.value, "events").array;
      int64_t next = since + 1;
      for (const auto& event : rows->array) {
        const auto n = event.GetInt("sequence");
        if (n <= since) continue;
        if (n != next++) return IoError();
        events.push_back(event);
      }
      if (next - 1 != latest) return IoError();
      return result;
    }
    Json record;
    if (!ReadKnown(name, found->second, &record)) return IoError();
    ObjectReply result;
    if (operation == "get") {
      result.value = Public(record);
    } else if (operation == "erase") {
      if (kind == "input_snapshot")
        return Error(409, "input_snapshot_immutable",
                     "Private snapshots are reclaimed by reference retention");
      const bool project_erase = kind == "batch" || found->second.webhook_deferred;
      if (project_erase || request.GetInt("mutate_before", 0)) {
        auto reply = Batch(Object({{"mutations", Array({request})}}));
        if (project_erase && reply.ok()) {
          const auto* rows = reply.value.GetArray("data");
          if (!rows || rows->array.size() != 1) return IoError(true);
          auto deleted = rows->array[0];
          reply.value = std::move(deleted);
        }
        return reply;
      }
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
      if (record->IsNull()) {
        if (exists && (!MatchesErase(name, old) ||
                       unlinkat(root_.Get(), name.c_str(), 0)))
          return false;
      } else {
        if (exists && !Load(name, &current, &metadata)) return false;
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
  using SnapshotChanges = std::map<std::string, const Json*>;
  using SnapshotIdentities =
      std::map<std::string, std::pair<std::string, std::string>>;
  ObjectReply PrivateSnapshotReferences(const Json& record,
                                        const SnapshotChanges& changes,
                                        SnapshotIdentities* checked) {
    const auto* data = record.Find("data");
    if (!data || !data->IsObject()) return {};
    const auto kind = record.GetString("kind");
    const bool page = kind == "tool_run" && IsBatchPage(*data);
    if (!SupportsApplicationPayload(kind) && kind != "input_snapshot" && !page)
      return {};
    struct Dependency {
      InputSnapshotRef reference;
      std::string binding;
    };
    std::vector<Dependency> dependencies;
    const auto collect = [&](const Json& source) -> bool {
      const auto* inputs = source.Find("input_snapshots");
      if (!inputs || inputs->IsNull()) return true;
      if (!inputs->IsArray() || inputs->array.size() > 4096)
        return false;
      for (const auto& input : inputs->array) {
        const auto* snapshot = input.Find("snapshot");
        Dependency dependency;
        if (!snapshot ||
            !ParseInputSnapshotReference(*snapshot, &dependency.reference))
          return false;
        if (const auto* binding = input.Find("binding_sha256")) {
          if (!binding->IsString() || !Hex(binding->str, 64))
            return false;
          dependency.binding = binding->str;
        }
        dependencies.push_back(std::move(dependency));
        if (dependencies.size() > kMaxTransactionObjects) return false;
      }
      return true;
    };
    if (!collect(*data)) return SnapshotConflict();
    if (page) {
      const auto* rows = data->GetArray("rows");
      if (!rows || rows->array.size() > 128) return SnapshotConflict();
      for (const auto& row : rows->array) {
        if (const auto* input = row.Find("prepared_inline");
            input &&
            input->GetString("scope_sha256") != record.GetString("owner"))
          return SnapshotConflict();
        if (!collect(row)) return SnapshotConflict();
      }
    }
    std::vector<ApplicationStringRef> strings;
    if (!ParseApplicationPayloadReferences(*data, &strings))
      return SnapshotConflict();
    for (const auto& string : strings)
      dependencies.push_back(
          {string.snapshot,
           Digest("q4t.application_string.v1:" + string.sha256)});
    const auto owner = record.GetString("owner");
    for (const auto& dependency : dependencies) {
      const auto& reference = dependency.reference;
      const auto name = owner + ".input_snapshot." + reference.id + ".json";
      auto found = checked->find(name);
      if (found == checked->end()) {
        if (checked->size() >= kMaxTransactionObjects)
          return Error(413, "object_dependency_capacity",
                       "Transaction snapshot dependencies exceed capacity");
        Json loaded;
        const Json* root = nullptr;
        if (const auto update = changes.find(name); update != changes.end()) {
          root = update->second;
        } else {
          const auto stored = records_.find(name);
          if (stored == records_.end()) return SnapshotConflict();
          if (!ReadKnown(name, stored->second, &loaded)) return IoError();
          root = &loaded;
        }
        const auto* root_data = root->Find("data");
        if (!root->IsObject() || root->GetString("owner") != owner ||
            root->GetString("kind") != "input_snapshot" ||
            root->GetString("id") != reference.id ||
            root->GetInt("expires_at", -1) != 0 || !root_data ||
            root_data->GetString("format") != "q4t.input_snapshot.v1")
          return SnapshotConflict();
        found = checked
                    ->emplace(name,
                              std::pair{Digest(OutputJson(*root_data)),
                                        root_data->GetString("binding_sha256")})
                    .first;
      }
      if (found->second.first != reference.sha256 ||
          (!dependency.binding.empty() &&
           found->second.second != dependency.binding))
        return SnapshotConflict();
    }
    return {};
  }
  ObjectReply BatchBudgets(const Json& changes, const std::string& owner) {
    struct Delta {
      uint64_t tokens = 0, rows = 0;
      uint64_t recounted = 0, recounted_line = 0;
      std::set<std::string> pages;
    };
    std::map<std::string, Delta> increments;
    std::map<std::string, std::pair<Json, const Json*>> roots;
    std::vector<BatchQueueChange> charges;
    const auto invalid = [] {
      return Error(409, "batch_budget_conflict",
                   "Batch input budget and checkpoint must commit together");
    };
    for (const auto& change : changes.array) {
      const auto name = change.GetString("name");
      const auto* next = change.Find("record");
      const auto found = records_.find(name);
      const auto kind =
          next->IsNull() ? found->second.kind : next->GetString("kind");
      if (kind != "batch" && kind != "tool_run") continue;
      const auto* data = next->Find("data");
      if (kind == "tool_run" && (!data || !IsBatchPage(*data))) continue;
      Json previous;
      if (found != records_.end() && !ReadKnown(name, found->second, &previous))
        return IoError();
      const auto* old = previous.Find("data");
      if (kind == "batch") {
        const auto before = found == records_.end()
                                ? BatchQueueCharge{}
                                : found->second.batch_charge;
        if (!data) {
          if (before.tokens || before.unknown)
            return Error(409, "batch_active_delete_conflict",
                         "Active Batch must drain before removal");
          charges.push_back({before, {}});
          continue;
        }
        auto checked = CheckBatchBudgetMutation(*data, old);
        if (!checked.ok()) return checked;
        const auto* progress = data->Find("input_recount");
        const auto* old_progress = old ? old->Find("input_recount") : nullptr;
        const bool recount =
            progress && (!old_progress ||
                         OutputJson(*progress) != OutputJson(*old_progress));
        charges.push_back(
            {before, InspectBatchQueueCharge(kind, *data), recount});
        if (data->Has("enqueued_input_tokens") || progress) {
          // Copy only the small prior root, not request/result pages. Check
          // its counter delta against precisely the newly committed rows.
          roots.emplace(next->GetString("id"),
                        std::make_pair(old ? *old : Json{}, data));
        }
      } else if (data && IsBatchPage(*data)) {
        const auto* rows = data->GetArray("rows");
        const auto* old_rows = old ? old->GetArray("rows") : nullptr;
        const auto previous_count = old_rows ? old_rows->array.size() : 0;
        for (size_t i = 0; i < rows->array.size(); ++i) {
          const auto* value = rows->array[i].Find("input_budget");
          if (!value ||
              (i < previous_count && old_rows->array[i].Has("input_budget")))
            continue;
          BatchInputBudget budget;
          if (!ParseBatchInputBudget(*value, &budget)) return invalid();
          auto& delta = increments[data->GetString("batch_id")];
          if (budget.tokens > kBatchTokenLimit - delta.tokens) return invalid();
          delta.tokens += budget.tokens;
          ++delta.rows;
          if (i < previous_count) {
            ++delta.recounted;
            delta.recounted_line = rows->array[i].GetInt("line");
          }
          delta.pages.insert(next->GetString("id"));
        }
      }
    }
    for (const auto& [id, delta] : increments) {
      const auto root = roots.find(id);
      if (root == roots.end()) return invalid();
      const auto* pages = root->second.second->GetArray("pages");
      if (!pages) return invalid();
      for (const auto& page : delta.pages)
        if (std::none_of(pages->array.begin(), pages->array.end(),
                         [&](const Json& value) {
                           return value.IsString() && value.str == page;
                         }))
          return invalid();
      if (delta.recounted) {
        BatchInputRecount recount;
        const auto* value = root->second.second->Find("input_recount");
        if (delta.recounted != 1 || delta.rows != 1 || !value ||
            !ParseBatchInputRecount(*value, &recount) ||
            recount.rows != delta.recounted_line)
          return invalid();
      }
    }
    for (const auto& [id, pair] : roots) {
      const auto& [old, data] = pair;
      const auto delta = increments.find(id);
      const auto tokens = delta == increments.end() ? 0 : delta->second.tokens;
      const auto rows = delta == increments.end() ? 0 : delta->second.rows;
      const auto progress = [](const Json& value) {
        BatchInputRecount result;
        if (value.Has("enqueued_input_tokens")) {
          result.tokens = value.GetInt("enqueued_input_tokens");
          result.rows = value.GetInt("metered_rows");
        } else if (const auto* partial = value.Find("input_recount")) {
          ParseBatchInputRecount(*partial, &result);
        }
        return result;
      };
      const auto before = progress(old), after = progress(*data);
      const auto* prior = old.Find("input_recount");
      const auto* current = data->Find("input_recount");
      const bool recount =
          current && (!prior || OutputJson(*current) != OutputJson(*prior));
      if (after.tokens < before.tokens || after.rows < before.rows ||
          after.tokens - before.tokens != tokens ||
          after.rows - before.rows != rows ||
          (recount &&
           (delta == increments.end() || delta->second.recounted != 1)) ||
          (!recount && delta != increments.end() && delta->second.recounted))
        return invalid();
    }
    return batch_quota_.Check(owner, charges, options_.batch_model_queue_tokens,
                              options_.batch_client_model_queue_tokens);
  }
  using Notices = std::map<std::string, Json>;
  static bool NoCapacity(const ObjectReply& result) {
    return !result.commit_unknown && (result.code == "object_quota_exceeded" ||
                                      result.code == "object_too_large");
  }
  // Earlier releases stored a single deferred event directly in the source.
  // Preserve its exact identity and recipient snapshot across upgrades.
  static Json DeferredEvents(const Json& data) {
    const auto* saved = data.Find("q4t_webhook_deferred");
    if (!saved) return Array();
    return saved->IsArray() ? *saved : Array({*saved});
  }

  // All recipients come from the owner's committed subscription index.
  Json Notice(const std::string& owner, const std::string& identity,
              std::string_view type, Json payload, int64_t occurred = 0) {
    const auto bit = webhook_detail::EventBit(type);
    if (!bit || payload.IsNull()) return {};
    const auto now = Now();
    const auto created = occurred > 0 ? occurred : now;
    const auto ttl = std::min<int64_t>(72 * 3600, options_.max_ttl_seconds);
    if (created > now || created <= now - ttl) return {};
    const auto prefix = owner + ".webhook_endpoint.";
    auto targets = Array();
    for (auto it = records_.lower_bound(prefix);
         it != records_.end() && it->first.starts_with(prefix); ++it)
      if ((it->second.webhook_subscriptions & bit) &&
          (!occurred || it->second.created <= occurred))
        targets.array.push_back(String(it->second.id));
    if (targets.array.empty()) return {};
    return webhook_detail::PendingEvent(
        owner, identity, type, std::move(payload), targets, created, ttl);
  }

  bool AgentPhaseChanged(const Json& data, const Metadata* prior) const {
    if (data.GetString("agent_session_id").empty() ||
        (prior && prior->terminal_event))
      return false;
    const auto* response = data.Find("response");
    if (!response) return false;
    if (response->GetString("status") == "in_progress")
      return !prior || !prior->agent_started;
    return !webhook_detail::Transition("response", data, false).empty();
  }

  bool Subscribed(const std::string& owner, uint32_t mask) const {
    const auto prefix = owner + ".webhook_endpoint.";
    for (auto it = records_.lower_bound(prefix);
         it != records_.end() && it->first.starts_with(prefix); ++it)
      if (it->second.webhook_subscriptions & mask) return true;
    return false;
  }

  Json SessionPhase(const std::string& owner, const std::string& response,
                    const Json& data, const Json& session, const Json& link,
                    bool cancelled_wait = false) {
    auto settings = agent_detail::SessionSettings(session, &link);
    AgentSessionView view;
    if (!settings.ok() ||
        !agent_detail::ProjectStep(settings.value, link, data, &view).ok() ||
        (cancelled_wait && view.turn.GetString("status") != "cancelled"))
      return {};
    auto status = view.session.GetString("status");
    const auto type =
        "agent.session." +
        (status == "requires_action" ? "action_required" : status);
    return Notice(owner, response, type,
                  webhook_detail::EventPayload(
                      type, data.GetString("agent_session_id"),
                      Object({{"session", std::move(view.session)}})));
  }

  // Use the same session/turn projection as public GET and SSE. Most writes
  // are incremental model output and never enter this path.
  Json AgentTurnNotice(const std::string& owner, const std::string& response,
                       const Json& data, const Json& writes) {
    const auto session_id = data.GetString("agent_session_id");
    if (!ValidId(session_id)) return {};
    static const auto phases =
        webhook_detail::EventBit("agent.session.idle") |
        webhook_detail::EventBit("agent.session.in_progress") |
        webhook_detail::EventBit("agent.session.failed") |
        webhook_detail::EventBit("agent.session.action_required");
    if (!Subscribed(owner, phases)) return {};
    const Json* session = nullptr;
    for (const auto& row : writes.array) {
      if (row.GetString("kind") == "agent_session" &&
          row.GetString("id") == session_id &&
          row.GetString("owner") == owner) {
        if (row.GetString("operation") != "put") return {};
        session = row.Find("data");
        break;
      }
    }
    Json stored;
    if (!session) {
      auto name = owner + ".agent_session." + session_id + ".json";
      auto it = records_.find(name);
      if (it == records_.end() || !ReadKnown(name, it->second, &stored))
        return {};
      session = stored.Find("data");
    }
    const auto* turns = session ? session->GetArray("turns") : nullptr;
    if (!turns || turns->array.empty()) return {};
    const auto& link = turns->array.back();
    if (link.GetString("response_id") != response) return {};
    return SessionPhase(owner, response, data, *session, link);
  }

  static const Json* CancelledLink(const Json& session) {
    const auto* turns = session.GetArray("turns");
    if (!turns || turns->array.empty()) return nullptr;
    const auto& link = turns->array.back();
    return link.GetInt("cancel_requested_at") > 0 ? &link : nullptr;
  }

  // A waiting tool call is already a completed native Response. Cancelling
  // it changes session state in the session CAS without another model write.
  Json CancelNotice(const std::string& name, const Metadata& prior,
                    const Json& session) {
    const auto* link = CancelledLink(session);
    if (!link || !Subscribed(prior.owner,
                             webhook_detail::EventBit("agent.session.idle")))
      return {};
    const auto response_id = link->GetString("response_id");
    if (!ValidId(response_id)) return {};
    Json before;
    if (!ReadKnown(name, prior, &before)) return {};
    const auto* old = CancelledLink(Field(before, "data"));
    if (old && old->GetString("response_id") == response_id) return {};
    const auto key = prior.owner + ".response." + response_id + ".json";
    const auto it = records_.find(key);
    Json stored;
    if (it == records_.end() || !it->second.terminal_event ||
        !ReadKnown(key, it->second, &stored))
      return {};
    const auto& data = Field(stored, "data");
    const auto* response = data.Find("response");
    if (!response || response->GetString("status") != "completed" ||
        data.GetString("agent_session_id") != prior.id)
      return {};
    return SessionPhase(prior.owner, response_id, data, session, *link, true);
  }

  Json Notifications(const Json& write, const std::string& name,
                     const Json& writes, size_t* added) {
    auto events = Array();
    const auto kind = write.GetString("kind");
    const auto prior = records_.find(name);
    const bool creating = prior == records_.end();
    if (!creating && prior->second.webhook_deferred) {
      Json record;
      if (!ReadKnown(name, prior->second, &record)) return events;
      events = DeferredEvents(Field(record, "data"));
    }
    const auto* data = write.Find("data");
    if (!data || write.GetString("operation") != "put") return events;
    const auto owner = write.GetString("owner"), id = write.GetString("id");
    auto append = [&](Json event) {
      if (event.IsNull()) return;
      events.array.push_back(std::move(event));
      ++*added;
    };
    const auto type = webhook_detail::Transition(kind, *data, creating);
    const bool transitioned =
        creating || (kind == "environment" ? prior->second.terminal_event !=
                                                 webhook_detail::EventBit(type)
                                           : !prior->second.terminal_event);
    if (!type.empty() && transitioned) {
      const bool suspended = type == "agent.environment.suspended";
      const auto occurred =
          suspended ? data->GetInt("suspended_at")
          : type == "agent.environment.ready" ? data->GetInt("prepared_at")
          : type == "agent.environment.failed" ? data->GetInt("failed_at")
          : type == "agent.environment.expired"
              ? data->GetInt("expired_at", data->GetInt("warm_until"))
              : 0;
      const auto identity =
          suspended ? id + "/" + std::to_string(write.GetInt("revision")) : id;
      append(Notice(owner, identity, type,
                    webhook_detail::EventPayload(type, id, *data), occurred));
      if (type == "agent.session.created" && data->GetBool("created_idle"))
        append(Notice(
            owner, id, "agent.session.idle",
            webhook_detail::EventPayload("agent.session.idle", id, *data)));
    }
    if (kind == "response" &&
        AgentPhaseChanged(*data, creating ? nullptr : &prior->second)) {
      append(AgentTurnNotice(owner, id, *data, writes));
    } else if (kind == "agent_session" && !creating) {
      append(CancelNotice(name, prior->second, *data));
    }
    return events;
  }

  // An external subscriber never participates in a business commit. Only its
  // durable notice is included. Preserve the original number of public rows.
  ObjectReply Batch(const Json& request, bool retention = false) {
    const auto* writes = request.GetArray("mutations");
    if (!writes || writes->array.size() > kMaxTransactionObjects)
      return CommitBatch(request, retention);
    Notices notices;
    size_t added = 0;
    for (const auto& write : writes->array) {
      const auto kind = write.GetString("kind");
      if (!webhook_detail::IsEventSource(kind)) continue;
      const auto name = Filename(write);
      auto events = Notifications(write, name, *writes, &added);
      if (!events.array.empty()) notices.emplace(name, std::move(events));
    }
    if (notices.empty()) return CommitBatch(request, retention);
    auto extended = request;
    auto& mutations = Field(extended, "mutations").array;
    const auto now = Now();
    for (const auto& [name, events] : notices)
      for (const auto& event : events.array)
        if (event.GetInt("expires_at") > now) mutations.push_back(event);
    if (mutations.size() <= kMaxTransactionObjects) {
      auto result = CommitBatch(extended, retention, &notices, true);
      if (result.ok()) {
        auto& outputs = Field(result.value, "data").array;
        if (outputs.size() != mutations.size()) return IoError(true);
        outputs.resize(writes->array.size());
        return result;
      }
      if (!NoCapacity(result)) return result;
    }
    // A full object-count quota can still hold the pending notice in the
    // terminal resource's existing slot. The reconciler moves it atomically.
    auto staged = CommitBatch(request, retention, &notices, false);
    if (staged.ok()) {
      webhook_capacity_deferrals_ += added;
      for (const auto& [name, events] : notices)
        if (!records_.contains(name))
          webhook_capacity_losses_ += events.array.size();
      return staged;
    }
    if (!NoCapacity(staged)) return staged;
    // Even the source plus its small receipt cannot fit. Never block a healthy
    // model response on notification capacity; expose the actual loss.
    auto plain = CommitBatch(request, retention);
    if (plain.ok()) {
      webhook_capacity_losses_ += added;
      for (const auto& [name, events] : notices)
        if (!records_.contains(name))
          webhook_capacity_losses_ += events.array.size();
    }
    return plain;
  }

  ObjectReply ReconcileWebhooks(const Json& request) {
    const auto after = request.GetString("after");
    if (!after.empty() && !ValidFilename(after))
      return Error(400, "invalid_object_cursor", "Invalid notification cursor");
    auto it = records_.upper_bound(after);
    std::string cursor;
    for (size_t n = 0; it != records_.end() && n < 64; ++n) {
      const auto current = it++;
      cursor = current->first;
      const auto& m = current->second;
      if (!m.webhook_deferred) continue;
      // Do not change a resource's retention while moving the notice.
      if (m.expires && m.expires <= Now()) continue;
      Json record;
      if (!ReadKnown(cursor, m, &record)) return IoError();
      auto write = Object({{"operation", String("put")}, {"owner", String(m.owner)},
          {"kind", String(m.kind)}, {"id", String(m.id)}, {"revision", Number(m.revision)},
          {"ttl", Number(0)}, {"expires_at", Number(m.expires)}, {"data", Field(record, "data")}});
      auto& data = Field(write, "data");
      auto pending = DeferredEvents(data);
      const auto now = Now();
      std::erase_if(pending.array, [&](const Json& notice) {
        return notice.GetInt("expires_at") <= now;
      });
      auto mutations = Array({std::move(write)});
      // A single free object slot suffices to make durable progress.
      if (!pending.array.empty()) {
        mutations.array.push_back(std::move(pending.array.front()));
        pending.array.erase(pending.array.begin());
      }
      Notices remaining{{cursor, std::move(pending)}};
      auto moved = CommitBatch(Object({{"mutations", std::move(mutations)}}),
                               false, &remaining);
      if (!moved.ok() && !NoCapacity(moved)) return moved;
      break;
    }
    ObjectReply reply;
    reply.value = Object(
        {{"after", String(it == records_.end() ? "" : cursor)},
         {"capacity_deferrals", Number(webhook_capacity_deferrals_)},
         {"capacity_losses", Number(webhook_capacity_losses_)},
         {"environment_expiry_retries", Number(environment_expiry_retries_)}});
    return reply;
  }

  ObjectReply CommitBatch(const Json& request, bool retention,
                          const Notices* notices = nullptr,
                          bool clear = false) {
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
      if (kind == "input_snapshot" && operation == "erase" && !retention)
        return Error(409, "input_snapshot_immutable",
                     "Private snapshots are reclaimed by reference retention");
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
      if (exists) {
        const bool verified = operation == "erase"
                                  ? MatchesErase(name, found->second.digest)
                                  : ReadKnown(name, found->second, &previous);
        if (!verified) return IoError();
      }
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
        if (kind == "input_snapshot") {
          auto immutable = ImmutableSnapshot(
              *data, exists ? previous.Find("data") : nullptr);
          if (!immutable.ok()) return immutable;
        }
        if (kind == "tool_run") {
          auto immutable = CheckBatchPageMutation(
              *data, exists ? previous.Find("data") : nullptr);
          if (!immutable.ok()) return immutable;
        }
        int64_t created = 0, expires = 0;
        auto timing = Times(mutation, now, exists ? found->second.created : 0,
                            options_.max_ttl_seconds, &created, &expires,
                            exists && (found->second.retention.active ||
                                       (retention && kind == "environment" &&
                                        found->second.environment_wake > 0))
                                ? found->second.expires
                                : 0);
        if (!timing.ok()) return timing;
        if (kind == "input_snapshot" && expires != 0)
          return Error(
              409, "input_snapshot_immutable",
              "Private snapshot retention is determined by references");
        record = Object({{"format", Number(1)},
                         {"owner", String(owner)},
                         {"kind", String(kind)},
                         {"id", String(id)},
                         {"revision", Number(revision + 1)},
                         {"created_at", Number(created)},
                         {"expires_at", Number(expires)},
                         {"data", *data}});
        if (webhook_detail::IsEventSource(kind)) {
          auto& data = Field(record, "data");
          std::erase_if(data.object, [](const auto& f) {
            return f.first == "q4t_webhook_deferred";
          });
          if (notices && notices->contains(name)) {
            const auto& pending = notices->at(name).array;
            if (!clear && !pending.empty())
              Set(&data, "q4t_webhook_deferred",
                  pending.size() == 1 ? pending.front() : notices->at(name));
          } else if (const auto* old = previous.Find("data");
                     old && old->Has("q4t_webhook_deferred")) {
            Set(&data, "q4t_webhook_deferred", *old->Find("q4t_webhook_deferred"));
          }
        }
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
    // Read the final transaction view, independent of mutation order. The
    // same exclusive IO worker makes this check and publishes the decision;
    // no pruning or other application write can run between them.
    SnapshotChanges final;
    for (const auto& change : changes.array)
      final.emplace(change.GetString("name"), change.Find("record"));
    SnapshotIdentities checked;
    for (const auto& change : changes.array) {
      auto references =
          PrivateSnapshotReferences(*change.Find("record"), final, &checked);
      if (!references.ok()) return references;
    }
    auto budget = BatchBudgets(changes, owner);
    if (!budget.ok()) return budget;
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
                                        *content->Find("data")),
                 InspectBatchQueueCharge(content->GetString("kind"),
                                         *content->Find("data"))};
    IndexWebhookState(metadata, *content->Find("data"));
    *record = *content;
    return true;
  }
  bool ReadKnown(const std::string& name, const Metadata& expected,
                 Json* record) {
    Metadata actual;
    return Load(name, record, &actual) && actual.digest == expected.digest &&
           actual.bytes == expected.bytes;
  }
  bool MatchesErase(const std::string& name, const std::string& digest) {
    Fd file(openat(root_.Get(), name.c_str(),
                   O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    std::string text;
    if (!file || !ReadAll(file.Get(), kMaxFrame / 2, &text)) return false;
    // An indexed digest already binds this filename to a validated record.
    // Hash the exact serialized record directly while reclaiming binary
    // chunks. Parsing and reserializing every payload twice can exhaust the
    // worker deadline before the unrelated request that triggered collection.
    const auto known = records_.find(name);
    if (known != records_.end() && known->second.bytes != text.size())
      return false;
    constexpr std::string_view prefix = "{\"record\":";
    const std::string suffix = ",\"sha256\":\"" + digest + "\"}";
    std::string_view record(text);
    if (known != records_.end() && known->second.digest == digest &&
        record.starts_with(prefix) && record.ends_with(suffix)) {
      record.remove_prefix(prefix.size());
      record.remove_suffix(suffix.size());
      if (Digest(record) == digest) return true;
    }
    // Cold journal replay has no trusted index. Also accept older, valid
    // encodings whose whitespace or field order differs from our writer.
    Json value;
    Metadata metadata;
    return DecodeRecord(name, text, &value, &metadata) &&
           metadata.digest == digest;
  }
  void Add(const std::string& name, Metadata metadata) {
    batch_quota_.Add(metadata.owner, metadata.batch_charge);
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
    if (metadata.environment_pending)
      pending_environments_[metadata.owner].names.insert(name);
    if (metadata.environment_wake > 0)
      environment_timers_.emplace(metadata.environment_wake, name);
    records_.emplace(name, std::move(metadata));
  }
  using Iterator = std::map<std::string, Metadata>::iterator;
  void Remove(Iterator found) {
    if (found->second.environment_pending) {
      auto group = pending_environments_.find(found->second.owner);
      group->second.names.erase(found->first);
      if (group->second.names.empty()) pending_environments_.erase(group);
    }
    if (found->second.environment_wake > 0)
      environment_timers_.erase({found->second.environment_wake, found->first});
    batch_quota_.Remove(found->second.owner, found->second.batch_charge);
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
    // The planner has checked all incoming references under this worker's
    // exclusive ownership. This authority is an internal call argument,
    // never a wire field or a generic caller's erase permission.
    return Batch(Object({{"mutations", std::move(mutations)}}), true);
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
    const auto* proposed = request.Find("data");
    const auto found = records_.find(name);
    const bool pending = found != records_.end() && found->second.webhook_deferred;
    const auto kind = request.GetString("kind");
    const auto needs_notice = [&] {
      if (!webhook_detail::IsEventSource(kind) || !proposed) return false;
      const bool creating = found == records_.end();
      if (webhook_detail::Transition(kind, *proposed, creating).empty() &&
          !(kind == "agent_session" && !creating && CancelledLink(*proposed)) &&
          !(kind == "response" &&
            AgentPhaseChanged(*proposed, creating ? nullptr : &found->second)))
        return false;
      const auto prefix = request.GetString("owner") + ".webhook_endpoint.";
      const auto next = records_.lower_bound(prefix);
      return next != records_.end() && next->first.starts_with(prefix);
    };
    if (pending || kind == "batch" || needs_notice() ||
        (proposed && IsBatchPage(*proposed)) ||
        request.GetInt("mutate_before", 0)) {
      auto reply = Batch(Object({{"mutations", Array({request})}}));
      if (reply.ok()) {
        const auto* rows = reply.value.GetArray("data");
        if (!rows || rows->array.size() != 1) return IoError(true);
        auto record = rows->array[0];
        reply.value = std::move(record);
      }
      return reply;
    }
    const auto revision = request.GetInt("revision", -1);
    const auto ttl = request.GetInt("ttl");
    const auto* data = request.Find("data");
    if (revision < 0 || revision >= (1LL << 53) - 1 || !data || ttl < 0 ||
        ttl > options_.max_ttl_seconds)
      return Error(400, "invalid_object_request", "Invalid object write");
    const bool exists = found != records_.end();
    if ((revision == 0 && exists) ||
        (revision > 0 && (!exists || found->second.revision != revision)))
      return Error(409, "object_revision_conflict", "Object revision changed");
    Json previous;
    if (exists && !ReadKnown(name, found->second, &previous)) return IoError();
    if (request.GetString("kind") == "input_snapshot") {
      auto immutable =
          ImmutableSnapshot(*data, exists ? previous.Find("data") : nullptr);
      if (!immutable.ok()) return immutable;
    }
    if (request.GetString("kind") == "tool_run") {
      auto immutable = CheckBatchPageMutation(
          *data, exists ? previous.Find("data") : nullptr);
      if (!immutable.ok()) return immutable;
    }
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
                         options_.max_ttl_seconds, &created, &expires,
                         exists && found->second.retention.active
                             ? found->second.expires : 0);
    if (!timing.ok()) return timing;
    if (request.GetString("kind") == "input_snapshot" && expires != 0)
      return Error(409, "input_snapshot_immutable",
                   "Private snapshot retention is determined by references");
    const auto owner = request.GetString("owner");
    auto record = Object({{"format", Number(1)},
                          {"owner", String(owner)},
                          {"kind", String(request.GetString("kind"))},
                          {"id", String(request.GetString("id"))},
                          {"revision", Number(revision + 1)},
                          {"created_at", Number(created)},
                          {"expires_at", Number(expires)},
                          {"data", *data}});
    SnapshotIdentities checked;
    auto references = PrivateSnapshotReferences(record, {}, &checked);
    if (!references.ok()) return references;
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
    Metadata metadata{owner, request.GetString("kind"), request.GetString("id"),
                      digest, revision + 1, created, expires, text.size(),
                      InspectObjectRetention(request.GetString("kind"), *data)};
    IndexWebhookState(&metadata, *data);
    Add(name, std::move(metadata));
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
           {"owner", RecoveryOwner(owner)},
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

  ObjectReply ScanBatches(const Json& request) {
    const auto after = request.GetString("after");
    if (!after.empty() && !ValidFilename(after))
      return Error(400, "invalid_object_cursor", "Invalid recovery cursor");
    auto current =
        after.empty() ? records_.begin() : records_.upper_bound(after);
    auto rows = Array();
    std::string cursor = after;
    size_t scanned = 0;
    for (; current != records_.end() && scanned < 32 && rows.array.size() < 8;
         ++current) {
      ++scanned;
      cursor = current->first;
      const auto& metadata = current->second;
      if (metadata.kind != "batch") continue;
      Json record;
      if (!ReadKnown(current->first, metadata, &record)) return IoError();
      const auto* data = record.Find("data");
      const auto* batch = data ? data->Find("batch") : nullptr;
      if (!data || data->GetString("format") != "q4t.batch.v1" || !batch)
        continue;
      const auto status = batch->GetString("status");
      if (status != "validating" && status != "in_progress" &&
          status != "finalizing" && status != "cancelling")
        continue;
      auto scope = data->GetString("scope");
      if (scope.empty() || scope.size() > 256 ||
          Digest(scope) != metadata.owner)
        scope.clear();
      const auto* owner = data->Find("owner");
      const auto* declared_endpoint = batch->Find("endpoint");
      const auto endpoint = declared_endpoint &&
                                    declared_endpoint->IsString() &&
                                    declared_endpoint->str.size() <= 64
                                ? declared_endpoint->str
                                : std::string();
      rows.array.push_back(Object(
          {{"id", String(metadata.id)},
           {"scope", String(scope)},
           {"owner_digest", String(metadata.owner)},
           {"owner", RecoveryOwner(owner)},
           {"status", String(status)},
           {"endpoint", String(endpoint)},
           {"execution_deadline_at", Number(batch->GetInt("expires_at"))}}));
    }
    ObjectReply result;
    result.value = Object({{"data", std::move(rows)},
                           {"cursor", String(cursor)},
                           {"scanned", Number(scanned)},
                           {"has_more", Boolean(current != records_.end())}});
    return result;
  }

  ObjectReply ScanVectorFiles(const Json& request) {
    const auto after = request.GetString("after");
    if (!after.empty() && !ValidFilename(after))
      return Error(400, "invalid_object_cursor", "Invalid recovery cursor");
    auto current = records_.upper_bound(after);
    auto rows = Array();
    std::string cursor = after;
    size_t scanned = 0;
    for (; current != records_.end() && scanned < 32 && rows.array.size() < 8;
         ++current, ++scanned) {
      cursor = current->first;
      const auto& metadata = current->second;
      if (metadata.kind != "vector_store_file") continue;
      Json record;
      if (!ReadKnown(current->first, metadata, &record)) return IoError();
      const auto* data = record.Find("data");
      const auto* file = data ? data->Find("file") : nullptr;
      const auto* job = data ? data->Find("job") : nullptr;
      if (!data || data->GetString("format") != "q4t.vector_store_file.v1" ||
          !file || !job || file->GetString("status") != "in_progress")
        continue;
      const auto store_id = file->GetString("vector_store_id");
      const auto parent = records_.find(metadata.owner + ".vector_store." +
                                        store_id + ".json");
      if (parent == records_.end()) continue;
      Json root;
      if (!ReadKnown(parent->first, parent->second, &root)) return IoError();
      const auto* root_data = root.Find("data");
      const auto* store = root_data ? root_data->Find("vector_store") : nullptr;
      const auto* members = root_data ? root_data->GetArray("members") : nullptr;
      if (!store || !members || members->array.size() > 10000 ||
          (store->GetInt("expires_at") > 0 &&
           store->GetInt("expires_at") <= std::time(nullptr)))
        continue;
      bool retained = std::any_of(members->array.begin(), members->array.end(),
                       [&](const auto& member) {
                         return member.GetString("id") == metadata.id &&
                                member.GetString("file_id") == file->GetString("id");
                       });
      if (!retained && data->GetBool("detached")) {
        const auto batch_id = data->GetString("batch_id");
        const auto* batches = root_data->GetArray("batches");
        if (batches && std::any_of(batches->array.begin(), batches->array.end(),
                                   [&](const auto& b) { return b.str == batch_id; })) {
          const auto found = records_.find(metadata.owner + ".vector_store_batch." +
                                            batch_id + ".json");
          if (found != records_.end()) {
            Json batch;
            if (!ReadKnown(found->first, found->second, &batch)) return IoError();
            const auto* batch_data = batch.Find("data");
            const auto* rows = batch_data ? batch_data->GetArray("members") : nullptr;
            retained = rows && std::any_of(rows->array.begin(), rows->array.end(),
                                            [&](const auto& row) {
              return row.GetString("id") == metadata.id && row.GetBool("detached") &&
                     row.GetString("status") == "in_progress";
            });
          }
        }
      }
      if (!retained) continue;
      auto scope = data->GetString("scope");
      if (scope.empty() || scope.size() > 256 || Digest(scope) != metadata.owner)
        scope.clear();
      rows.array.push_back(Object(
          {{"id", String(metadata.id)}, {"scope", String(scope)},
           {"owner_digest", String(metadata.owner)},
           {"store_id", String(store_id)},
           {"file_id", String(file->GetString("id"))},
           {"state", String(job->GetString("state"))},
           {"cancel_requested", Boolean(job->GetBool("cancel_requested"))},
           {"token", String(job->GetString("token"))},
           {"owner", String(job->GetString("owner"))},
           {"deadline_at", Number(job->GetInt("deadline_at"))},
           {"execution_deadline_at", Number(job->GetInt("execution_deadline_at"))},
           {"attempts", Number(job->GetInt("attempts"))}}));
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
      if (metadata.kind == "chatkit_thread") {
        const auto* stored = record.Find("data");
        const auto* thread = stored ? stored->Find("thread") : nullptr;
        if (!thread || !thread->IsObject()) return IoError();
        // Thread lists only need public metadata. Item history stays in the
        // owning record and is fetched explicitly by the items operation.
        Set(&record, "data", Object({{"thread", *thread}}));
      }
      if (request.GetString("kind") == "trace") {
        const auto* data = record.Find("data");
        const auto* trace = data ? data->Find("trace") : nullptr;
        if (!data || data->GetString("format") != "q4t.realtime_trace.v1" ||
            !trace || !trace->IsObject())
          return Error(503, "realtime_trace_record_invalid",
                       "Invalid trace record");
        auto summary = Object();
        for (const auto* key : {"object", "id", "observation_boundary",
                               "version", "session_id", "conversation_id",
                               "workflow_name", "group_id", "status",
                               "close_reason", "started_at_ms", "ended_at_ms",
                               "duration_ms", "revision", "events_seen",
                               "dropped_span_events", "collector_errors"})
          if (const auto* value = trace->Find(key)) Set(&summary, key, *value);
        const auto* spans = trace->GetArray("spans");
        Set(&summary, "span_count", Number(spans ? spans->array.size() : 0));
        Set(&summary, "summary", Boolean(true));
        Set(&Field(record, "data"), "trace", std::move(summary));
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
  std::set<std::pair<int64_t, std::string>> environment_timers_;
  struct PendingEnvironments {
    std::set<std::string> names;
    std::string previous;
  };
  // Rotate owners and then jobs within an owner, including jobs which need
  // another attempt. Inactive tenants cannot monopolize the worker.
  std::map<std::string, PendingEnvironments> pending_environments_;
  std::map<std::string, Usage> clients_;
  BatchQueueQuota batch_quota_;
  Usage total_;
  size_t retention_bytes_ = 0;
  int64_t webhook_capacity_deferrals_ = 0, webhook_capacity_losses_ = 0;
  int64_t environment_expiry_retries_ = 0;
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
  const bool lose_snapshot_ack =
      std::strcmp(argv[1], "--q4t-object-worker-snapshot-lost-ack") == 0;
  const bool lose_cleanup_ack =
      std::strcmp(argv[1], "--q4t-object-worker-read-close-lost-ack") == 0;
  const bool lose_application_ack =
      std::strcmp(argv[1], "--q4t-object-worker-application-lost-ack") == 0;
  const bool lose_batch_create =
      std::strcmp(argv[1], "--q4t-object-worker-batch-create-lost-ack") == 0;
  const bool lose_batch_response_begin =
      std::strcmp(argv[1],
                  "--q4t-object-worker-batch-response-begin-lost-ack") == 0;
  const bool lose_batch_start =
      std::strcmp(argv[1], "--q4t-object-worker-batch-start-lost-ack") == 0;
  const bool lose_batch_result =
      std::strcmp(argv[1], "--q4t-object-worker-batch-result-lost-ack") == 0;
  const bool lose_batch_budget =
      std::strcmp(argv[1], "--q4t-object-worker-batch-budget-lost-ack") == 0;
  const bool lose_batch_recount =
      std::strcmp(argv[1], "--q4t-object-worker-batch-recount-lost-ack") == 0;
  int successful_puts = 0;
  matched = matched || lose_ack || lose_second_ack || lose_snapshot_ack ||
            lose_cleanup_ack || lose_application_ack || lose_batch_create ||
            lose_batch_start || lose_batch_result || lose_batch_budget ||
            lose_batch_recount || lose_batch_response_begin;
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
      if (initialized) {
        const int ready = store.WaitForRequest();
        if (ready < 0) return 4;
        if (ready == 0) continue;
      }
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
      if (reply.ok() &&
          (lose_batch_create || lose_batch_start || lose_batch_result ||
           lose_batch_budget || lose_batch_recount))
        if (const auto* mutations = request.GetArray("mutations")) {
          const bool page_update = std::any_of(
              mutations->array.begin(), mutations->array.end(),
              [](const Json& mutation) {
                const auto* data = mutation.Find("data");
                return mutation.GetString("operation") == "put" && data &&
                       IsBatchPage(*data);
              });
          for (const auto& mutation : mutations->array) {
            const auto* data = mutation.Find("data");
            if (!data || mutation.GetString("operation") != "put") continue;
            const auto format = data->GetString("format");
            const bool root = format == "q4t.batch.v1";
            if ((lose_batch_create && root &&
                 mutation.GetInt("revision", -1) == 0) ||
                (lose_batch_budget && page_update && root &&
                 data->Has("enqueued_input_tokens") &&
                 data->Find("batch")->GetString("status") == "validating") ||
                (lose_batch_recount && page_update && root &&
                 data->Has("input_recount")) ||
                (lose_batch_start && page_update && root &&
                 data->GetInt("running") == 1) ||
                (lose_batch_result &&
                 ((format == "q4t.batch_result.v1" &&
                   mutation.GetInt("revision", -1) == 0) ||
                  (page_update && root && data->GetInt("next_row") > 0 &&
                   data->GetInt("running") == 0 &&
                   data->Find("batch")->GetString("status") == "in_progress"))))
              for (;;) pause();  // Actual atomic commit, no receipt.
          }
        }
      if (reply.ok() && lose_batch_response_begin &&
          request.GetString("operation") == "put" &&
          request.GetString("kind") == "response" &&
          request.GetInt("revision", -1) == 0 && request.Find("data") &&
          request.Find("data")->Has("batch_parent"))
        for (;;) pause();  // Original bound Response committed, no receipt.
      if (reply.ok() && lose_cleanup_ack &&
          request.GetString("operation") == "erase" &&
          request.GetString("kind") == "tool_run")
        for (;;) pause();  // Durable cleanup completed; its receipt is lost.
      if (reply.ok() && lose_application_ack) {
        const auto application = [](const Json& value) {
          const auto kind = value.GetString("kind");
          return value.GetString("operation") == "put" && SupportsApplicationPayload(kind);
        };
        if (application(request))
          for (;;) pause();
        if (const auto* mutations = request.GetArray("mutations"))
          for (const auto& mutation : mutations->array)
            if (application(mutation))
              for (;;) pause();
      }
      if (reply.ok() && lose_snapshot_ack)
        if (const auto* mutations = request.GetArray("mutations"))
          for (const auto& mutation : mutations->array)
            if (mutation.GetString("kind") == "input_snapshot" &&
                mutation.GetString("operation") == "put" &&
                mutation.GetInt("revision", -1) == 0)
              for (;;) pause();  // Atomic snapshot/hold commit, no receipt.
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
