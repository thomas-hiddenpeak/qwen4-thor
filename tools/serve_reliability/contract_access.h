// Unloaded production-server seam for CPU lifecycle/slot contracts.
#pragma once

#include "q4t/server/chat_server.h"

#include <arpa/inet.h>
#include <sys/socket.h>

namespace q4t::server {
struct ServeContractAccess {
  static void Slots(ChatServer& server, std::vector<int> capacities,
                    int queued) {
    server.max_seq_ = capacities.size();
    server.max_len_ = *std::max_element(capacities.begin(), capacities.end());
    server.seq_capacities_ = std::move(capacities);
    server.seq_free_.assign(server.max_seq_, false);
    server.admission_limits_.queued = queued;
  }
  static int Allocate(ChatServer& server, RequestControl& control, int fd,
                      size_t required) {
    return server.AllocSeqId(&control, fd, required);
  }
  static void Release(ChatServer& server, int slot) { server.FreeSeqId(slot); }
  static void Notify(ChatServer& server) { server.seq_cv_.notify_all(); }
  static size_t Queued(ChatServer& server) {
    const std::lock_guard<std::mutex> lock(server.seq_mu_);
    return server.seq_waiters_.Size();
  }
  static int Free(ChatServer& server) {
    const std::lock_guard<std::mutex> lock(server.seq_mu_);
    return std::count(server.seq_free_.begin(), server.seq_free_.end(), true);
  }
  static uint64_t Rejected(ChatServer& server) {
    return server.metrics_.queue_rejected.load();
  }
  static void StopAdmissions(ChatServer& server) {
    const std::lock_guard<std::mutex> lock(server.seq_mu_);
    server.seq_stopping_ = true;
    server.seq_cv_.notify_all();
  }
  static std::shared_ptr<RequestControl> Register(ChatServer& server,
                                                  const std::string& token) {
    return server.requests_.Register(
        "cancel-contract", token,
        std::chrono::steady_clock::now() + std::chrono::seconds(30));
  }
  static void Configure(ChatServer& server) {
    server.port_ = 0;
    server.conn_cap_ = 2;
    server.model_ready_.store(true);
  }
  static int Business(ChatServer& server) {
    return server.business_conns_.load();
  }
  static void Healthy(ChatServer& server, bool healthy) {
    server.gpu_healthy_.store(healthy);
  }
  static int Port(ChatServer& server) {
    const int fd = server.listen_fd_.load();
    sockaddr_in addr{};
    socklen_t length = sizeof(addr);
    return fd >= 0 &&
                   !getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &length)
               ? ntohs(addr.sin_port)
               : -1;
  }
  static bool Empty(ChatServer& server) {
    const std::lock_guard<std::mutex> lock(server.connections_mu_);
    return server.connections_.empty() && server.active_conns_.load() == 0 &&
           server.business_conns_.load() == 0 &&
           server.control_conns_.load() == 0 && server.listen_fd_.load() == -1;
  }
};
}  // namespace q4t::server
