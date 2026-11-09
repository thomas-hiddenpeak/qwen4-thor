// Native durable application objects. The caller supplies authenticated scope;
// request body user/metadata fields must never be used as an ownership key.
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <initializer_list>
#include <memory>
#include <span>
#include <string>
#include <string_view>

#include "q4t/io/json.h"

namespace q4t::server {

enum class ObjectKind {
  kResponse,
  kChat,
  kConversation,
  kConversationItem,
  kFile,
  kBatch,
  kToolRun,
  kPrompt
};
enum class ObjectOperation { kGet, kPut, kErase, kList, kHealth };

struct ObjectStoreOptions {
  std::string directory;
  uint64_t max_object_bytes = 16ULL << 20;
  uint64_t max_client_bytes = 64ULL << 20;
  uint64_t max_total_bytes = 256ULL << 20;
  uint64_t max_client_objects = 1000;
  uint64_t max_total_objects = 10000;
  int64_t max_ttl_seconds = 30 * 24 * 3600;
  int io_timeout_ms = 2000;
  size_t max_queue = 8;
  uint64_t max_queue_bytes = 64ULL << 20;
  int restart_limit = 3;
  int restart_window_ms = 60000;
  int restart_backoff_ms = 100;
};

struct ObjectRequest {
  ObjectOperation operation = ObjectOperation::kGet;
  ObjectKind kind = ObjectKind::kResponse;
  std::string scope;
  std::string id;
  io::Json data;
  // Put: zero creates; a positive value updates exactly that revision.
  // Erase: zero accepts the current revision; positive values are conditional.
  int64_t expected_revision = 0;
  // Zero preserves conversation-associated state until explicit deletion.
  // All persistent state still counts against object and byte quotas.
  int64_t ttl_seconds = 30 * 24 * 3600;
  // Trusted application timestamp; zero uses the worker's clock. Updates
  // cannot change an existing creation time. Never sourced from a user ID.
  int64_t created_at = 0;
  // -1 applies ttl_seconds. Otherwise preserve an absolute retention deadline
  // (zero means no expiry); metadata updates must not extend retention.
  int64_t expires_at = -1;
  int limit = 20;
  std::string after;
  std::string before;
  bool descending = true;
  // Trusted Files list projection: omit binary manifests from IPC pages.
  // Valid only for list/file; durable records and ordinary readers are intact.
  bool file_metadata_only = false;
};

struct ObjectReply {
  io::Json value;
  int http_status = 200;
  std::string code;
  std::string message;
  // A mutation may have committed before a timeout/transport/fsync failure.
  // Never blindly replay it: retrieve the known ID and revision first.
  bool commit_unknown = false;
  bool ok() const { return code.empty(); }
  // Keep the actual internal cause for diagnostics. Public clients must not
  // mistake an unconfirmed mutation for a known failed, retryable operation.
  std::string_view ClientCode() const {
    return commit_unknown ? std::string_view("object_commit_unknown") : code;
  }
  std::string ClientMessage() const {
    if (!commit_unknown) return message;
    std::string text = message +
                       "; commit outcome unknown; retrieve the original "
                       "resource before retrying";
    for (const auto* key : {"id", "response_id"}) {
      const auto* identity = value.Find(key);
      if (identity && identity->IsString())
        text += "; " + std::string(key) + "=" + identity->str;
    }
    return text;
  }
};

struct ObjectStoreHealth {
  std::string state;
  size_t pending = 0;
  uint64_t workers_started = 0;
  uint64_t workers_reaped = 0;
  uint64_t controller_starts = 0;
  uint64_t controller_failures = 0;
  uint64_t controller_recoveries = 0;
};

class ObjectStore {
 public:
  explicit ObjectStore(ObjectStoreOptions options);
  ~ObjectStore();
  ObjectStore(const ObjectStore&) = delete;
  ObjectStore& operator=(const ObjectStore&) = delete;
  ObjectReply Execute(const ObjectRequest& request,
                      std::chrono::steady_clock::time_point deadline,
                      const std::function<bool()>& cancelled = {});
  // All mutations share one authenticated scope and commit atomically. A
  // revision/quota error changes none; a lost receipt requires retrieval.
  ObjectReply ExecuteBatch(std::span<const ObjectRequest> requests,
                           std::chrono::steady_clock::time_point deadline,
                           const std::function<bool()>& cancelled = {});
  ObjectStoreHealth Health() const;
  // Trusted service recovery only, never an HTTP/API operation. Returns a
  // bounded page of active response metadata across scopes, with no input or
  // output content. The scope is accepted only when its digest matches the
  // durable owner; the opaque cursor survives deletion of its prior record.
  ObjectReply ScanResponses(std::string after,
                            std::chrono::steady_clock::time_point deadline,
                            const std::function<bool()>& cancelled = {});
  // Trusted same-UID operator control only. Empty expected_plan inspects;
  // collection requires the exact prior plan digest and rechecks dependencies.
  ObjectReply Retention(int64_t item_created_before, size_t limit,
                        std::string expected_plan,
                        std::chrono::steady_clock::time_point deadline,
                        const std::function<bool()>& cancelled = {});

 private:
  ObjectReply Dispatch(std::string wire, bool mutation,
                       std::chrono::steady_clock::time_point deadline,
                       const std::function<bool()>& cancelled);
  struct State;
  std::shared_ptr<State> state_;
};

// Uses the kernel random source; empty means unavailable, never a weak ID.
std::string NewObjectId(ObjectKind kind);
// Private same-ELF worker entry. No model initialization occurs here.
int MaybeRunObjectWorker(int argc, char** argv);

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
// Host-only fault injection; neither symbol nor selector exists in q4t serve.
void ObjectWorkerEntryForTesting(std::string entry);
void DeferObjectSpawnForTesting(bool enabled);
void FailObjectControllerForTesting();
void FailObjectThreadStartForTesting();
#endif

}  // namespace q4t::server
