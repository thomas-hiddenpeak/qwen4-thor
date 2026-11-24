#pragma once

#include "q4t/server/object_store.h"

namespace q4t::server::eval_detail {
// The native sampler consumes Responses input. Graders observe Chat-shaped
// calls.
ObjectReply NormalizeEvalTrajectory(io::Json* messages);
ObjectReply EvalChatTool(const io::Json& item, io::Json* tool);
}  // namespace q4t::server::eval_detail
