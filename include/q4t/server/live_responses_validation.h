// Session-thread owner of immutable backend checks. Only the validator runs
// off-thread. It must honor cancellation around any blocking resource access.
#pragma once

#include <deque>
#include <future>
#include <optional>
#include <stop_token>

#include "q4t/server/object_store.h"

namespace q4t::server {
using LiveResponsesCheck =
    std::function<ObjectReply(const io::Json&, std::stop_token)>;

class LiveResponsesValidation {
 public:
  struct Result {
    ObjectReply reply;
    std::string client_event;
    bool current;
  };
  explicit LiveResponsesValidation(LiveResponsesCheck check);
  ~LiveResponsesValidation();
  // Reserve capacity before an update commits, then publish its private
  // snapshot only after the session acknowledgement is queued.
  ObjectReply CheckCapacity(const io::Json& settings, size_t* bytes) const;
  void Publish(io::Json settings, std::string client_event, size_t bytes);
  std::optional<Result> Poll();
  void Close() noexcept;
  void Join() noexcept;
  bool Drained() const { return !worker_.valid() && queue_.empty(); }
  bool Ready() const { return ready_; }

 private:
  struct Pending {
    io::Json settings;
    std::string event;
    size_t charge;
  };
  const LiveResponsesCheck check_;
  std::deque<Pending> queue_;
  std::future<ObjectReply> worker_;
  std::stop_source stop_;
  size_t bytes_ = 0;
  bool ready_ = false;
};
}  // namespace q4t::server
