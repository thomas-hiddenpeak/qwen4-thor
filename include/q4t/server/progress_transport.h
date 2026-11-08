// Private, fixed-size observation channel. Failure never controls inference.
#pragma once

#include <array>
#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>

#include "q4t/server/request_progress.h"
#include "q4t/service/guardian_process.h"

namespace q4t::server {

struct ProgressIdentity {
  uint64_t pid = 0;
  uint64_t start_ticks = 0;
  uint64_t pid_namespace = 0;
  uint64_t controller_generation = 0;
  uint64_t model_generation = 0;
  std::array<char, 36> boot_id{};
  std::array<char, 36> service_id{};
  bool Valid() const noexcept;
};

// Little-endian u64 fields; no ABI struct layout or pointers cross processes.
// Header: magic[8], sequence/time/complete, pid/start/namespace,
// controller/model, active/untracked/count, successes/steps, boot[36],
// service[36]. Rows:
// ticket/deadline/stage/stage_since/wait/wait_since/work/work_id/
// work_since/completed/last_completed/updated, each a u64.
struct ProgressPacket {
  static constexpr size_t kHeaderBytes = 184;
  static constexpr size_t kRowBytes = 96;
  std::array<unsigned char,
             kHeaderBytes + kRowBytes * RequestProgress::kCapacity>
      data{};
  size_t size = 0;
};
ProgressPacket EncodeProgress(const RequestProgress::Snapshot& snapshot,
                              const ProgressIdentity& identity,
                              uint64_t sequence, uint64_t successes,
                              uint64_t steps) noexcept;

class ProgressPublisher {
 public:
  // Takes ownership of an already validated private socket. Does not start
  // until Start; Publish is also useful for a one-shot local observation.
  ProgressPublisher(service::OwnedFd socket, ProgressIdentity identity,
                    const RequestProgress& progress,
                    const std::atomic<uint64_t>& successes,
                    const std::atomic<uint64_t>& steps);
  ~ProgressPublisher();
  ProgressPublisher(const ProgressPublisher&) = delete;
  ProgressPublisher& operator=(const ProgressPublisher&) = delete;
  void Start();
  bool Publish() noexcept;
  uint64_t Dropped() const noexcept { return dropped_.load(); }

 private:
  service::OwnedFd socket_;
  const ProgressIdentity identity_;
  const RequestProgress& progress_;
  const std::atomic<uint64_t>& successes_;
  const std::atomic<uint64_t>& steps_;
  std::atomic_flag publishing_ = ATOMIC_FLAG_INIT;
  uint64_t sequence_ = 0;  // Single publisher, not a work-completion counter.
  std::atomic<uint64_t> dropped_{0};
  std::mutex wait_mutex_;
  std::condition_variable wake_;
  std::jthread worker_;
};

// Only a private controller-owned socket with a matching parent identity is
// adopted. Missing/invalid observation setup cannot fail the core service.
std::unique_ptr<ProgressPublisher> StartProgressPublisher(
    const RequestProgress& progress, const std::atomic<uint64_t>& successes,
    const std::atomic<uint64_t>& steps) noexcept;

}  // namespace q4t::server
