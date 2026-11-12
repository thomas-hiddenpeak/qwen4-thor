// Optional, isolated version selection. This class never grants role launches.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "q4t/service/guardian_journal.h"

namespace q4t::service {

struct VersionFacts {
  bool running = false;
  bool termination_requested = false;
  bool lease_continuous = false;
  bool resources_safe = false;
  bool device_failure = false;
  io::Json generations;
};

class VersionRecovery {
 public:
  VersionRecovery(io::Json config, std::string python, std::string directory,
                  std::string service_id, std::string boot_id, int lease_fd);
  ~VersionRecovery();
  void Tick(int64_t now);
  void Stop(int64_t now);
  // Caller independently pins direct/adopted exits or validates the actual
  // model parent's wait result against its already pinned, exited pidfd.
  bool Signal(const std::string& role, uint64_t generation, int signal,
              int64_t now, const VersionFacts& facts);
  // Only after every old payload descendant and core active slot is gone.
  void Cleaned(int64_t now, const VersionFacts& facts);
  void Confirm(int64_t now, const VersionFacts& facts);
  bool NeedsCleanup() const;
  bool CanLaunch() const;
  bool CanServe() const;
  bool Protected() const;
  bool Available() const;
  bool BindingMatches(const io::Json& binding) const;
  const std::vector<std::string>& Controller() const;
  const std::vector<std::string>& Gateway() const;
  bool Owns(const ProcessIdentity& identity) const;
  bool CleanupConfirmed() const;
  io::Json StatusValue() const;

 private:
  void StartWorker(int64_t now);
  void SetState(const io::Json& value);
  void Submit(const char* operation, const io::Json& args, int64_t now);
  void CheckProbe(int64_t now);
  std::string Target() const;
  io::Json Proof(const VersionFacts& facts, bool device_usable) const;

  io::Json config_, state_, fault_, fault_proof_;
  std::string python_, directory_, service_id_, boot_id_;
  std::vector<std::string> controllers_[2], gateways_[2];
  int lease_fd_;
  std::unique_ptr<JournalClient> worker_;
  ChildProcess probe_;
  std::string operation_, prepared_, error_;
  uint64_t sequence_ = 0;
  uint64_t probe_count_ = 0;
  int64_t retry_at_ = 0, probe_started_ = 0;
  std::vector<int64_t> attempts_;
  bool stopping_ = false, boot_resumed_ = false;
  bool probe_started_once_ = false, probe_reaped_ = true;
  bool probe_passed_ = false, probe_killed_ = false;
};
}  // namespace q4t::service
