#include "q4t/server/client_policy.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <initializer_list>
#include <set>
#include <stdexcept>
#include <utility>

#include "q4t/io/json.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {

bool Fields(const io::Json& value,
            std::initializer_list<std::string_view> fields) {
  if (!value.IsObject() || value.object.size() != fields.size()) return false;
  for (const auto& [name, unused] : value.object) {
    (void)unused;
    if (std::find(fields.begin(), fields.end(), name) == fields.end())
      return false;
  }
  return true;
}

bool Integer(const io::Json* value, uint32_t maximum, uint32_t* output,
             bool allow_zero = false) {
  if (!value || !value->IsNumber() || value->str.empty()) return false;
  for (char c : value->str)
    if (c < '0' || c > '9') return false;
  uint32_t number = 0;
  const auto result = std::from_chars(
      value->str.data(), value->str.data() + value->str.size(), number);
  if (result.ec != std::errc() ||
      result.ptr != value->str.data() + value->str.size() ||
      (!number && !allow_zero) || number > maximum)
    return false;
  *output = number;
  return true;
}

bool Hex64(std::string_view value) {
  if (value.size() != 64) return false;
  for (char c : value)
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  return true;
}

bool ValidLimits(const ClientLimits& value) {
  return ValidClientId(value.id) && value.max_inflight >= 1 &&
         value.max_inflight <= 4096 && value.max_control_inflight >= 1 &&
         value.max_control_inflight <= 64 && value.max_prompt_tokens >= 1 &&
         value.max_prompt_tokens <= 1048576 && value.max_output_tokens >= 1 &&
         value.max_output_tokens <= 1048576 && value.max_body_bytes >= 1024 &&
         value.max_body_bytes <= 16 * 1024 * 1024 &&
         value.requests_per_minute <= 1000000 &&
         value.tokens_per_minute <= 1000000000;
}

bool ValidPolicy(const GatewayClientPolicy& policy) {
  if ((policy.version != 1 && policy.version != 2 && policy.version != 3) ||
      policy.revision > kMaxClientRevision ||
      (policy.version < 3 && policy.revision != 0) ||
      (policy.version == 1 && policy.fair_queue) ||
      !Hex64(policy.gateway_token) || !Hex64(policy.sha256) ||
      policy.clients.empty() || policy.clients.size() > kMaxClients)
    return false;
  std::set<std::string> ids;
  for (const auto& limits : policy.clients)
    if (!ValidLimits(limits) || !ids.insert(limits.id).second ||
        (policy.version == 1 &&
         (limits.requests_per_minute || limits.tokens_per_minute)))
      return false;
  return true;
}

bool FairCapacity(const GatewayClientPolicy& policy, int inflight, int queued) {
  if (!policy.fair_queue) return true;
  uint64_t total = 0;
  for (const auto& client : policy.clients) total += client.max_inflight;
  return inflight > 0 && queued >= static_cast<int>(policy.clients.size()) &&
         total <= static_cast<uint64_t>(inflight);
}

bool SameFile(const struct stat& before, const struct stat& after) {
  return before.st_dev == after.st_dev && before.st_ino == after.st_ino &&
         before.st_size == after.st_size && before.st_mode == after.st_mode &&
         before.st_uid == after.st_uid && before.st_nlink == after.st_nlink &&
         before.st_mtim.tv_sec == after.st_mtim.tv_sec &&
         before.st_mtim.tv_nsec == after.st_mtim.tv_nsec &&
         before.st_ctim.tv_sec == after.st_ctim.tv_sec &&
         before.st_ctim.tv_nsec == after.st_ctim.tv_nsec;
}

Status Invalid() { return Status::Fail("invalid gateway client policy"); }

}  // namespace

