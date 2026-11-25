#pragma once

#include "q4t/server/application_routes.h"
#include "q4t/server/evals.h"

namespace q4t::server {
class ResponsesJobs;
}
namespace q4t::server::eval_detail {
struct SamplingOwner;
bool ReplaysStoredTrajectory(const io::Json& source);
std::string RenderRunReport(const io::Json& run);
void ProjectRun(io::Json* run, std::string_view base);
ObjectReply PublishStoredRun(ObjectStore& objects, const StateAccess& access,
                             const io::Json& config, const io::Json& query,
                             ObjectRequest run, const ObjectRequest& parent);
ObjectReply RunRequest(ObjectStore& objects, const StateAccess& access,
                       const EvalRoute& route, std::string_view method,
                       std::string_view query, const std::string& body,
                       ResponsesJobs* jobs, std::string_view report_base);
ObjectReply RemoveChildren(ObjectStore& objects, const StateAccess& access,
                           ObjectRequest* parent, io::Json* record,
                           ResponsesJobs* jobs);
ObjectReply ResumeRunDeletion(ObjectStore& objects, const StateAccess& access,
                              const io::Json& record, ResponsesJobs* jobs);
ObjectReply AdvanceRun(ObjectStore& objects, const StateAccess& access,
                       const io::Json& record, SamplingOwner* owner = nullptr);
}  // namespace q4t::server::eval_detail
