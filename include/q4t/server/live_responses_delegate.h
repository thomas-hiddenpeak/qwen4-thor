// One Live session's independent, authenticated Responses conversation.
#pragma once

#include <condition_variable>
#include <future>

#include "q4t/server/live_responses_execution.h"
#include "q4t/server/live_responses_turn.h"

namespace q4t::server {
struct LiveResponsesNative {
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  std::function<LiveResponsesResult(const io::Json&, io::Json,
                                    const std::shared_ptr<RequestControl>&,
                                    NativeResponsesStream::Sink)>
      execute;
};
class LiveResponsesDelegate {
 public:
  LiveResponsesDelegate(LiveResponsesNative native, LiveEventSink emit,
                        std::function<ObjectReply(std::string)> speak);
  ~LiveResponsesDelegate();
  void Settings(io::Json settings) { settings_ = std::move(settings); }
  void Ready(bool ready) { ready_ = ready; }
  ObjectReply Add(const io::Json& item);
  ObjectReply Create(std::string client_event);
  // Complete frontend control calls only. The voice frontend's private
  // instructions and internal tool/acknowledgement history are never copied.
  ObjectReply Delegate(std::string id, io::Json task);
  ObjectReply Tick(uint64_t offset_ms);
  io::Json ForkHistory() const;
  ObjectReply Restore(const io::Json& state);
  bool Running() const { return worker_.valid(); }
  void Close() noexcept;
  void Interrupt() noexcept;
  void Join() noexcept;

 private:
  struct Queued {
    io::Json event;
    size_t bytes;
  };
  ObjectReply Start(std::string delegation, std::string client_event,
                    io::Json task);
  ObjectReply Enqueue(const io::Json& event,
                      const std::shared_ptr<RequestControl>& control);
  ObjectReply Report(ObjectReply error);
  ObjectReply Next();
  void Release() noexcept;
  const LiveResponsesNative native_;
  const LiveEventSink emit_;
  const std::function<ObjectReply(std::string)> speak_;
  io::Json settings_, history_, added_;
  std::unique_ptr<LiveResponsesTurn> turn_;
  std::shared_ptr<RequestControl> control_;
  std::future<LiveResponsesResult> worker_;
  std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<Queued> mailbox_;
  size_t bytes_ = 0;
  std::string command_;
  bool ready_ = false, closing_ = false, interrupted_ = false;
};
}  // namespace q4t::server
