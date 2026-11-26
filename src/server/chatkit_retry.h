#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server::chatkit_detail {

// Only the authenticated thread's stored Response supplies these inputs.
struct RetryHistory {
  std::string source_response_id;
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
// An interrupted turn can lack a published input item. Resolve the exact
// user row again from its owning thread and authenticated Response journal.
ObjectReply ReadRetryReference(ResponsesState& state, const StateAccess& access,
                               std::string_view thread, const io::Json& input,
                               const RetryHistory& history);
ObjectReply ApplyRetryHistory(const RetryHistory& history,
                              ResponseStatePlan* plan);

}  // namespace q4t::server::chatkit_detail
