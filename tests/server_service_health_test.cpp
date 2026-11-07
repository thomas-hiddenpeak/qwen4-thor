#include "q4t/server/service_health.h"

#include <atomic>
#include <string_view>
#include <thread>
#include <sys/socket.h>
#include <unistd.h>

#include "q4t/io/json.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

namespace {
using q4t::runtime::ModelFault;
using q4t::runtime::ModelHealth;
using q4t::server::MemoryState;
using q4t::server::ServiceHealth;
using namespace std::string_view_literals;
}  // namespace

Q4T_TEST(service_health_ple_failure_is_unready_but_not_cuda_or_liveness) {
  ServiceHealth health;
  health.model_ready = true;
  Q4T_CHECK(health.Ready() && health.Healthy());
  for (auto fault : {ModelFault::kPleRead, ModelFault::kPleUpload}) {
    health.model_fault = fault;
    Q4T_CHECK(!health.Ready() && !health.Healthy());
    Q4T_CHECK(health.live && health.gpu_healthy);
    Q4T_CHECK(health.State() == "unhealthy"sv);
    Q4T_CHECK(health.ErrorCode() == "model_unhealthy"sv);
    q4t::io::Json json;
    Q4T_CHECK(q4t::io::ParseJson(health.ReadinessJson(), &json).ok());
    Q4T_CHECK(json.GetString("status") == "unhealthy");
    Q4T_CHECK(json.GetBool("live") && !json.GetBool("ready"));
  }
  return true;
}

Q4T_TEST(service_health_resource_recovery_never_clears_model_failure) {
  ServiceHealth health;
  health.model_ready = true;
  ModelHealth model;
  model.Fail(ModelFault::kPleRead);
  for (auto memory : {MemoryState::kLow, MemoryState::kUnavailable,
                      MemoryState::kOpen, MemoryState::kDisabled}) {
    health.memory = memory;
    health.model_fault = model.Fault();
    Q4T_CHECK(!health.Ready() && health.State() == "unhealthy"sv);
  }
  model.Fail(ModelFault::kNone);
  model.Fail(ModelFault::kPleUpload);
  Q4T_CHECK(model.Fault() == ModelFault::kPleRead);
  return true;
}

Q4T_TEST(service_health_temporary_admission_does_not_poison_backend) {
  ServiceHealth health;
  health.model_ready = true;
  for (auto memory : {MemoryState::kLow, MemoryState::kUnavailable}) {
    health.memory = memory;
    Q4T_CHECK(!health.Ready() && health.Healthy() && health.live);
    Q4T_CHECK(health.ErrorCode() == ""sv);
  }
  health.memory = MemoryState::kOpen;
  Q4T_CHECK(health.Ready());
  health.draining = true;
  Q4T_CHECK(!health.Ready() && health.Healthy());
  Q4T_CHECK(health.State() == "draining"sv);
  return true;
}

Q4T_TEST(service_health_startup_and_fatal_priority) {
  ServiceHealth health;
  Q4T_CHECK(!health.Ready() && health.State() == "starting"sv);
  health.model_ready = true;
  health.draining = true;
  health.memory = MemoryState::kLow;
  health.model_fault = ModelFault::kPleRead;
  health.gpu_healthy = false;
  Q4T_CHECK(!health.Ready() && health.State() == "unhealthy"sv);
  Q4T_CHECK(health.ErrorCode() == "gpu_unhealthy"sv);
  health = {};
  health.live = false;
  Q4T_CHECK(!health.Ready() && !health.Healthy());
  Q4T_CHECK(health.ErrorCode() == "server_unhealthy"sv);
  return true;
}

Q4T_TEST(service_health_fault_observation_is_thread_safe_and_sticky) {
  ModelHealth model;
  std::atomic<bool> published{false};
  std::jthread writer([&] {
    model.Fail(ModelFault::kPleRead);
    published.store(true);
    for (int i = 0; i < 10000; ++i) {
      model.Fail(ModelFault::kNone);
      model.Fail(ModelFault::kPleUpload);
    }
  });
  while (!published.load()) std::this_thread::yield();
  for (int i = 0; i < 10000; ++i)
    Q4T_CHECK(model.Fault() == ModelFault::kPleRead);
  return true;
}

Q4T_TEST(service_health_model_error_preserves_single_json_or_sse_terminal) {
  using namespace q4t::server::detail;
  for (bool stream : {false, true}) {
    int sockets[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    struct Close {
      int* sockets;
      ~Close() {
        close(sockets[0]);
        close(sockets[1]);
      }
    } close{sockets};
    HttpResponseContext context(sockets[0]);
    if (stream) Q4T_CHECK(BeginStreamingResponse(sockets[0], context.State()));
    ServiceHealth health;
    health.model_ready = true;
    health.model_fault = ModelFault::kPleRead;
    SendError(sockets[0], 503, "model backend is unhealthy", {},
              health.ErrorCode());
    SendError(sockets[0], 500, "duplicate terminal");
    shutdown(sockets[0], SHUT_WR);
    std::string wire;
    char buffer[4096];
    ssize_t bytes;
    while ((bytes = recv(sockets[1], buffer, sizeof(buffer), 0)) > 0)
      wire.append(buffer, bytes);
    Q4T_CHECK(wire.find("\"code\":\"model_unhealthy\"") != wire.npos);
    Q4T_CHECK(wire.find("duplicate terminal") == wire.npos);
    Q4T_CHECK(wire.find("HTTP/1.1", 1) == wire.npos);
    if (stream) {
      Q4T_CHECK(wire.starts_with("HTTP/1.1 200 "));
      Q4T_CHECK(wire.ends_with("data: [DONE]\r\n\r\n"));
      Q4T_CHECK(wire.find("[DONE]", wire.find("[DONE]") + 1) == wire.npos);
      Q4T_CHECK(wire.find("\"finish_reason\":\"stop\"") == wire.npos);
    } else {
      Q4T_CHECK(wire.starts_with("HTTP/1.1 503 "));
    }
  }
  return true;
}
