#pragma once

#include "assistant_threads_internal.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server::assistant_detail {
// Private execution settings stay with this run even if its Assistant changes.
ObjectReply PrepareRunSettings(const io::Json& assistant_record,
                               const io::Json& thread_data,
                               const io::Json& request,
                               const ChatPlanConfig& config, std::string run_id,
                               int64_t created, io::Json* run,
                               io::Json* native);
ObjectReply CreateRun(ObjectStore& objects, ResponsesJobs* jobs, int fd,
                      const StateAccess& access, const ChatPlanConfig& config,
                      ObjectRequest write, const io::Json& request,
                      bool new_thread, bool ingestion, std::string* admitted);
ObjectReply SubmitOutputs(ObjectStore& objects, ResponsesJobs* jobs, int fd,
                          const StateAccess& access,
                          const ChatPlanConfig& config, ObjectRequest write,
                          const io::Json& request, const std::string& run_id,
                          std::string* admitted);
void StreamAssistantRun(int fd, ObjectStore& objects, const StateAccess& access,
                        const io::Json& initial, AssistantRunRouteKind kind,
                        const std::string& response_id, bool include_search);
struct RunView {
  io::Json run, steps, messages;
  int64_t prompt_tokens = 0;
};
ObjectReply ProjectRun(ObjectStore& objects, const StateAccess& access,
                       const io::Json& entry, RunView* view);
ObjectReply LoadRunPromptBudget(ObjectStore& objects, const StateAccess& access,
                                const ResponseStatePlan& plan,
                                std::optional<uint64_t>* remaining);
bool ActiveRun(const io::Json& run);
// Reads project immutable native output without changing storage. Writers
// persist a terminal projection with their own thread CAS.
ObjectReply ObserveLastRun(ObjectStore& objects, const StateAccess& access,
                           io::Json* thread_data, bool require_idle);
}  // namespace q4t::server::assistant_detail
