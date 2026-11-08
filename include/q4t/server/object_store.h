// Native durable application objects. The caller supplies authenticated scope;
// request body user/metadata fields must never be used as an ownership key.
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <span>
#include <string>

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
  int limit = 20;
  std::string after;
  std::string before;
  bool descending = true;
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
};

struct ObjectStoreHealth {
  std::string state;
  size_t pending = 0;
  uint64_t workers_started = 0;
  uint64_t workers_reaped = 0;
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
#endif

}  // namespace q4t::server
