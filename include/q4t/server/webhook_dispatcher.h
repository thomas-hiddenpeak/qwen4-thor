#pragma once

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>

#include "q4t/server/client_policy.h"
#include "q4t/server/webhook.h"

namespace q4t::server {
class WebhookDispatcher {
 public:
  WebhookDispatcher(ObjectStore& objects, ClientAdmission& clients,
                    FileFetchOptions network);
  void RequestStop();
  void Join();
  io::Json Health() const;

 private:
  void Run(ObjectStore& objects, ClientAdmission& clients,
           const FileFetchOptions& network, std::stop_token stop);
  std::mutex mutex_;
  std::condition_variable_any changed_;
  std::atomic<int64_t> acknowledged_{0}, retries_{0}, errors_{0}, deferred_{0},
      dropped_{0}, expiry_errors_{0};
  std::jthread thread_;
};
}  // namespace q4t::server