Status ParseGatewayClientPolicy(std::string_view raw,
                                GatewayClientPolicy* output) {
  if (!output || raw.empty() || raw.size() > kClientPolicyBytes)
    return Invalid();
  io::Json root;
  const io::JsonParseLimits limits{.max_depth = 6,
                                   .max_values = 2048,
                                   .max_string_bytes = kClientPolicyBytes,
                                   .reject_duplicate_keys = true};
  if (!io::ParseJson(std::string(raw), &root, true, limits)) return Invalid();
  uint32_t version = 0;
  if (!Integer(root.Find("version"), 3, &version) ||
      !(version == 1   ? Fields(root, {"version", "gateway_token", "clients"})
        : version == 2 ? Fields(root, {"version", "gateway_token", "clients",
                                       "fair_queue"})
                       : Fields(root, {"version", "gateway_token", "clients",
                                       "fair_queue", "revision"})))
    return Invalid();
  GatewayClientPolicy policy;
  policy.version = version;
  if (version == 3) {
    const auto* revision = root.Find("revision");
    if (!revision || !revision->IsNumber() || revision->str.empty())
      return Invalid();
    for (char c : revision->str)
      if (c < '0' || c > '9') return Invalid();
    const auto result = std::from_chars(
        revision->str.data(), revision->str.data() + revision->str.size(),
        policy.revision);
    if (result.ec != std::errc() ||
        result.ptr != revision->str.data() + revision->str.size() ||
        policy.revision > kMaxClientRevision)
      return Invalid();
  }
  if (version >= 2) {
    const auto* fair = root.Find("fair_queue");
    if (!fair || !fair->IsBool()) return Invalid();
    policy.fair_queue = root.GetBool("fair_queue");
  }
  policy.gateway_token = root.GetString("gateway_token");
  const auto* clients = root.GetArray("clients");
  if (!clients || clients->array.empty() || clients->array.size() > kMaxClients)
    return Invalid();
  for (const auto& entry : clients->array) {
    ClientLimits value;
    if (!(version == 1
              ? Fields(entry, {"id", "max_inflight", "max_control_inflight",
                               "max_prompt_tokens", "max_output_tokens",
                               "max_body_bytes"})
              : Fields(entry, {"id", "max_inflight", "max_control_inflight",
                               "max_prompt_tokens", "max_output_tokens",
                               "max_body_bytes", "requests_per_minute",
                               "tokens_per_minute"})))
      return Invalid();
    if (version >= 2 && (!Integer(entry.Find("requests_per_minute"), 1000000,
                                  &value.requests_per_minute, true) ||
                         !Integer(entry.Find("tokens_per_minute"), 1000000000,
                                  &value.tokens_per_minute, true)))
      return Invalid();
    value.id = entry.GetString("id");
    if (!Integer(entry.Find("max_inflight"), 4096, &value.max_inflight) ||
        !Integer(entry.Find("max_control_inflight"), 64,
                 &value.max_control_inflight) ||
        !Integer(entry.Find("max_prompt_tokens"), 1048576,
                 &value.max_prompt_tokens) ||
        !Integer(entry.Find("max_output_tokens"), 1048576,
                 &value.max_output_tokens) ||
        !Integer(entry.Find("max_body_bytes"), 16 * 1024 * 1024,
                 &value.max_body_bytes))
      return Invalid();
    policy.clients.push_back(std::move(value));
  }
  policy.sha256 = trace::HexDigest(trace::Sha256(
      {reinterpret_cast<const uint8_t*>(raw.data()), raw.size()}));
  if (!ValidPolicy(policy)) return Invalid();
  *output = std::move(policy);
  return Status();
}

