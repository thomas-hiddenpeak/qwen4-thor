#pragma once

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>

#include "q4t/server/agent_automation.h"

namespace q4t::server {
class ResponsesJobs;

// One auxiliary dispatcher shares the existing background execution queue.
// Scheduling has no ownership of the model or its recovery controller.
class AutomationDispatcher {
 public:
  AutomationDispatcher(ObjectStore& objects, ClientAdmission& clients,
                       ResponsesJobs& jobs, ChatPlanConfig config);
  void Wake();
  void RequestStop();
  void Join();
  uint64_t Errors() const { return errors_.load(); }

 private:
  void Run(ObjectStore& objects, ClientAdmission& clients, ResponsesJobs& jobs,
           const ChatPlanConfig& config, std::stop_token stop);
  std::mutex mutex_;
  std::condition_variable_any changed_;
  uint64_t epoch_ = 0;
  std::atomic<uint64_t> errors_{0};
  std::jthread thread_;
};
}  // namespace q4t::server
