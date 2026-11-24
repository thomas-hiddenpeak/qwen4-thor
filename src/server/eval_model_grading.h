#pragma once

#include "eval_sampling.h"

namespace q4t::server::eval_detail {
bool ModelCriterion(const io::Json& criterion);
void RecordModelUsage(io::Json* resource, const io::Json& sample);
// Advances one model criterion. HTTP 202 means resume from the durable run;
// HTTP 200 means all model grades are available for the final row transaction.
ObjectReply AdvanceModelGrade(ObjectStore& objects, const StateAccess& access,
                              SamplingOwner* owner, ObjectRequest& write,
                              io::Json& resource, const io::Json& row,
                              ObjectRequest* consumed);
void ClearModelGrades(io::Json& data);
}  // namespace q4t::server::eval_detail