Status LoadGatewayClientPolicy(const std::string& path,
                               GatewayClientPolicy* output) {
  const int fd =
      ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
  if (fd < 0) return Status::Fail("cannot open private client policy");
  struct Close {
    int fd;
    ~Close() { ::close(fd); }
  } close{fd};
  struct stat before {
  }, after{};
  if (::fstat(fd, &before) != 0 || !S_ISREG(before.st_mode) ||
      before.st_uid != ::geteuid() || (before.st_mode & 0077) ||
      before.st_nlink != 1 || before.st_size <= 0 ||
      static_cast<uint64_t>(before.st_size) > kClientPolicyBytes)
    return Status::Fail("client policy must be a private owned regular file");
  std::string raw;
  raw.resize(static_cast<size_t>(before.st_size) + 1);
  size_t count = 0;
  while (count < raw.size()) {
    const ssize_t got = ::read(fd, raw.data() + count, raw.size() - count);
    if (got < 0 && errno == EINTR) continue;
    if (got < 0) return Status::Fail("cannot read client policy");
    if (got == 0) break;
    count += static_cast<size_t>(got);
  }
  if (::fstat(fd, &after) != 0 || !SameFile(before, after) ||
      count != static_cast<size_t>(before.st_size))
    return Status::Fail("client policy changed while reading");
  raw.resize(count);
  return ParseGatewayClientPolicy(raw, output);
}

std::string ClientPolicyDigest(const GatewayClientPolicy& policy) {
  if (!ValidPolicy(policy)) return {};
  std::string raw = "q4t-client-policy-v1\n" + std::to_string(policy.version) +
                    '\n' + std::to_string(policy.revision) + '\n' +
                    (policy.fair_queue ? "1\n" : "0\n");
  for (const auto& client : policy.clients) {
    raw += client.id;
    for (const auto value : {client.max_inflight, client.max_control_inflight,
                             client.max_prompt_tokens, client.max_output_tokens,
                             client.max_body_bytes, client.requests_per_minute,
                             client.tokens_per_minute})
      raw += ':' + std::to_string(value);
    raw += '\n';
  }
  return trace::HexDigest(trace::Sha256(
      {reinterpret_cast<const uint8_t*>(raw.data()), raw.size()}));
}

Status ClientAdmission::Configure(GatewayClientPolicy policy) {
  const std::lock_guard lock(policy_mutex_);
  if (Enabled()) return Status::Fail("client policy already configured");
  if (!ValidPolicy(policy)) return Invalid();
  policy_.store(std::make_shared<const GatewayClientPolicy>(std::move(policy)),
                std::memory_order_release);
  return Status();
}

Status ClientAdmission::Publish(GatewayClientPolicy policy,
                                uint64_t expected_revision,
                                uint32_t global_inflight,
                                uint32_t global_queued) {
  const std::lock_guard lock(policy_mutex_);
  const auto previous = Snapshot();
  if (!previous || previous->version != 3 || policy.version != 3 ||
      !ValidPolicy(policy) || expected_revision != previous->revision ||
      policy.revision <= previous->revision ||
      policy.gateway_token != previous->gateway_token ||
      policy.fair_queue != previous->fair_queue ||
      policy.clients.size() < previous->clients.size() ||
      global_inflight > INT32_MAX || global_queued > INT32_MAX ||
      !q4t::server::FairCapacity(policy, global_inflight, global_queued))
    return Status::Fail("invalid client policy update");
  for (size_t i = 0; i < previous->clients.size(); ++i)
    if (policy.clients[i].id != previous->clients[i].id)
      return Status::Fail("client slots cannot be reassigned");
  policy_.store(std::make_shared<const GatewayClientPolicy>(std::move(policy)),
                std::memory_order_release);
  return Status();
}

bool ClientAdmission::FairCapacity(int inflight, int queued) const {
  const auto policy = Snapshot();
  return !policy || q4t::server::FairCapacity(*policy, inflight, queued);
}

void ClientAdmission::Counters::Advance(ClientClock::time_point now) {
  if (!window_started || now - window >= std::chrono::seconds(60)) {
    window_started = true;
    window = now;
    rate.requests = rate.tokens = 0;
  }
}

