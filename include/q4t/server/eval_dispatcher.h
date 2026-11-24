#pragma once

#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <thread>

#include "q4t/server/evals.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
class EvalDispatcher {
 public:
  using Embed = std::function<ObjectReply(
      const io::Json&, const ClientIdentity&, ClientLease&,
      const std::shared_ptr<RequestControl>&)>;
  EvalDispatcher(
      ObjectStore& objects, ClientAdmission& clients, ChatPlanConfig config,
      std::function<ResponsesJobs*()> jobs, Embed embed = {},
      std::chrono::milliseconds model_timeout = std::chrono::seconds(8));
  ~EvalDispatcher();
  void Wake();
  void CancelEmbedding(std::string_view scope, std::string_view eval,
                       std::string_view run);
  void RequestStop();
  void Join();
  uint64_t Errors() const { return errors_.load(); }

 private:
  void Run(ObjectStore& objects, ClientAdmission& clients,
           std::stop_token stop);
  ObjectReply EmbedPair(ObjectStore& objects, ClientAdmission& clients,
                        const ClientIdentity& owner, const StateAccess& access,
                        const io::Json& record, const io::Json& input);
  ChatPlanConfig config_;
  std::function<ResponsesJobs*()> jobs_;
  Embed embed_;
  std::chrono::milliseconds model_timeout_;
  std::mutex mutex_;
  std::shared_ptr<RequestControl> active_embedding_;
  std::string active_scope_, active_eval_, active_run_;
  std::condition_variable_any changed_;
  uint64_t epoch_ = 0;
  std::atomic<uint64_t> errors_{0};
  std::jthread thread_;
};
}  // namespace q4t::server
