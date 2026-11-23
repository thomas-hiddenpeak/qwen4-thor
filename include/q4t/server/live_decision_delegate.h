// Immutable, session-local Decisions work for manual, voice and transcript
// input.
#pragma once

#include <deque>
#include <future>
#include <set>

#include "q4t/server/live_decision_execution.h"
#include "q4t/server/live_decision_items.h"

namespace q4t::server {
struct LiveDecisionNative {
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  std::function<ObjectReply(const io::Json&,
                            const std::shared_ptr<RequestControl>&)>
      execute;
  bool Bound() const { return acquire && release && execute; }
};

Status CheckLiveDecisionSettings(const io::Json& configuration,
                                 const ChatPlanConfig& catalog,
                                 ChatPlanError* error);

class LiveDecisionDelegate {
 public:
  LiveDecisionDelegate(LiveDecisionNative native, LiveEventSink emit,
                       ChatPlanConfig catalog, const LiveDecisionItems& items,
                       const io::Json& initial, size_t output_limit);
  ~LiveDecisionDelegate();
  void InputsChanged() { ++revision_; }
  void Settings(const io::Json& configuration);
  ObjectReply Create(std::string command, uint64_t offset);
  ObjectReply Delegate(std::string id, io::Json task, uint64_t offset);
  ObjectReply Transcript(std::string type, std::string delta,
                         std::string event_id, int64_t begin_ms);
  ObjectReply Tick(bool allow_start);
  ObjectReply RestoreHistory(const io::Json& history);
  ObjectReply ForkHistory(io::Json* history) const;
  bool Executing() const { return worker_.valid(); }
  bool Known(std::string_view delegation) const;
  bool Running() const { return !jobs_.empty(); }
  void Close() noexcept { closing_ = true; }
  void Cancel() noexcept;
  void Join() noexcept;

 private:
  struct Job {
    std::string command, delegation, trigger, source, type;
    io::Json request;
    size_t charge = 0;
    int64_t begin_ms = -1;
    uint64_t revision = 0;
    std::chrono::steady_clock::time_point deadline;
  };
  ObjectReply Queue(Job job, const io::Json& settings, io::Json task,
                    uint64_t offset);
  ObjectReply ErrorEvent(const ObjectReply& error, const std::string& command);
  void Release() noexcept;
  const LiveDecisionNative native_;
  const LiveEventSink emit_;
  const ChatPlanConfig catalog_;
  const LiveDecisionItems& items_;
  const size_t output_limit_;
  io::Json conversation_, delegation_, triggers_;
  std::set<std::string, std::less<>> commands_, delegations_;
  std::deque<Job> jobs_;
  std::shared_ptr<RequestControl> control_;
  std::future<ObjectReply> worker_;
  uint64_t revision_ = 0;
  int64_t last_start_ms_ = -1;
  size_t bytes_ = 0, history_bytes_ = 0;
  bool closing_ = false, cancelled_ = false, history_full_ = false;
};
}  // namespace q4t::server
