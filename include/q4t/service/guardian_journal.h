// Nonblocking native client for the isolated Python journal worker.
#pragma once

#include <cstdint>
#include <optional>
#include <string>

#include "q4t/io/json.h"
#include "q4t/service/guardian_process.h"

namespace q4t::service {

enum class JournalState {
  kNew,
  kStarting,
  kReady,
  kBusy,
  kFrozen,
  kStopping,
  kStopped
};

struct JournalReply {
  uint64_t sequence = 0;
  uint64_t revision = 0;
  std::string transaction_id;
  io::Json result;
};

class JournalClient {
 public:
  ~JournalClient();
  // Persistent directory initialization is a separate explicit operation.
  // Start is never retried on this object, even if the worker fails or stalls.
  Status Start(const std::string& python, const std::string& script,
               const std::string& directory, const std::string& service_id,
               int64_t now_ns, int64_t timeout_ns);
  Status Submit(const std::string& operation, const io::Json& args,
                int64_t now_ns, uint64_t* sequence);
  // Tick, Submit and BeginStop contain no waits or ordinary filesystem writes.
  // A failure closes the channel and freezes new authority, not the model.
  Status Tick(int64_t now_ns);
  void BeginStop(int64_t now_ns, int64_t grace_ns);
  std::optional<JournalReply> TakeReply();
  JournalState State() const { return state_; }
  const io::Json& Snapshot() const { return snapshot_; }
  const ProcessIdentity& Worker() const { return worker_.identity; }
  int WorkerPidfd() const { return worker_.pidfd.Get(); }
  bool CleanupConfirmed() const { return reaped_; }
  const std::string& Failure() const { return failure_; }

 private:
  Status Freeze(std::string reason);
  Status Receive();
  JournalState state_ = JournalState::kNew;
  ChildProcess worker_;
  OwnedFd socket_;
  io::Json snapshot_;
  std::optional<JournalReply> reply_;
  std::string service_id_;
  std::string request_id_;
  std::string failure_;
  uint64_t sequence_ = 0;
  uint64_t revision_ = 0;
  int64_t timeout_ns_ = 0;
  int64_t deadline_ns_ = 0;
  int64_t cleanup_deadline_ns_ = 0;
  int64_t last_now_ns_ = 0;
  bool reaped_ = false;
  bool kill_sent_ = false;
};

}  // namespace q4t::service
