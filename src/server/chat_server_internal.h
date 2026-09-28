#pragma once

#include "q4t/server/chat_server.h"
#include "q4t/io/json.h"

namespace q4t::server::detail {
bool RequestCancelled(RequestControl* control, int fd);
bool ValidRequestKey(const std::string& key);
Status FinishHostReadback(
    cudaError_t copy_error, std::atomic<bool>* gpu_healthy,
    std::span<model::ModelSequence* const> sequences = {},
    const Status& submitted = Status());
std::string JsonEscape(const std::string& s);
bool WriteAll(int fd, const char* data, size_t len);
bool WriteAll(int fd, const std::string& s);
void SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type);
void SendError(int fd, int code, const std::string& message);
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index);
bool PrepareChatInput(int fd, const io::Json& req, std::string* prompt_out,
                      std::vector<VisionItem>* items_out);
}  // namespace q4t::server::detail
