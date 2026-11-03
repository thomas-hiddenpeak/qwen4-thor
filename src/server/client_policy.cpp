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

bool Integer(const io::Json* value, uint32_t maximum, uint32_t* output) {
  if (!value || !value->IsNumber() || value->str.empty()) return false;
  for (char c : value->str)
    if (c < '0' || c > '9') return false;
  uint32_t number = 0;
  const auto result = std::from_chars(
      value->str.data(), value->str.data() + value->str.size(), number);
  if (result.ec != std::errc() ||
      result.ptr != value->str.data() + value->str.size() || !number ||
      number > maximum)
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
         value.max_body_bytes <= 16 * 1024 * 1024;
}

bool ValidPolicy(const GatewayClientPolicy& policy) {
  if (!Hex64(policy.gateway_token) || !Hex64(policy.sha256) ||
      policy.clients.empty() || policy.clients.size() > kMaxClients)
    return false;
  std::set<std::string> ids;
  for (const auto& limits : policy.clients)
    if (!ValidLimits(limits) || !ids.insert(limits.id).second) return false;
  return true;
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
  if (!io::ParseJson(std::string(raw), &root, true, limits) ||
      !Fields(root, {"version", "gateway_token", "clients"}))
    return Invalid();
  uint32_t version = 0;
  if (!Integer(root.Find("version"), 1, &version)) return Invalid();
  GatewayClientPolicy policy;
  policy.gateway_token = root.GetString("gateway_token");
  const auto* clients = root.GetArray("clients");
  if (!clients || clients->array.empty() || clients->array.size() > kMaxClients)
    return Invalid();
  for (const auto& entry : clients->array) {
    ClientLimits value;
    if (!Fields(entry,
                {"id", "max_inflight", "max_control_inflight",
                 "max_prompt_tokens", "max_output_tokens", "max_body_bytes"}))
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

Status ClientAdmission::Configure(GatewayClientPolicy policy) {
  if (Enabled()) return Status::Fail("client policy already configured");
  if (!ValidPolicy(policy)) return Invalid();
  policy_ = std::move(policy);
  return Status();
}

ClientIdentity ClientAdmission::Authenticate(
    std::string_view id, std::string_view gateway_token) const {
  ClientIdentity result;
  if (!Enabled()) {
    if (id.empty() && gateway_token.empty()) result.owner_ = this;
    return result;
  }
  if (!ValidClientId(id) || gateway_token.size() != 64) return result;
  unsigned char difference = 0;
  for (size_t i = 0; i < 64; ++i)
    difference |= static_cast<unsigned char>(gateway_token[i]) ^
                  static_cast<unsigned char>(policy_.gateway_token[i]);
  if (difference) return result;
  for (size_t i = 0; i < Size(); ++i) {
    if (policy_.clients[i].id != id) continue;
    result.owner_ = this;
    result.limits_ = &policy_.clients[i];
    result.index_ = i;
    return result;
  }
  return result;
}

const ClientLimits& ClientAdmission::Limits(size_t index) const {
  return policy_.clients.at(index);
}

ClientUsage ClientAdmission::Usage(size_t index) const {
  if (index >= Size()) throw std::out_of_range("client counter index");
  const auto& counters = counters_[index];
  return {counters.data.load(), counters.control.load(),
          counters.data_rejected.load(), counters.control_rejected.load()};
}

ClientLease::~ClientLease() {
  if (active_) active_->fetch_sub(1, std::memory_order_release);
}

bool ClientLease::Acquire(ClientAdmission& admission,
                          const ClientIdentity& identity, ClientLane lane) {
  if (attempted_) return false;
  attempted_ = true;
  if (identity.owner_ != &admission ||
      (lane != ClientLane::kData && lane != ClientLane::kControl))
    return false;
  if (!identity.Scoped()) return !admission.Enabled();
  if (identity.index_ >= admission.Size() ||
      identity.limits_ != &admission.policy_.clients[identity.index_])
    return false;
  auto& counters = admission.counters_[identity.index_];
  const bool control = lane == ClientLane::kControl;
  auto& active = control ? counters.control : counters.data;
  auto& rejected = control ? counters.control_rejected : counters.data_rejected;
  const uint32_t limit = control ? identity.limits_->max_control_inflight
                                 : identity.limits_->max_inflight;
  uint32_t previous = active.load(std::memory_order_relaxed);
  while (previous < limit) {
    if (active.compare_exchange_weak(previous, previous + 1,
                                     std::memory_order_acquire,
                                     std::memory_order_relaxed)) {
      active_ = &active;
      return true;
    }
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
  header("q4t_client_policy_enabled", "Immutable gateway policy enabled.",
         "gauge");
  out +=
      "q4t_client_policy_enabled " + std::to_string(admission.Enabled()) + '\n';
  header("q4t_client_requests_active", "Client-owned HTTP request leases.",
         "gauge");
  header("q4t_client_requests_limit", "Configured client HTTP request limit.",
         "gauge");
  header("q4t_client_capacity_rejected_total", "Client quota refusals.",
         "counter");
  for (size_t i = 0; i < admission.Size(); ++i) {
    const auto& limits = admission.Limits(i);
    const auto usage = admission.Usage(i);
    for (const bool control : {false, true}) {
      const auto labels = "{client=\"" + limits.id + "\",lane=\"" +
                          (control ? "control" : "data") + "\"} ";
      out +=
          "q4t_client_requests_active" + labels +
          std::to_string(control ? usage.control_active : usage.data_active) +
          '\n';
      out += "q4t_client_requests_limit" + labels +
             std::to_string(control ? limits.max_control_inflight
                                    : limits.max_inflight) +
             '\n';
      out += "q4t_client_capacity_rejected_total" + labels +
             std::to_string(control ? usage.control_rejected
                                    : usage.data_rejected) +
             '\n';
    }
  }
  return out;
}

}  // namespace q4t::server
