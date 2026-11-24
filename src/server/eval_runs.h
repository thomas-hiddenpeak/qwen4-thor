#pragma once

#include "q4t/server/application_routes.h"
#include "q4t/server/evals.h"

namespace q4t::server {
class ResponsesJobs;
}
namespace q4t::server::eval_detail {
struct SamplingOwner;
ObjectReply RunRequest(ObjectStore& objects, const StateAccess& access,
                       const EvalRoute& route, std::string_view method,
                       std::string_view query, const std::string& body,
                       ResponsesJobs* jobs);
ObjectReply RemoveChildren(ObjectStore& objects, const StateAccess& access,
                           ObjectRequest* parent, io::Json* record,
                           ResponsesJobs* jobs);
ObjectReply ResumeRunDeletion(ObjectStore& objects, const StateAccess& access,
                              const io::Json& record, ResponsesJobs* jobs);
ObjectReply AdvanceRun(ObjectStore& objects, const StateAccess& access,
                       const io::Json& record, SamplingOwner* owner = nullptr);
}  // namespace q4t::server::eval_detail
