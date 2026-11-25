#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server::chatkit_detail {

// Only the authenticated thread's stored Response supplies these inputs.
struct RetryHistory {
  io::Json items;
  io::Json snapshots;
  io::Json contracts;
  io::Json web_content;
};

// A missing item in an accessible thread produces the reference server's
// empty stream. Only the caller's proposed thread revision is modified.
ObjectReply SelectRetryHistory(const io::Json& record, std::string_view item_id,
                               io::Json* thread_data, io::Json* user,
                               RetryHistory* history);
ObjectReply ApplyRetryHistory(const RetryHistory& history,
                              ResponseStatePlan* plan);

}  // namespace q4t::server::chatkit_detail
