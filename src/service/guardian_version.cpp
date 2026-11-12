#include "q4t/service/guardian_version.h"

#include <signal.h>

#include <algorithm>
#include <stdexcept>
#include <utility>

#include "q4t/service/guardian_wire.h"
#include "q4t/trace/sha256.h"

namespace q4t::service {
namespace {
namespace w = wire;
using io::Json;
constexpr int64_t kSecond = 1'000'000'000;
void Need(bool valid, const char* reason) {
  if (!valid) throw std::runtime_error(reason);
}
bool Hash(const std::string& value) {
  return value.size() == 64 &&
         value.find_first_not_of("0123456789abcdef") == std::string::npos;
}
uint64_t Number(const Json& value, const char* key) {
  uint64_t result = 0;
  Need(w::GetUnsigned(value, key, &result).ok(), "invalid version integer");
  return result;
}
std::string Text(const Json& value, const char* key) {
  const auto* found = value.Find(key);
  Need(found && found->IsString() && found->str.size() <= 4096 &&
           found->str.find('\0') == std::string::npos,
       "invalid version string");
  return found->str;
}
bool Equal(const Json& left, const Json& right) {
  if (left.type != right.type) return false;
  if (left.IsObject()) {
    if (left.object.size() != right.object.size()) return false;
    for (const auto& [key, value] : left.object) {
      const auto* other = right.Find(key);
      if (!other || !Equal(value, *other)) return false;
    }
    return true;
  }
  if (left.IsArray()) {
    if (left.array.size() != right.array.size()) return false;
    for (size_t i = 0; i < left.array.size(); ++i)
      if (!Equal(left.array[i], right.array[i])) return false;
    return true;
  }
  std::string a, b;
  return w::Encode(left, &a).ok() && w::Encode(right, &b).ok() && a == b;
}
std::vector<std::string> Command(const Json& value) {
  Need(value.IsArray() && !value.array.empty() && value.array.size() <= 64,
       "invalid version command");
  std::vector<std::string> result;
  for (const auto& arg : value.array) {
    Need(arg.IsString() && arg.str.size() <= 4096 &&
             arg.str.find('\0') == std::string::npos,
         "invalid version command argument");
    result.push_back(arg.str);
  }
  Need(result[0].starts_with('/'), "version command must be absolute");
  return result;
}
}  // namespace

VersionRecovery::VersionRecovery(Json config, std::string python,
                                 std::string directory, std::string service_id,
                                 std::string boot_id, int lease_fd)
    : config_(std::move(config)),
      python_(std::move(python)),
      directory_(std::move(directory)),
      service_id_(std::move(service_id)),
      boot_id_(std::move(boot_id)),
      lease_fd_(lease_fd) {
  Need(w::HasKeys(config_, {"registry", "payloads", "journal", "cuda_probe"}),
       "invalid version recovery configuration");
  for (const char* name : {"journal", "cuda_probe"})
    Need(Text(config_, name).starts_with('/'),
         "absolute version helper required");
  const auto& registry = *config_.Find("registry");
  Need(w::HasKeys(registry,
                  {"format", "cycle_id", "root_manifest_sha256", "payloads",
                   "compatibility_sha256", "qualification_sha256"}) &&
           registry.GetString("format") == "q4t-version-registry-v1" &&
           Text(registry, "cycle_id").size() == 36,
       "invalid version registry");
  for (const char* name :
       {"root_manifest_sha256", "compatibility_sha256", "qualification_sha256"})
    Need(Hash(Text(registry, name)), "invalid version registry hash");
  Need(w::HasKeys(*registry.Find("payloads"), {"primary", "fallback"}) &&
           w::HasKeys(*config_.Find("payloads"), {"primary", "fallback"}),
       "exactly two version payloads required");
  int index = 0;
  for (const char* name : {"primary", "fallback"}) {
    const auto& identity = *registry.Find("payloads")->Find(name);
    Need(w::HasKeys(identity, {"manifest_sha256", "binding_sha256"}) &&
             Hash(Text(identity, "manifest_sha256")) &&
             Hash(Text(identity, "binding_sha256")),
         "invalid payload identity");
    const auto& payload = *config_.Find("payloads")->Find(name);
    Need(w::HasKeys(payload, {"controller", "gateway", "binding"}),
         "invalid version payload");
    controllers_[index] = Command(*payload.Find("controller"));
    gateways_[index++] = Command(*payload.Find("gateway"));
    const auto& binding = *payload.Find("binding");
    Need(w::HasKeys(binding, {"binary_sha256", "protocol_sha256", "port",
                              "operations_port", "profile_capacities",
                              "profile_registry_sha256", "controller_sha256",
                              "ingress_sha256"}),
         "invalid version binding");
    for (const char* field :
         {"binary_sha256", "protocol_sha256", "profile_registry_sha256",
          "controller_sha256", "ingress_sha256"})
      Need(Hash(Text(binding, field)), "invalid payload binding hash");
    for (const char* port : {"port", "operations_port"})
      Need(Number(binding, port) >= 1024 && Number(binding, port) <= 65535,
           "invalid payload port");
    Need(w::HasKeys(*binding.Find("profile_capacities"), {"primary", "light"}),
         "invalid qualified payload capacities");
    for (const char* profile : {"primary", "light"})
      Need(Hash(Text(*binding.Find("profile_capacities"), profile)),
           "invalid payload capacity hash");
  }
}

VersionRecovery::~VersionRecovery() {
  if (!probe_reaped_ && probe_.pidfd.Get() >= 0)
    SignalProcess(probe_.pidfd.Get(), SIGKILL);
}
void VersionRecovery::StartWorker(int64_t now) {
  std::erase_if(attempts_,
                [now](int64_t at) { return now - at >= 60 * kSecond; });
  if (attempts_.size() >= 3 || now < retry_at_) return;
  attempts_.push_back(now);
  worker_ = std::make_unique<JournalClient>();
  boot_resumed_ = false;
  operation_.clear();
  const auto result =
      worker_->Start(python_, Text(config_, "journal"), directory_, service_id_,
                     now, 5 * kSecond, JournalDomain::kVersion);
  if (!result.ok()) error_ = result.message();
}
void VersionRecovery::SetState(const Json& value) {
  Need(w::HasKeys(value,
                  {"format", "revision", "registry", "boot_id", "selected",
                   "faults", "switch", "completed", "terminal_fault"}) &&
           value.GetString("format") == "q4t-version-selection-v1" &&
           value.Find("registry") &&
           Equal(*value.Find("registry"), *config_.Find("registry")) &&
           value.Has("switch") && value.Has("completed") &&
           value.Has("terminal_fault") && value.Has("faults"),
       "version state does not match frozen registration");
  Need(value.Find("faults")->IsArray() &&
           value.Find("faults")->array.size() <= 1 &&
           (value.GetString("selected") == "primary" ||
            value.GetString("selected") == "fallback") &&
           (value.Find("switch")->IsNull() ||
            w::HasKeys(*value.Find("switch"),
                       {"request_id", "fault", "generations"})) &&
           (value.Find("completed")->IsNull() ||
            w::HasKeys(*value.Find("completed"),
                       {"generations", "binding_sha256"})) &&
           (value.Find("terminal_fault")->IsNull() ||
            value.Find("terminal_fault")->IsObject()),
       "invalid version state projection");
  state_ = value;
}
void VersionRecovery::Submit(const char* operation, const Json& args,
                             int64_t now) {
  if (!worker_ || worker_->State() != JournalState::kReady ||
      !operation_.empty() || stopping_)
    return;
  auto result = worker_->Submit(operation, args, now, &sequence_);
  if (result.ok())
    operation_ = operation;
  else
    error_ = result.message();
}
void VersionRecovery::Tick(int64_t now) {
  CheckProbe(now);
  if (!worker_) {
    if (!stopping_) StartWorker(now);
    return;
  }
  worker_->Tick(now);
  if (stopping_) {
    worker_->BeginStop(now, kSecond);
    return;
  }
  if (worker_->State() == JournalState::kFrozen) {
    error_ = worker_->Failure();
    worker_->BeginStop(now, kSecond);
    retry_at_ = now + 5 * kSecond;
  }
  if (worker_->State() == JournalState::kStopped) {
    if (worker_->CleanupConfirmed()) StartWorker(now);
    return;
  }
  try {
    if (auto reply = worker_->TakeReply()) {
      Need(reply->sequence == sequence_ && reply->result.Has("state"),
           "unbound version receipt");
      SetState(*reply->result.Find("state"));
      if (operation_ == "resume") boot_resumed_ = true;
      if (operation_ == "observe") {
        fault_ = Json{};
        prepared_.clear();
      }
      if (operation_ == "prepare") {
        const auto* receipt = reply->result.Find("preparation");
        const auto target = Target();
        const auto& identity =
            *config_.Find("registry")->Find("payloads")->Find(target);
        const auto& binding =
            *config_.Find("payloads")->Find(target)->Find("binding");
        Need(receipt && receipt->GetString("payload") == target &&
                 receipt->GetString("manifest_sha256") ==
                     identity.GetString("manifest_sha256") &&
                 receipt->GetString("binding_sha256") ==
                     identity.GetString("binding_sha256") &&
                 receipt->GetString("profile_registry_sha256") ==
                     binding.GetString("profile_registry_sha256"),
             "payload preparation receipt differs");
        prepared_ = target;
        probe_started_once_ = false;
        probe_passed_ = false;
      }
      operation_.clear();
      error_.clear();
    }
    if (worker_->State() == JournalState::kReady && !boot_resumed_ &&
        operation_.empty()) {
      SetState(*worker_->Snapshot().Find("state"));
      // An unknown observe receipt may already have consumed the cycle. Its
      // persisted intent wins; never submit a second attribution on restart.
      if (!fault_.IsNull() &&
          ((!state_.Find("switch")->IsNull() &&
            Equal(*state_.Find("switch")->Find("fault"), fault_)) ||
           (!state_.Find("terminal_fault")->IsNull() &&
            Equal(*state_.Find("terminal_fault"), fault_)))) {
        fault_ = Json{};
        prepared_.clear();
      }
      Submit("resume", w::Object({{"boot_id", w::String(boot_id_)}}), now);
    }
  } catch (const std::exception& error) {
    error_ = error.what();
    worker_->BeginStop(now, kSecond);
    retry_at_ = now + 5 * kSecond;
  }
}

std::string VersionRecovery::Target() const {
  return !state_.IsNull() && !state_.Find("switch")->IsNull() ? "fallback"
                                                              : "primary";
}
bool VersionRecovery::Protected() const {
  return !state_.IsNull() && !state_.Find("terminal_fault")->IsNull();
}
bool VersionRecovery::Available() const {
  return worker_ && boot_resumed_ &&
         (worker_->State() == JournalState::kReady ||
          worker_->State() == JournalState::kBusy);
}
bool VersionRecovery::NeedsCleanup() const {
  return !fault_.IsNull() || Protected() ||
         (!prepared_.empty() && prepared_ != Target());
}
bool VersionRecovery::CanLaunch() const {
  return !stopping_ && !state_.IsNull() && !Protected() && fault_.IsNull() &&
         !prepared_.empty() && prepared_ == Target();
}
bool VersionRecovery::CanServe() const {
  return CanLaunch() &&
         (Target() == "primary" || !state_.Find("completed")->IsNull());
}
Json VersionRecovery::Proof(const VersionFacts& facts,
                            bool device_usable) const {
  const auto& identity =
      *config_.Find("registry")->Find("payloads")->Find(Target());
  return w::Object(
      {{"desired", w::String(facts.running ? "RUNNING" : "STOPPED")},
       {"boot_id", w::String(boot_id_)},
       {"payload_sha256", *identity.Find("manifest_sha256")},
       {"generations", facts.generations},
       {"termination_requested", w::Boolean(facts.termination_requested)},
       {"independent_observation", w::Boolean(true)},
       {"identity_valid", w::Boolean(true)},
       {"lease_continuous", w::Boolean(facts.lease_continuous)},
       {"resources_safe", w::Boolean(facts.resources_safe)},
       {"device_usable", w::Boolean(device_usable && !facts.device_failure)}});
}
bool VersionRecovery::Signal(const std::string& role, uint64_t generation,
                             int signal, int64_t now,
                             const VersionFacts& facts) {
  (void)now;
  if (!CanLaunch() || !worker_ || worker_->State() != JournalState::kReady ||
      !boot_resumed_ || !operation_.empty() || !facts.running ||
      facts.termination_requested || !facts.lease_continuous ||
      !facts.resources_safe || facts.device_failure ||
      (signal != SIGILL && signal != SIGABRT && signal != SIGSEGV) ||
      (role != "controller" && role != "model" && role != "gateway") ||
      generation == 0 || Number(facts.generations, role.c_str()) != generation)
    return false;
  const auto& identity =
      *config_.Find("registry")->Find("payloads")->Find(Target());
  const std::string fingerprint =
      identity.GetString("manifest_sha256") + ":" + role +
      ":native_process_signal:" + std::to_string(signal);
  const auto digest = trace::Sha256(
      std::span(reinterpret_cast<const uint8_t*>(fingerprint.data()),
                fingerprint.size()));
  fault_ = w::Object({{"role", w::String(role)},
                      {"generation", w::Unsigned(generation)},
                      {"boot_id", w::String(boot_id_)},
                      {"payload_sha256", *identity.Find("manifest_sha256")},
                      {"kind", w::String("native_process_signal")},
                      {"signal", w::Unsigned(signal)},
                      {"fingerprint", w::String(trace::HexDigest(digest))}});
  fault_proof_ = Proof(facts, false);
  probe_started_once_ = false;
  probe_passed_ = false;
  return true;
}
void VersionRecovery::CheckProbe(int64_t now) {
  if (probe_reaped_ || probe_.pidfd.Get() < 0) return;
  int code = 0;
  const auto result = ReapProcess(probe_.pidfd.Get(), &probe_reaped_, &code);
  if (!result.ok()) {
    error_ = "version probe reap unavailable";
    return;
  }
  if (probe_reaped_) {
    probe_passed_ = code == 0 && !probe_killed_ && !stopping_;
    if (!probe_passed_) error_ = "version device proof unavailable";
  } else if ((stopping_ || now - probe_started_ >= 5 * kSecond) &&
             !probe_killed_) {
    SignalProcess(probe_.pidfd.Get(), SIGKILL);
    probe_killed_ = true;
  }
}
void VersionRecovery::Cleaned(int64_t now, const VersionFacts& facts) {
  if (stopping_ || !facts.running || facts.termination_requested ||
      !facts.lease_continuous || !facts.resources_safe ||
      facts.device_failure || Protected() || !worker_ ||
      worker_->State() != JournalState::kReady || !boot_resumed_ ||
      !operation_.empty())
    return;
  if (!fault_.IsNull()) {
    if (!probe_started_once_) {
      probe_started_once_ = true;
      ++probe_count_;
      probe_reaped_ = false;
      probe_killed_ = false;
      probe_ = ChildProcess{};
      probe_started_ = now;
      const auto result =
          SpawnProcess({Text(config_, "cuda_probe")}, {}, {lease_fd_}, &probe_);
      if (!result.ok()) {
        probe_reaped_ = probe_.identity.pid < 0;
        error_ = "version device probe could not start";
      }
      return;
    }
    if (!probe_reaped_ || !probe_passed_) return;
    // Preserve generations and pre-fault memory facts. Current independent
    // lease/resources/device checks must also pass before committing blame.
    auto proof = fault_proof_;
    for (auto& [key, value] : proof.object)
      if (key == "device_usable") value = w::Boolean(true);
    Submit("observe", w::Object({{"event", fault_}, {"proof", proof}}), now);
    return;
  }
  if (!CanLaunch()) {
    Submit("prepare",
           w::Object({{"payload", w::String(Target())},
                      {"root_manifest_sha256",
                       *config_.Find("registry")->Find("root_manifest_sha256")},
                      {"cleanup_complete", w::Boolean(true)},
                      {"roles_retired", w::Boolean(true)},
                      {"lease_continuous", w::Boolean(true)}}),
           now);
  }
}
void VersionRecovery::Confirm(int64_t now, const VersionFacts& facts) {
  if (!CanLaunch() || Target() != "fallback" || CanServe() || !facts.running ||
      facts.termination_requested || !facts.resources_safe ||
      !facts.lease_continuous || facts.device_failure)
    return;
  Submit("finish",
         w::Object({{"proof", Proof(facts, true)},
                    {"binding_sha256", *config_.Find("registry")
                                            ->Find("payloads")
                                            ->Find("fallback")
                                            ->Find("binding_sha256")}}),
         now);
}
bool VersionRecovery::BindingMatches(const Json& binding) const {
  if (!CanLaunch()) return false;
  const auto& expected =
      *config_.Find("payloads")->Find(prepared_)->Find("binding");
  for (const char* key : {"binary_sha256", "protocol_sha256"})
    if (binding.GetString(key) != expected.GetString(key)) return false;
  for (const char* key : {"port", "operations_port"}) {
    uint64_t actual = 0;
    if (!w::GetUnsigned(binding, key, &actual).ok() ||
        actual != Number(expected, key))
      return false;
  }
  const auto capacity = binding.GetString("capacity_sha256");
  const auto& profiles = *expected.Find("profile_capacities");
  return capacity == profiles.GetString("primary") ||
         capacity == profiles.GetString("light");
}
const std::vector<std::string>& VersionRecovery::Controller() const {
  return controllers_[prepared_ == "fallback" ? 1 : 0];
}
const std::vector<std::string>& VersionRecovery::Gateway() const {
  return gateways_[prepared_ == "fallback" ? 1 : 0];
}
void VersionRecovery::Stop(int64_t now) {
  stopping_ = true;
  if (worker_) worker_->BeginStop(now, kSecond);
  CheckProbe(now);
}
bool VersionRecovery::Owns(const ProcessIdentity& identity) const {
  return (worker_ && identity == worker_->Worker()) ||
         identity == probe_.identity;
}
bool VersionRecovery::CleanupConfirmed() const {
  return (!worker_ || worker_->CleanupConfirmed()) && probe_reaped_;
}
Json VersionRecovery::StatusValue() const {
  const auto& registry = *config_.Find("registry");
  Json last;
  if (!state_.IsNull()) {
    if (Protected())
      last = *state_.Find("terminal_fault");
    else if (!state_.Find("faults")->array.empty())
      last = state_.Find("faults")->array.back();
  }
  return w::Object(
      {{"enabled", w::Boolean(true)},
       {"root_manifest_sha256", *registry.Find("root_manifest_sha256")},
       {"cycle_id", *registry.Find("cycle_id")},
       {"selected", state_.IsNull() ? Json{} : *state_.Find("selected")},
       {"launch_payload", state_.IsNull() ? Json{} : w::String(Target())},
       {"payload_manifest_sha256", prepared_.empty()
                                       ? Json{}
                                       : *registry.Find("payloads")
                                              ->Find(prepared_)
                                              ->Find("manifest_sha256")},
       {"fallback_consumed",
        w::Boolean(!state_.IsNull() && Target() == "fallback")},
       {"protected", w::Boolean(Protected())},
       {"pending", w::Boolean(!state_.IsNull() && Target() == "fallback" &&
                              state_.Find("completed")->IsNull())},
       {"payload_prepared", w::Boolean(CanLaunch())},
       {"state_available",
        w::Boolean(worker_ && boot_resumed_ &&
                   (worker_->State() == JournalState::kReady ||
                    worker_->State() == JournalState::kBusy))},
       {"worker_pid",
        worker_ && !worker_->CleanupConfirmed() && worker_->Worker().pid > 0
            ? w::Unsigned(worker_->Worker().pid)
            : Json{}},
       {"worker_start_ticks",
        w::Unsigned(worker_ ? worker_->Worker().start_ticks : 0)},
       {"probe_pid", !probe_reaped_ && probe_.identity.pid > 0
                         ? w::Unsigned(probe_.identity.pid)
                         : Json{}},
       {"probe_count", w::Unsigned(probe_count_)},
       {"last_fault", last},
       {"reason", w::String(error_)},
       {"role_launch_authority", w::Boolean(false)}});
}
}  // namespace q4t::service
