#include "q4t/server/chat_server.h"

#include <cstdio>

namespace q4t::server {

void ChatServer::RecordMemoryRejection(int fd, MemoryState state,
                                       MemoryCheckpoint checkpoint) {
  const size_t reason = state == MemoryState::kLow ? 0 : 1;
  const size_t point = static_cast<size_t>(checkpoint);
  metrics_.memory_rejected[point][reason].fetch_add(1,
                                                  std::memory_order_relaxed);
  const auto* context = detail::HttpResponseContext::Current(fd);
  std::fprintf(stderr,
               "[q4t][memory-admission] trace_id=%s checkpoint=%s reason=%s\n",
               context ? context->TraceId().c_str() : "-",
               kMemoryCheckpoints[point], kMemoryRejectionReasons[reason]);
}

void ChatServer::RejectMemoryAdmission(int fd, MemoryState state,
                                       MemoryCheckpoint checkpoint) {
  RecordMemoryRejection(fd, state, checkpoint);
  const char* code = state == MemoryState::kLow
                         ? "memory_pressure"
                         : "memory_observation_unavailable";
  detail::SendError(fd, 503, "memory admission temporarily unavailable", {},
                    code);
}

bool ChatServer::CheckMemoryAdmission(int fd, MemoryCheckpoint checkpoint) {
  const auto memory = memory_admission_.Observe(true);
  if (memory.Allowed()) return true;
  RejectMemoryAdmission(fd, memory.state, checkpoint);
  return false;
}

}  // namespace q4t::server
