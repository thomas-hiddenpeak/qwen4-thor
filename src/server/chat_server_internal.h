#pragma once

#include "q4t/server/chat_server.h"
#include "q4t/io/json.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/serve_fault.h"

namespace q4t::server::detail {
bool RequestCancelled(RequestControl* control, int fd);
bool ValidRequestKey(const std::string& key);
Status FinishHostReadback(cudaError_t copy_error,
                          std::atomic<bool>* gpu_healthy,
                          std::span<model::ModelSequence* const> sequences = {},
                          const Status& submitted = Status(),
                          trace::RouterCollector* trace = nullptr,
                          std::atomic<uint64_t>* execution_steps = nullptr);
bool PrepareChatInput(int fd, const io::Json& req, std::string* prompt_out,
                      std::vector<VisionItem>* items_out);
}  // namespace q4t::server::detail
