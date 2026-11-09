// Local operator control, isolated from HTTP clients and model execution.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <memory>
#include <string>
#include <thread>

#include "q4t/server/state_cipher.h"
#include "q4t/service/guardian_process.h"

namespace q4t::server {
// Abstract Unix address: no filesystem socket cleanup or external daemon.
// Both peers must have the same effective UID. The object directory is the
// service selection; no request can choose a different key store after connect.
std::string StateKeyAdminAddress(const std::string& directory);
ObjectReply HandleStateKeyCommand(ObjectStore& objects, const io::Json& request,
                                  const StateAccess& access);
ObjectReply ExchangeStateKeyCommand(
    const std::string& directory, const io::Json& request,
    std::chrono::steady_clock::time_point deadline);

class StateKeyAdmin {
 public:
  static std::unique_ptr<StateKeyAdmin> Start(
      ObjectStore& objects, const std::string& directory) noexcept;
  ~StateKeyAdmin();
  bool Alive() const noexcept { return alive_.load(); }

 private:
  explicit StateKeyAdmin(ObjectStore& objects) : objects_(objects) {}
  struct Peer {
    service::OwnedFd fd;
    std::chrono::steady_clock::time_point deadline;
    std::string reply;
  };
  void Run(std::stop_token stop) noexcept;
  void Step(std::stop_token stop);
  ObjectStore& objects_;
  service::OwnedFd listener_;
  std::array<Peer, 2> peers_;
  std::atomic<bool> alive_{false};
  std::jthread worker_;
};

// Native q4t state-keys entry; -1 means this is not the selected command.
// Live operation never starts or restarts a model. Explicit --offline uses the
// same exclusive durable worker and fails if a running service owns the store.
int MaybeRunStateKeyCommand(int argc, char** argv);
// Retention shares the authenticated local transport, not key material or
// application credentials. No model work is performed by either command.
int MaybeRunStateRetentionCommand(int argc, char** argv);
}  // namespace q4t::server