ClientRateUsage ClientAdmission::RateUsage(size_t index,
                                           ClientClock::time_point now) const {
  if (index >= Size()) throw std::out_of_range("client rate index");
  const auto& counter = counters_[index];
  const std::lock_guard lock(counter.mutex);
  auto value = counter.rate;
  if (!counter.window_started ||
      now - counter.window >= std::chrono::seconds(60)) {
    value.requests = value.tokens = 0;
  } else {
    value.window_remaining_ms = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            counter.window + std::chrono::seconds(60) - now)
            .count());
  }
  return value;
}

ClientRateResult ClientRateLease::Reserve(ClientAdmission& admission,
                                          const ClientIdentity& identity,
                                          uint64_t prompt, uint64_t output,
                                          ClientClock::time_point now) {
  if (attempted_) return ClientRateResult::kInvalid;
  attempted_ = true;
  if (identity.owner_ != &admission || !identity.Allowed())
    return ClientRateResult::kInvalid;
  if (!identity.Scoped())
    return admission.Enabled() ? ClientRateResult::kInvalid
                               : ClientRateResult::kAllowed;
  const auto policy = admission.Snapshot();
  if (!policy || identity.index_ >= policy->clients.size() ||
      identity.Id() != policy->clients[identity.index_].id)
    return ClientRateResult::kInvalid;
  const auto* limits = &policy->clients[identity.index_];
  if (prompt > limits->max_prompt_tokens || output > limits->max_output_tokens)
    return ClientRateResult::kInvalid;
  // v3 records usage even while enforcement is disabled. Re-enabling a quota
  // cannot erase requests, tokens or outstanding reservations in the window.
  if (policy->version < 3 && !limits->requests_per_minute &&
      !limits->tokens_per_minute)
    return ClientRateResult::kAllowed;
  auto& counter = admission.counters_[identity.index_];
  const std::lock_guard lock(counter.mutex);
  counter.Advance(now);
  auto& rate = counter.rate;
  if (limits->requests_per_minute &&
      rate.requests >= limits->requests_per_minute) {
    ++rate.rpm_rejected;
    return ClientRateResult::kRpm;
  }
  const uint64_t amount = prompt + output;
  if (limits->tokens_per_minute &&
      rate.tokens + rate.reserved + amount > limits->tokens_per_minute) {
    ++rate.tpm_rejected;
    return ClientRateResult::kTpm;
  }
  ++rate.requests;
  ++rate.admitted;
  rate.reserved += amount;
  counters_ = &counter;
  prompt_ = prompt;
  reserved_ = amount;
  return ClientRateResult::kAllowed;
}

void ClientRateLease::Settle(ClientClock::time_point now) noexcept {
  if (!counters_) return;
  const std::lock_guard lock(counters_->mutex);
  counters_->Advance(now);
  // Charge observed logical usage even if a handler ever exceeds its budget.
  // Such a bug must not silently turn into unaccounted tokens.
  const uint64_t actual = started_ ? admitted_prompt_ + completion_ : 0;
  auto& rate = counters_->rate;
  rate.reserved -= reserved_;
  rate.tokens += actual;
  rate.settled_tokens += actual;
  counters_ = nullptr;
}

ClientIdentity ClientAdmission::Authenticate(
    std::string_view id, std::string_view gateway_token) const {
  ClientIdentity result;
  const auto policy = Snapshot();
  if (!policy) {
    if (id.empty() && gateway_token.empty()) result.owner_ = this;
    return result;
  }
  if (!ValidClientId(id) || gateway_token.size() != 64) return result;
  unsigned char difference = 0;
  for (size_t i = 0; i < 64; ++i)
    difference |= static_cast<unsigned char>(gateway_token[i]) ^
                  static_cast<unsigned char>(policy->gateway_token[i]);
  if (difference) return result;
  for (size_t i = 0; i < policy->clients.size(); ++i) {
    if (policy->clients[i].id != id) continue;
    result.owner_ = this;
    result.policy_ = policy;
    result.index_ = i;
    return result;
  }
  return result;
}

ClientLimits ClientAdmission::Limits(size_t index) const {
  const auto policy = Snapshot();
  if (!policy) throw std::out_of_range("client policy index");
  return policy->clients.at(index);
}

