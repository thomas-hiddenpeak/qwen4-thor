#pragma once

#include "q4t/server/application_routes.h"
#include "q4t/server/evals.h"

namespace q4t::server::eval_detail {
ObjectReply RunRequest(ObjectStore& objects, const StateAccess& access,
                       const EvalRoute& route, std::string_view method,
                       std::string_view query, const std::string& body);
ObjectReply RemoveChildren(ObjectStore& objects, const StateAccess& access,
                           ObjectRequest* parent, io::Json* record);
ObjectReply ResumeRunDeletion(ObjectStore& objects, const StateAccess& access,
                              const io::Json& record);
ObjectReply AdvanceRun(ObjectStore& objects, const StateAccess& access,
                       const io::Json& record);
}  // namespace q4t::server::eval_detail
