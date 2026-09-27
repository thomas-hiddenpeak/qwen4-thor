// Request identity and monotonic cancellation, independent of scheduler queues.
#pragma once

#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>

namespace q4t::server {

class RequestControl {
 public:
  enum class State {
    kActive, kDisconnect, kExplicit, kDeadline, kShutdown, kFinished
  };
  RequestControl(std::string id, std::string key,
                 std::chrono::steady_clock::time_point deadline)
      : id_(std::move(id)), key_(std::move(key)), deadline_(deadline) {}
  const std::string& id() const { return id_; }
  bool MatchesKey(const std::string& key) const {
    if (key.empty() || key.size() != key_.size()) return false;
    unsigned char diff = 0;
    for (size_t i = 0; i < key.size(); ++i) diff |= key[i] ^ key_[i];
    return diff == 0;
  }
  // True means cancellation accepted (including a duplicate), not GPU drained.
  bool Cancel(State reason = State::kExplicit) {
    if (reason == State::kActive || reason == State::kFinished) return false;
    State expected = State::kActive;
    return state_.compare_exchange_strong(expected, reason) ||
           (expected != State::kActive && expected != State::kFinished);
  }
  bool Cancelled() {
    if (std::chrono::steady_clock::now() >= deadline_) Cancel(State::kDeadline);
    const State state = state_.load();
    return state != State::kActive && state != State::kFinished;
  }
  const char* Reason() const {
    switch (state_.load()) {
      case State::kDisconnect: return "client_disconnected";
      case State::kExplicit: return "explicit_cancel";
      case State::kDeadline: return "deadline_exceeded";
      case State::kShutdown: return "server_shutdown";
      default: return "not_cancelled";
    }
  }
  // Linearize completion against a concurrent Cancel. No successful response
  // may be finalized if cancellation won. Idempotent for terminal cleanup.
  bool Finish() {
    State expected = State::kActive;
    return state_.compare_exchange_strong(expected, State::kFinished) ||
           expected == State::kFinished;
  }

 private:
  const std::string id_;
  const std::string key_;
  const std::chrono::steady_clock::time_point deadline_;
  std::atomic<State> state_{State::kActive};
};

// Entries exist only while a handler owns the request. Retaining a shared_ptr
// during Cancel cannot affect a newer request reusing the same external ID.
class RequestRegistry {
 public:
  std::shared_ptr<RequestControl> Register(
      const std::string& id, const std::string& key,
      std::chrono::steady_clock::time_point deadline) {
    const std::lock_guard<std::mutex> lock(mu_);
    if (requests_.contains(id)) return nullptr;
    auto request = std::make_shared<RequestControl>(id, key, deadline);
    requests_.emplace(id, request);
    return request;
  }
  bool Cancel(const std::string& id, const std::string& key) {
    const std::lock_guard<std::mutex> lock(mu_);
    const auto it = requests_.find(id);
    return it != requests_.end() && it->second->MatchesKey(key) &&
           it->second->Cancel();
  }
  void CancelAll() {
    const std::lock_guard<std::mutex> lock(mu_);
    for (const auto& [id, request] : requests_) {
      (void)id;
      request->Cancel(RequestControl::State::kShutdown);
    }
  }
  void Remove(const std::shared_ptr<RequestControl>& request) {
    const std::lock_guard<std::mutex> lock(mu_);
    request->Finish();
    const auto it = requests_.find(request->id());
    if (it != requests_.end() && it->second == request) requests_.erase(it);
  }

 private:
  std::mutex mu_;
  std::unordered_map<std::string, std::shared_ptr<RequestControl>> requests_;
};

}  // namespace q4t::server
