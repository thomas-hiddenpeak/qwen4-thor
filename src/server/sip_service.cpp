#include "q4t/server/sip_service.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <condition_variable>
#include <filesystem>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/native_crypto.h"
#include "sip_child.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
struct Config {
  std::string scope, ip, certs, subject, port, webhook;
  bool live = false, realtime = false;
};
bool Name(std::string_view text) {
  return !text.empty() && text.size() <= 128 &&
         text.find_first_not_of(
             "0123456789abcdefghijklmnopqrstuvwxyz"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ-_.") == text.npos;
}
bool Load(const std::string& file, std::vector<Config>* configs) {
  std::string raw;
  if (!ReadMcpPrivateConfig(file, &raw)) return false;
  Json j;
  const bool parsed =
      io::ParseJson(raw, &j, false, {12, 4000, 262144, true}).ok();
  CryptoWipe(&raw);
  if (!parsed || !j.IsObject() || j.object.size() != 1) return false;
  auto* list = j.GetArray("trunks");
  if (!list || list->array.empty() || list->array.size() > 8) return false;
  std::set<std::pair<std::string, int>> listeners;
  for (const auto& row : list->array) {
    Config c;
    c.scope = row.GetString("client_id");
    c.certs = row.GetString("certificate_dir");
    c.subject = row.GetString("peer_subject");
    c.ip = row.GetString("listen_ip");
    const auto port = row.GetInt("port", -1);
    auto* webhook = row.Find("webhook");
    if (row.object.size() != (webhook ? 6u : 5u) || !Name(c.scope) ||
        !Name(c.subject) || c.certs.empty() || c.certs.front() != '/' ||
        c.certs.size() > 4096 || c.certs.find('\0') != c.certs.npos ||
        c.ip.find('\0') != c.ip.npos || port < 1 || port > 65535 ||
        !listeners.emplace(c.ip, port).second)
      return false;
    in6_addr address{};
    if (inet_pton(AF_INET, c.ip.c_str(), &address) != 1 &&
        inet_pton(AF_INET6, c.ip.c_str(), &address) != 1)
      return false;
    c.port = std::to_string(port);
    if (webhook) {
      if (!webhook->IsObject() || webhook->Has("scope")) return false;
      auto subscription = *webhook;
      if (const auto* events = webhook->Find("event_types")) {
        if (!events->IsArray() || events->array.empty() ||
            events->array.size() > 2)
          return false;
        for (const auto& type : events->array) {
          if (type.str == "live.transport.incoming" && !c.live)
            c.live = true;
          else if (type.str == "realtime.call.incoming" && !c.realtime)
            c.realtime = true;
          else
            return false;
        }
        std::erase_if(subscription.object,
                      [](const auto& p) { return p.first == "event_types"; });
      } else {
        c.live = true;
      }
      Set(&subscription, "scope", String(c.scope));
      c.webhook = OutputJson(subscription);
      if (c.webhook.size() > 32768) return false;
    }
    configs->push_back(std::move(c));
  }
  return true;
}
ObjectReply Unknown() {
  return Error(504, "sip_outcome_unknown",
               "Carrier receipt unavailable before the call deadline");
}
}  // namespace

class SipService::Trunk {
 public:
  ~Trunk() {
    reader_.request_stop();
    if (reader_.joinable()) reader_.join();
    signal_.Send("STOP");
  }

  bool Start(Config* c, const std::filesystem::path& engine,
             const std::function<bool(std::string_view)>& current_scope) {
    scope_ = c->scope;
    current_scope_ = current_scope;
    if (!current_scope_(scope_)) return false;
    std::vector<std::string> args{"3",      c->ip,      c->port,
                                  c->certs, c->subject, scope_};
    if (!c->webhook.empty()) {
      if (!publisher_.Start(engine, {"--sip-webhook-worker",
                                     std::to_string(getpid()), "1073741824"}) ||
          !publisher_.Send(c->webhook) || !publisher_.Ready("READY " + scope_))
        return false;
      args.push_back("4");
      live_ = c->live;
      realtime_ = c->realtime;
    }
    CryptoWipe(&c->webhook);
    if (!signal_.Start(engine.parent_path() / "q4t-live-sip", std::move(args),
                       publisher_.Fd()))
      return false;
    publisher_.CloseChannel();
    if (!signal_.Ready("READY " + c->port)) return false;
    online_ = true;
    reader_ = std::jthread([this](std::stop_token stop) { Run(stop); });
    return true;
  }