ClientIdentity ClientAdmission::RecoveryIdentity(size_t index) const {
  ClientIdentity result;
  const auto policy = Snapshot();
  if (!policy || index >= policy->clients.size()) {
    if (!policy && index == 0) result.owner_ = this;
    return result;
  }
  result.owner_ = this;
  result.policy_ = policy;
  result.index_ = index;
  return result;
}

ClientUsage ClientAdmission::Usage(size_t index) const {
  if (index >= Size()) throw std::out_of_range("client counter index");
  const auto& counters = counters_[index];
  return {counters.data.load(),          counters.control.load(),
          counters.data_rejected.load(), counters.control_rejected.load(),
          counters.cancel.load(),        counters.cancel_rejected.load()};
}

ClientLease::~ClientLease() {
  if (!active_) return;
  {
    const std::lock_guard lock(counters_->mutex);
    if (handing_off_) --*finishing_;
    active_->fetch_sub(1, std::memory_order_release);
  }
  counters_->changed.notify_all();
}

bool ClientLease::TransferTo(ClientLease& destination) noexcept {
  if (&destination == this || !admitted_ || destination.attempted_)
    return false;
  destination.active_ = active_;
  destination.counters_ = counters_;
  destination.finishing_ = finishing_;
  destination.handing_off_ = handing_off_;
  destination.attempted_ = true;
  destination.admitted_ = true;
  active_ = nullptr;
  counters_ = nullptr;
  finishing_ = nullptr;
  handing_off_ = false;
  admitted_ = false;
  return true;
}

void ClientLease::BeginHandoff() noexcept {
  if (!active_ || handing_off_) return;
  const std::lock_guard lock(counters_->mutex);
  ++*finishing_;
  handing_off_ = true;
}

bool ClientLease::Acquire(ClientAdmission& admission,
                          const ClientIdentity& identity, ClientLane lane) {
  if (attempted_) return false;
  attempted_ = true;
  if (identity.owner_ != &admission || !identity.Allowed() ||
      (lane != ClientLane::kData && lane != ClientLane::kControl &&
       lane != ClientLane::kCancel))
    return false;
  if (!identity.Scoped()) {
    admitted_ = !admission.Enabled();
    return admitted_;
  }
  const auto policy = admission.Snapshot();
  if (!policy || identity.index_ >= policy->clients.size() ||
      identity.Id() != policy->clients[identity.index_].id)
    return false;
  auto& counters = admission.counters_[identity.index_];
  const bool control = lane == ClientLane::kControl;
  const bool cancel = lane == ClientLane::kCancel;
  auto& active = cancel    ? counters.cancel
                 : control ? counters.control
                           : counters.data;
  auto& rejected = cancel    ? counters.cancel_rejected
                   : control ? counters.control_rejected
                             : counters.data_rejected;
  const uint32_t limit =
      cancel    ? 1
      : control ? policy->clients[identity.index_].max_control_inflight
                : policy->clients[identity.index_].max_inflight;
  auto& finishing = cancel    ? counters.cancel_finishing
                    : control ? counters.control_finishing
                              : counters.data_finishing;
  std::unique_lock lock(counters.mutex);
  if (active.load(std::memory_order_relaxed) >= limit && finishing) {
    counters.changed.wait_for(lock, kClientHandoffWait, [&] {
      return active.load(std::memory_order_relaxed) < limit || finishing == 0;
    });
  }
  if (active.load(std::memory_order_relaxed) < limit) {
    active.fetch_add(1, std::memory_order_relaxed);
    counters_ = &counters;
    active_ = &active;
    finishing_ = &finishing;
    admitted_ = true;
    return true;
  }
  rejected.fetch_add(1, std::memory_order_relaxed);
  return false;
}

