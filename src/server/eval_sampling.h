#pragma once

#include "eval_runs.h"
#include "q4t/server/responses_jobs.h"

namespace q4t::server::eval_detail {
struct SamplingOwner {
  ClientAdmission& clients;
  ClientIdentity client;
  ResponsesJobs& jobs;
  const ChatPlanConfig& config;
};
ObjectReply SamplingRequest(const io::Json& source, const io::Json& row,
                            const ChatPlanConfig& config, io::Json* request);
// 202 means the row is durably coupled to a pending native model response.
ObjectReply SampleRow(ObjectStore& objects, const StateAccess& access,
                      SamplingOwner& owner, ObjectRequest& write,
                      const io::Json& source, io::Json* row,
                      ObjectRequest* consumed);
void ClearSampling(io::Json& data);
ObjectReply ReleaseSampling(ObjectStore& objects, const StateAccess& access,
                            ResponsesJobs* jobs, ObjectRequest& parent);
}  // namespace q4t::server::eval_detail
