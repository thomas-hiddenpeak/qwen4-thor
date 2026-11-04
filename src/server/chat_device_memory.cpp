#include "q4t/server/chat_server.h"

#include <cstdio>

#include "q4t/server/device_memory.h"

namespace q4t::server {

void ChatServer::RecordDeviceMemoryFailure(const DeviceMemoryResult& result,
                                           const char* site) noexcept {
  if (result.ok()) return;
  ApplyDeviceMemoryFailure(result, gpu_healthy_, serving_failed_,
                           stop_requested_);
  std::fprintf(
      stderr, "[q4t][device-memory] site=%s reason=%s error=%d pending=%d\n",
      site, result.Reason(), result.operation_error, result.pending_error);
}

}  // namespace q4t::server
