// Asynchronous, bounded storage for one client-owned Live recording.
#pragma once

#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

#include "q4t/server/live_recording.h"
#include "q4t/server/http_request.h"

namespace q4t::server {
inline constexpr size_t kLiveRecordingHostBytes = 64U << 20;
class LiveRecordingCapture {
 public:
  LiveRecordingCapture(ObjectStore& objects, std::string scope, std::string id,
                       RealtimeAudioFormat format,
                       std::function<bool()> revoked,
                       std::unique_ptr<HttpBodyLease> memory = {});
  ~LiveRecordingCapture();
  void Audio(LiveAudioChannel channel, uint64_t ticks, std::string_view bytes);
  void Finish(uint64_t end_ticks);
  void Cancel();
  void Join();
  bool Drained() const;
  ObjectReply TakeError();

 private:
  struct Block {
    LiveAudioChannel channel;
    uint64_t ticks;
    std::string bytes;
  };
  BinaryAccess Access();
  void Run() noexcept;
  ObjectReply Store(BinaryStage* stage);
  const std::unique_ptr<HttpBodyLease> memory_;
  ObjectStore& objects_;
  BinaryStore binary_;
  const std::string scope_, id_;
  const RealtimeAudioFormat format_;
  const std::function<bool()> revoked_;
  mutable std::mutex mutex_;
  std::condition_variable ready_;
  std::deque<Block> queue_;
  size_t bytes_ = 0;
  uint64_t end_ticks_ = 0;
  bool finishing_ = false, done_ = false;
  ObjectReply error_;
  std::atomic<bool> stopped_{false};
  std::thread worker_;
};

void SendLiveRecording(int fd, ObjectStore& objects, const BinaryAccess& access,
                       const std::string& id);
}  // namespace q4t::server
