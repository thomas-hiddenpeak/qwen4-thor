#pragma once

#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <thread>

#include "q4t/server/evals.h"

namespace q4t::server {
class EvalDispatcher {
 public:
  EvalDispatcher(ObjectStore& objects, ClientAdmission& clients,
                 ChatPlanConfig config, std::function<ResponsesJobs*()> jobs);
  void Wake();
  void RequestStop();
  void Join();
  uint64_t Errors() const { return errors_.load(); }

 private:
  void Run(ObjectStore& objects, ClientAdmission& clients,
           std::stop_token stop);
  ChatPlanConfig config_;
  std::function<ResponsesJobs*()> jobs_;
  std::mutex mutex_;
  std::condition_variable_any changed_;
  uint64_t epoch_ = 0;
  std::atomic<uint64_t> errors_{0};
  std::jthread thread_;
};
}  // namespace q4t::server