  bool Online() const { return online_.load(); }
  uint64_t WebhookFailures() const { return webhook_failures_.load(); }
  uint64_t Delivered() const { return delivered_.load(); }

  std::optional<ObjectReply> Reject(std::string_view scope, std::string_view id,
                                    int status, Clock::time_point deadline) {
    if (!online_.load() || scope != scope_) return {};
    std::unique_lock lock(mutex_);
    auto it = calls_.find(id);
    if (id.starts_with("rtc_"))
      it = std::find_if(calls_.begin(), calls_.end(), [&](const auto& entry) {
        return entry.second->realtime_id == id;
      });
    if (it == calls_.end()) return {};
    auto call = it->second;
    if (call->requested || call->final)
      return Error(409, "sip_call_conflict", "Call has a final disposition");
    call->requested = status;
    if (!signal_.Send("REJECT " + it->first + " " + scope_ + " " + it->first +
                      " " + std::to_string(status))) {
      call->requested = 0;  // No packet committed to the local channel.
      return Error(503, "sip_control_unavailable", "Call control unavailable");
    }
    if (!updated_.wait_until(lock, deadline, [&] { return call->done; }))
      return Unknown();
    if (call->conflict)
      return Error(409, "sip_call_conflict",
                   "Another disposition won the call");
    if (!call->submitted || call->ack != status) return Unknown();
    return ObjectReply{};
  }

 private:
  struct Call {
    std::string realtime_id;
    int requested = 0, final = 0, ack = 0;
    bool submitted = false, conflict = false, done = false;
  };
  void Event(std::string_view packet) {
    const auto lf = packet.find('\n');
    auto head = packet.substr(0, lf);
    std::array<std::string_view, 4> word{};
    for (auto& part : word) {
      auto space = head.find(' ');
      part = head.substr(0, space);
      if (space == head.npos) break;
      head.remove_prefix(space + 1);
    }
    if (word[0] == "RESULT" && word[1] == "hook") {
      if (word[2] != "submitted") ++webhook_failures_;
      return;
    }
    if (word[0] == "RESULT") {
      auto it = calls_.find(word[1]);
      if (it == calls_.end() || !it->second->requested) return;
      auto& call = *it->second;
      if (word[2] == "submitted")
        call.submitted = true;
      else {
        call.conflict = word[2] == "conflict" || word[2] == "not_found";
        call.done = true;
        updated_.notify_all();
      }
      return;
    }
    if (word[1] != scope_) return;
    if (word[0] == "WEBHOOK") {
      if (word[3] == "delivered")
        ++delivered_;
      else
        ++webhook_failures_;
      return;
    }
    if (!word[2].starts_with("live_") || word[2].size() != 37) return;
    std::string id(word[2]);
    if (word[0] == "INCOMING") {
      if (lf == packet.npos || calls_.size() >= 64 || !current_scope_(scope_)) {
        signal_.Send("REJECT denied " + scope_ + " " + id + " 480");
        return;
      }
      auto call = std::make_shared<Call>();
      if (realtime_) call->realtime_id = detail::NewRequestId("rtc_");
      if (!calls_.emplace(id, call).second) return;
      const auto publish = "PUBLISH hook " + scope_ + " " + id;
      if (live_ && !signal_.Send(publish)) ++webhook_failures_;
      if (realtime_ && !signal_.Send(publish + " " + call->realtime_id))
        ++webhook_failures_;
      return;
    }
    auto it = calls_.find(id);
    if (it == calls_.end()) return;
    auto& call = *it->second;
    if (word[0] == "FINAL") {
      call.final = 1;
    } else if (word[0] == "ACK" || word[0] == "LOST" || word[0] == "FAILED") {
      if (word[0] == "ACK") {
        const auto [end, ec] = std::from_chars(
            word[3].data(), word[3].data() + word[3].size(), call.ack);
        if (ec != std::errc{} || end != word[3].data() + word[3].size())
          call.ack = 0;
      }
      if (word[0] == "ACK" && call.requested && !call.submitted)
        call.conflict = true;
      call.done = true;
      calls_.erase(it);
      updated_.notify_all();
    }
  }
  void Run(std::stop_token stop) {
    std::array<char, 66560> buffer;
    while (!stop.stop_requested()) {
      pollfd event{signal_.Fd(), POLLIN, 0};
      const int result = poll(&event, 1, 100);
      if (result < 0 && errno == EINTR) continue;
      if (result < 0) break;
      if (!result) continue;
      const auto size = recv(signal_.Fd(), buffer.data(), buffer.size(),
                             MSG_TRUNC | MSG_DONTWAIT);
      if (size < 0 && (errno == EINTR || errno == EAGAIN)) continue;
      if (size <= 0 || size_t(size) > buffer.size()) break;
      std::lock_guard lock(mutex_);
      try {
        Event({buffer.data(), size_t(size)});
      } catch (...) {
        // Allocation or identity generation failure belongs to this trunk.
        signal_.Send("STOP");
        break;
      }
    }
    std::lock_guard lock(mutex_);
    online_ = false;
    for (const auto& [_, call] : calls_) call->done = true;
    calls_.clear();
    updated_.notify_all();
  }

