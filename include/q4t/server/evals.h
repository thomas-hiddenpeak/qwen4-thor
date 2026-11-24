#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
struct HttpRequestHeaders;
// Returns an empty base on invalid authority, scheme or mount prefix.
std::string EvalReportBase(const HttpRequestHeaders& headers, bool gateway,
                           std::string_view fallback, std::string_view configured);
class ResponsesJobs;
Status ValidateEval(const io::Json& value, const char* root,
                    ChatPlanError* error);
ObjectReply HandleEval(ObjectStore& objects, const StateAccess& access,
                       std::string_view method, std::string_view target,
                       const std::string& body, ResponsesJobs* jobs = nullptr,
                       std::string_view report_base = {});
}  // namespace q4t::server
