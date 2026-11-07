#include "q4t/server/chat_server.h"

#include "q4t/ple/ple_embedding.h"

namespace q4t::server {

ServiceHealth ChatServer::ObserveHealth(MemoryState memory) const noexcept {
  ServiceHealth health;
  health.live = !serving_failed_.load();
  health.gpu_healthy = gpu_healthy_.load();
  health.draining = stop_requested_.load();
  health.model_ready = model_ready_.load();
  health.memory = memory;
  // Start publishes the immutable model only after loading it. Run joins all
  // handlers before destruction. Only the dependency's atomic code is read;
  // a busy/stalled model lock cannot block health endpoints.
  if (health.model_ready && model_.Get().ple_emb)
    health.model_fault = model_.Get().ple_emb->HealthFault();
  return health;
}

Status ChatServer::ExecutionStatus() const {
  const auto health = ObserveHealth();
  if (health.Healthy()) return {};
  return Status::Fail(std::string(health.ErrorCode()) + ": " +
                      runtime::ModelFaultName(health.model_fault));
}

bool ChatServer::RejectUnhealthy(int fd, const detail::WriteOptions& options) {
  const auto health = ObserveHealth();
  if (health.Healthy()) return false;
  detail::SendError(fd, 503,
                    "model backend is unhealthy; operator recovery required",
                    options, health.ErrorCode());
  return true;
}

}  // namespace q4t::server
