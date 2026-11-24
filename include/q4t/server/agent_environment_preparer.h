#pragma once

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>

#include "q4t/server/agent_environment.h"
#include "q4t/server/client_policy.h"

namespace q4t::server {
// Complete one durable prewarm. Concurrent helpers use the original revision.
ObjectReply CompleteEnvironmentPreparation(ObjectStore& objects,
                                           const StateAccess& access,
                                           const std::string& id);

class EnvironmentPreparer {
 public:
  EnvironmentPreparer(ObjectStore& objects, ClientAdmission& clients);
  void RequestStop();
  void Join();
  io::Json Health() const;

 private:
  void Run(ObjectStore& objects, ClientAdmission& clients,
           std::stop_token stop);
  std::mutex mutex_;
  std::condition_variable_any changed_;
  std::atomic<uint64_t> completed_{0}, errors_{0};
  std::jthread thread_;
};
}  // namespace q4t::server