std::string ClientPolicyMetrics(const ClientAdmission& admission) {
  std::string out;
  const auto header = [&](const char* name, const char* help,
                          const char* type) {
    out += std::string("# HELP ") + name + ' ' + help + "\n# TYPE " + name +
           ' ' + type + '\n';
  };
  header("q4t_client_policy_enabled", "Gateway client policy enabled.",
         "gauge");
  out +=
      "q4t_client_policy_enabled " + std::to_string(admission.Enabled()) + '\n';
  header("q4t_client_requests_active", "Client-owned HTTP request leases.",
         "gauge");
  header("q4t_client_requests_limit", "Configured client HTTP request limit.",
         "gauge");
  header("q4t_client_capacity_rejected_total", "Client quota refusals.",
         "counter");
  header("q4t_client_rate_requests", "Accepted chats in current fixed window.",
         "gauge");
  header("q4t_client_rate_tokens", "Settled tokens in current fixed window.",
         "gauge");
  header("q4t_client_rate_reserved_tokens", "Outstanding token reservations.",
         "gauge");
  header("q4t_client_rate_window_remaining_ms",
         "Fixed window remaining milliseconds.", "gauge");
  header("q4t_client_rate_admitted_total",
         "Chats tracked by logical rate accounting.", "counter");
  header("q4t_client_rate_settled_tokens_total",
         "Logical tokens settled since startup.", "counter");
  header("q4t_client_rate_rejected_total", "Minute quota refusals by reason.",
         "counter");
  header("q4t_client_rate_limit", "Configured minute quota; zero disables.",
         "gauge");
  const auto policy = admission.Snapshot();
  for (size_t i = 0; policy && i < policy->clients.size(); ++i) {
    const auto& limits = policy->clients[i];
    const auto usage = admission.Usage(i);
    const auto rate = admission.RateUsage(i);
    const auto client_label = "{client=\"" + limits.id + "\"} ";
    const auto gauge = [&](const char* name, uint64_t value) {
      out += std::string(name) + client_label + std::to_string(value) + '\n';
    };
    gauge("q4t_client_rate_requests", rate.requests);
    gauge("q4t_client_rate_tokens", rate.tokens);
    gauge("q4t_client_rate_reserved_tokens", rate.reserved);
    gauge("q4t_client_rate_window_remaining_ms", rate.window_remaining_ms);
    gauge("q4t_client_rate_admitted_total", rate.admitted);
    gauge("q4t_client_rate_settled_tokens_total", rate.settled_tokens);
    for (bool tpm : {false, true}) {
      const auto labels = "{client=\"" + limits.id + "\",kind=\"" +
                          (tpm ? "tpm" : "rpm") + "\"} ";
      out += "q4t_client_rate_rejected_total" + labels +
             std::to_string(tpm ? rate.tpm_rejected : rate.rpm_rejected) + '\n';
      out += "q4t_client_rate_limit" + labels +
             std::to_string(tpm ? limits.tokens_per_minute
                                : limits.requests_per_minute) +
             '\n';
    }
    for (const auto lane :
         {ClientLane::kData, ClientLane::kControl, ClientLane::kCancel}) {
      const bool control = lane == ClientLane::kControl;
      const bool cancel = lane == ClientLane::kCancel;
      const auto labels = "{client=\"" + limits.id + "\",lane=\"" +
                          (cancel    ? "cancel"
                           : control ? "control"
                                     : "data") +
                          "\"} ";
      out += "q4t_client_requests_active" + labels +
             std::to_string(cancel    ? usage.cancel_active
                            : control ? usage.control_active
                                      : usage.data_active) +
             '\n';
      out += "q4t_client_requests_limit" + labels +
             std::to_string(cancel    ? 1
                            : control ? limits.max_control_inflight
                                      : limits.max_inflight) +
             '\n';
      out += "q4t_client_capacity_rejected_total" + labels +
             std::to_string(cancel    ? usage.cancel_rejected
                            : control ? usage.control_rejected
                                      : usage.data_rejected) +
             '\n';
    }
  }
  return out;
}

}  // namespace q4t::server