  std::string scope_;
  std::function<bool(std::string_view)> current_scope_;
  SipChild publisher_, signal_;
  std::mutex mutex_;
  std::condition_variable updated_;
  std::map<std::string, std::shared_ptr<Call>, std::less<>> calls_;
  std::atomic<bool> online_{false};
  std::atomic<uint64_t> webhook_failures_{0}, delivered_{0};
  bool live_ = false, realtime_ = false;
  std::jthread reader_;
};

SipService::SipService() = default;
SipService::~SipService() { Stop(); }

void SipService::Stop() {
  owner_.request_stop();
  if (owner_.joinable()) owner_.join();
  std::vector<std::shared_ptr<Trunk>> trunks;
  {
    std::lock_guard lock(mutex_);
    trunks.swap(trunks_);
  }
}

void SipService::Start(const std::string& config,
                       std::function<bool(std::string_view)> current_scope) {
  if (config.empty()) return;
  auto run = [this, config,
              current_scope = std::move(current_scope)](std::stop_token stop) {
    try {
      std::error_code ec;
      auto engine = std::filesystem::read_symlink("/proc/self/exe", ec);
      std::vector<Config> configs;
      if (ec || !engine.is_absolute() || !Load(config, &configs)) {
        ++activation_failures_;
        return;
      }
      configured_ = configs.size();
      for (auto& entry : configs) {
        if (stop.stop_requested()) break;
        auto trunk = std::make_shared<Trunk>();
        // Make the registry discoverable before its reader can publish the
        // first incoming event. A subscriber can call HTTP immediately.
        {
          std::lock_guard lock(mutex_);
          trunks_.push_back(trunk);
        }
        if (!trunk->Start(&entry, engine, current_scope)) {
          ++activation_failures_;
          std::lock_guard lock(mutex_);
          std::erase(trunks_, trunk);
        }
        CryptoWipe(&entry.webhook);
      }
    } catch (...) {
      ++activation_failures_;
    }
    // Linux delivers PDEATHSIG when the thread that spawned the publisher
    // exits. Keep that thread alive for the entire serving lifetime.
    std::unique_lock lock(mutex_);
    if (!trunks_.empty()) {
      std::condition_variable_any shutdown;
      shutdown.wait(lock, stop, [] { return false; });
    }
  };
  try {
    owner_ = std::jthread(std::move(run));
  } catch (const std::system_error&) {
    ++activation_failures_;
  }
}

std::string SipService::HealthJson() const {
  std::lock_guard lock(mutex_);
  int ready = 0;
  uint64_t failures = 0, delivered = 0;
  for (const auto& trunk : trunks_) {
    ready += trunk->Online();
    failures += trunk->WebhookFailures();
    delivered += trunk->Delivered();
  }
  return OutputJson(
      Object({{"configured", Number(configured_)},
              {"ready", Number(ready)},
              {"activation_failures", Number(activation_failures_)},
              {"webhooks_delivered", Number(delivered)},
              {"webhook_failures", Number(failures)}}));
}

ObjectReply SipService::Reject(std::string_view scope, std::string_view id,
                               int status, Clock::time_point deadline) {
  std::vector<std::shared_ptr<Trunk>> trunks;
  {
    std::lock_guard lock(mutex_);
    trunks = trunks_;
  }
  for (const auto& trunk : trunks)
    if (auto reply = trunk->Reject(scope, id, status, deadline)) return *reply;
  return Error(404, "not_found", "Call not found");
}
}  // namespace q4t::server
