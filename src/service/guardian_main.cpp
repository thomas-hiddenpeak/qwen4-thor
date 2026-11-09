// Standalone CPU Guardian. The controller is the only model-launch actor.
#include "q4t/service/guardian_journal.h"
#include "q4t/service/guardian_quota.h"
#include "q4t/service/guardian_wire.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
namespace s = q4t::service;
namespace w = q4t::service::wire;
using q4t::io::Json;
constexpr int64_t kSecond = 1'000'000'000;
volatile sig_atomic_t stop_signal = 0;
void SignalHandler(int) { stop_signal = 1; }

int64_t Now() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}
void Require(bool value, const char* reason) {
  if (!value) throw std::runtime_error(reason);
}
std::string Text(const Json& object, const char* key) {
  const auto* value = object.Find(key);
  Require(value && value->IsString() && value->str.size() <= 4096,
          "missing bounded string");
  return value->str;
}
uint64_t Integer(const Json& object, const char* key) {
  uint64_t value = 0;
  Require(w::GetUnsigned(object, key, &value).ok(), "missing exact integer");
  return value;
}
std::string ReadFile(const std::string& path, bool private_file) {
  s::OwnedFd fd(open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
  Require(fd.Get() >= 0, "cannot open startup file");
  struct stat info {};
  Require(fstat(fd.Get(), &info) == 0 && S_ISREG(info.st_mode),
          "startup input must be a regular file");
  if (private_file) {
    Require(info.st_uid == geteuid() && (info.st_mode & 0777) == 0600 &&
                info.st_nlink == 1,
            "startup file is not private");
  }
  std::array<char, 65537> bytes;
  const ssize_t count = read(fd.Get(), bytes.data(), bytes.size());
  Require(count > 0 && count < static_cast<ssize_t>(bytes.size()),
          "startup file is empty, oversized or unavailable");
  return std::string(bytes.data(), count);
}
bool HostMemorySafe(uint64_t floor) {
  try {
    std::istringstream lines(ReadFile("/proc/meminfo", false));
    std::string line;
    uint64_t available = 0, total = 0;
    bool saw_available = false, saw_total = false;
    while (std::getline(lines, line)) {
      std::istringstream fields(line);
      std::string key, unit, extra;
      uint64_t value = 0;
      fields >> key;
      if (key != "MemTotal:" && key != "MemAvailable:") continue;
      if (!(fields >> value >> unit) || unit != "kB" || fields >> extra) {
        return false;
      }
      if (key == "MemAvailable:") {
        if (saw_available) return false;
        saw_available = true;
        available = value;
      } else {
        if (saw_total) return false;
        saw_total = true;
        total = value;
      }
    }
    return saw_available && saw_total && total && available <= total &&
           available / 1024 >= floor;
  } catch (const std::exception&) {
    return false;
  }
}

// Seqpacket preserves each bounded command. No caller can stall this loop by
// withholding a body or by refusing to read a response.
bool Send(int fd, const Json& message) {
  std::string data;
  if (!w::Encode(message, &data).ok()) return false;
  return send(fd, data.data(), data.size(), MSG_DONTWAIT | MSG_NOSIGNAL) ==
         static_cast<ssize_t>(data.size());
}
int Receive(int fd, Json* result) {
  std::array<char, 8192> bytes;
  iovec vector{bytes.data(), bytes.size()};
  msghdr header{};
  header.msg_iov = &vector;
  header.msg_iovlen = 1;
  const ssize_t count = recvmsg(fd, &header, MSG_DONTWAIT);
  if (count < 0 &&
      (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
    return 0;
  }
  if (count <= 0 || (header.msg_flags & (MSG_TRUNC | MSG_CTRUNC))) return -1;
  return w::Parse(std::string(bytes.data(), count), result).ok() ? 1 : -1;
}

struct Config {
  std::string directory, service_id, python, journal, lease_path, boot_id;
  std::vector<std::string> controller;
  std::vector<std::string> gateway;
  bool quota_enforced = false;
  bool usage_handoff = false;
  int lease_fd = -1;
  int64_t term_ns = 30 * kSecond, kill_ns = 5 * kSecond;
  int64_t heartbeat_ns = 15 * kSecond;
  uint64_t memory_floor_mib = 1024;
  bool daemon = false;
  s::ProcessIdentity launcher;
  int startup_fd = -1;
  int64_t startup_ns = 300 * kSecond;
};
Config ReadConfig(const std::string& path) {
  Json value;
  Require(w::Parse(ReadFile(path, true), &value).ok(),
          "invalid Guardian config");
  const auto version = Integer(value, "version");
  auto fields = value;
  if (version == 2) {
    const auto* handoff = value.Find("usage_handoff");
    Require(handoff && handoff->IsBool(), "invalid usage handoff policy");
    std::erase_if(fields.object, [](const auto& item) {
      return item.first == "usage_handoff";
    });
  }
  Require(w::HasKeys(fields,
                     {"version", "directory", "service_id", "python", "journal",
                      "controller", "lease_path", "lease_fd", "resource_mode",
                      "require_hard_memory_limit", "term_ms", "kill_ms",
                      "heartbeat_ms", "critical_memory_mib", "launch",
                      "gateway", "quota_enforced"}),
          "unknown Guardian configuration fields");
  Require((version == 1 || version == 2) &&
              Text(value, "resource_mode") == "process",
          "unsupported service mode");
  const auto* hard = value.Find("require_hard_memory_limit");
  Require(hard->IsBool() && !hard->boolean,
          "process mode cannot supply a hard physical RAM limit");
  Config config;
  config.directory = Text(value, "directory");
  config.service_id = Text(value, "service_id");
  config.python = Text(value, "python");
  config.journal = Text(value, "journal");
  config.lease_path = Text(value, "lease_path");
  const uint64_t lease = Integer(value, "lease_fd");
  Require(lease >= 3 && lease <= 1'000'000, "invalid inherited lease FD");
  config.lease_fd = static_cast<int>(lease);
  const auto* command = value.Find("controller");
  Require(command->IsArray() && !command->array.empty() &&
              command->array.size() <= 64,
          "invalid controller command");
  for (const auto& arg : command->array) {
    Require(arg.IsString() && arg.str.size() <= 4096 &&
                arg.str.find('\0') == std::string::npos,
            "invalid controller argument");
    config.controller.push_back(arg.str);
  }
  const auto* gateway = value.Find("gateway");
  Require(gateway->IsArray() && gateway->array.size() <= 64,
          "invalid gateway command");
  for (const auto& arg : gateway->array) {
    Require(arg.IsString() && arg.str.size() <= 4096 &&
                arg.str.find('\0') == std::string::npos,
            "invalid gateway argument");
    config.gateway.push_back(arg.str);
  }
  Require(config.gateway.empty() ||
              (!config.gateway[0].empty() && config.gateway[0][0] == '/'),
          "absolute gateway required");
  Require(value.Find("quota_enforced")->IsBool(), "invalid quota policy");
  config.quota_enforced = value.Find("quota_enforced")->boolean;
  config.usage_handoff = version == 2 && value.GetBool("usage_handoff");
  Require(!config.usage_handoff || config.quota_enforced,
          "usage handoff requires quota protection");
  for (const auto* absolute :
       {&config.directory, &config.python, &config.journal, &config.lease_path,
        &config.controller[0]}) {
    Require(!absolute->empty() && (*absolute)[0] == '/' &&
                absolute->find('\0') == std::string::npos,
            "absolute path required");
  }
  Require(config.service_id.size() == 36, "invalid service identity");
  const uint64_t term = Integer(value, "term_ms");
  const uint64_t kill = Integer(value, "kill_ms");
  const uint64_t heartbeat = Integer(value, "heartbeat_ms");
  Require(term <= 60'000 && kill >= 100 && kill <= 30'000 &&
              heartbeat >= 1000 && heartbeat <= 60'000,
          "invalid lifecycle deadlines");
  config.term_ns = static_cast<int64_t>(term) * 1'000'000;
  config.kill_ns = static_cast<int64_t>(kill) * 1'000'000;
  config.heartbeat_ns = static_cast<int64_t>(heartbeat) * 1'000'000;
  config.memory_floor_mib = Integer(value, "critical_memory_mib");
  Require(config.memory_floor_mib >= 64 && config.memory_floor_mib <= 65536,
          "invalid critical memory floor");
  config.boot_id = ReadFile("/proc/sys/kernel/random/boot_id", false);
  if (config.boot_id.ends_with('\n')) config.boot_id.pop_back();
  Require(config.boot_id.size() == 36, "invalid boot identity");
  const auto& launch = *value.Find("launch");
  Require(w::HasKeys(launch, {"mode", "parent_pid", "parent_start_ticks",
                              "startup_fd", "timeout_ms"}),
          "invalid launch lifecycle");
  const auto mode = Text(launch, "mode");
  Require(mode == "foreground" || mode == "daemon", "unknown launch mode");
  config.daemon = mode == "daemon";
  Require(Integer(launch, "parent_pid") > 0 &&
              Integer(launch, "parent_pid") <= 0x7fffffff,
          "invalid launcher PID");
  config.launcher = {static_cast<pid_t>(Integer(launch, "parent_pid")),
                     Integer(launch, "parent_start_ticks")};
  const auto startup = Integer(launch, "startup_fd");
  Require(config.daemon
              ? startup >= 3 && startup <= 1'000'000 && startup != lease
              : startup == 0,
          "invalid startup channel");
  config.startup_fd = config.daemon ? static_cast<int>(startup) : -1;
  const auto timeout = Integer(launch, "timeout_ms");
  Require(timeout >= 100 && timeout <= 600'000, "invalid startup deadline");
  config.startup_ns = static_cast<int64_t>(timeout) * 1'000'000;
  return config;
}

enum class Purpose {
  kNone,
  kActivate,
  kGrant,
  kSpawned,
  kReady,
  kStopIntent,
  kRetired,
  kModel,
  kModelRetired,
  kGatewayGrant,
  kGatewaySpawned,
  kGatewayReady,
  kGatewayRetired
};
struct ModelRequest {
  uint64_t sequence = 0;
  std::string operation;
  Json args;
};
struct Client {
  s::OwnedFd fd;
  int64_t accepted = 0;
  bool stopping = false;
  pid_t peer = -1;
};

class Guardian {
 public:
  Guardian(Config config, bool activate)
      : config_(std::move(config)),
        activate_(activate),
        quota_(config_.quota_enforced) {}

  void Initialize() {
    umask(0077);
    Require(s::EnableSubreaper().ok(), "subreaper required");
    s::ProcessRecord parent;
    Require(getppid() == config_.launcher.pid &&
                s::ReadProcess(config_.launcher.pid, &parent).ok() &&
                parent.identity == config_.launcher,
            "launcher identity changed");
    if (config_.daemon) {
      bootstrap_.Reset(config_.startup_fd);
      ucred peer{};
      socklen_t length = sizeof(peer);
      int type = 0;
      socklen_t type_length = sizeof(type);
      Require(getsockopt(bootstrap_.Get(), SOL_SOCKET, SO_PEERCRED, &peer,
                         &length) == 0 &&
                  peer.pid == config_.launcher.pid && peer.uid == geteuid() &&
                  getsockopt(bootstrap_.Get(), SOL_SOCKET, SO_TYPE, &type,
                             &type_length) == 0 &&
                  type == SOCK_SEQPACKET &&
                  fcntl(bootstrap_.Get(), F_SETFD, FD_CLOEXEC) == 0 &&
                  fcntl(bootstrap_.Get(), F_SETFL, O_NONBLOCK) == 0,
              "invalid daemon startup channel");
      startup_deadline_ = Now() + config_.startup_ns;
    } else {
      Require(prctl(PR_SET_PDEATHSIG, SIGTERM, 0, 0, 0) == 0 &&
                  getppid() == config_.launcher.pid &&
                  s::ReadProcess(config_.launcher.pid, &parent).ok() &&
                  parent.identity == config_.launcher,
              "foreground launcher died");
    }
    struct stat info {};
    Require(lstat(config_.directory.c_str(), &info) == 0 &&
                S_ISDIR(info.st_mode) && info.st_uid == geteuid() &&
                (info.st_mode & 0777) == 0700,
            "private service directory required");
    const std::string lock = config_.directory + "/guardian.lock";
    instance_.Reset(
        open(lock.c_str(), O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600));
    Require(instance_.Get() >= 0 && fstat(instance_.Get(), &info) == 0 &&
                S_ISREG(info.st_mode) && info.st_uid == geteuid() &&
                info.st_nlink == 1 && (info.st_mode & 0777) == 0600 &&
                flock(instance_.Get(), LOCK_EX | LOCK_NB) == 0,
            "service instance already exists or lock invalid");
    Require(s::CheckInheritedLease(config_.lease_fd, config_.lease_path).ok(),
            "GPU lease is not continuously owned");
    lease_.Reset(fcntl(config_.lease_fd, F_DUPFD_CLOEXEC, 3));
    Require(lease_.Get() >= 0, "cannot retain lease FD");
    close(config_.lease_fd);
    // A stable abstract address avoids the Unix socket path-length limit for
    // deep release directories. Every peer is authenticated by kernel UID.
    socket_path_ =
        "q4t-" + std::to_string(geteuid()) + "-" + config_.service_id;
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    Require(socket_path_.size() + 1 < sizeof(address.sun_path),
            "control path too long");
    std::memcpy(address.sun_path + 1, socket_path_.data(), socket_path_.size());
    listener_.Reset(
        socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    Require(listener_.Get() >= 0 &&
                bind(listener_.Get(), reinterpret_cast<sockaddr*>(&address),
                     static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) +
                                            1 + socket_path_.size())) == 0 &&
                listen(listener_.Get(), 8) == 0,
            "cannot bind private control socket");
    Require(s::ReadProcess(getpid(), &self_).ok(),
            "Guardian identity unavailable");
    const auto status =
        journal_.Start(config_.python, config_.journal, config_.directory,
                       config_.service_id, Now(), 5 * kSecond);
    if (!status.ok()) Freeze(status.message());
  }

  int Run() {
    for (;;) {
      pollfd descriptors[3]{{listener_.Get(), POLLIN, 0},
                            {controller_socket_.Get(), POLLIN, 0},
                            {gateway_socket_.Get(), POLLIN, 0}};
      poll(descriptors, 3, 25);
      const int64_t now = Now();
      if (stop_signal) RequestStop(now);
      Clients(now);  // Stop precedes consuming any new durable launch receipt.
      Bootstrap(now);
      ControllerEvents(now);
      GatewayEvents(now);
      const auto journal_status = journal_.Tick(now);
      if (!journal_status.ok() &&
          journal_.State() == s::JournalState::kFrozen) {
        Freeze(journal_status.message());
      }
      if (auto reply = journal_.TakeReply()) Complete(*reply, now);
      if (!initialized_ && journal_.State() == s::JournalState::kReady) {
        initialized_ = true;
        const auto& snapshot = journal_.Snapshot();
        const auto* active = snapshot.Find("active");
        Require(active && active->IsObject(), "invalid active ledger slots");
        for (const auto& [role, item] : active->object) {
          (void)role;
          if (!item.IsNull()) {
            cold_unreconciled_ = true;
            Freeze("cold startup requires slot reconciliation");
          }
        }
        if (const auto* quarantine = snapshot.Find("quarantine");
            quarantine && !quarantine->IsNull()) {
          quarantined_ = true;
          Freeze("persistent quarantine");
        }
        desired_running_ = snapshot.GetString("desired") == "RUNNING";
        model_generation_ = Integer(*snapshot.Find("generations"), "model");
        stop_durable_ =
            !stopping_ && snapshot.GetString("desired") == "STOPPED";
      }
      CheckController(now);
      CheckGateway(now);
      CheckModel();
      Drive(now);
      if (stopping_ && stop_durable_ && !cold_unreconciled_ &&
          controller_ticket_.empty() && gateway_ticket_.empty() &&
          journal_.CleanupConfirmed()) {
        bool empty = false;
        Require(s::NoChildren(&empty).ok(),
                "cannot confirm empty service tree");
        if (empty) {
          stopped_ = true;
          ReplyToStops();
          return 0;
        }
      }
    }
  }

  // Used for internal contract failures after startup. Keep the Guardian alive
  // to complete owned cleanup; never unlock underneath surviving children.
  void Fail(const std::string& reason) {
    Freeze(reason);
    RequestStop(Now());
  }

 private:
  bool BusinessReady() const {
    return model_ready_ && model_admission_open_ && quota_.Open(Now()) &&
           !stopping_ &&
           (config_.gateway.empty() ||
            (gateway_ready_ && gateway_bound_model_ == model_generation_));
  }
  Json Status() const {
    const bool observations_unknown =
        !controller_health_.IsNull() &&
        controller_health_.GetString("observation") != "AVAILABLE";
    const char* state =
        stopped_                             ? "STOPPED"
        : stopping_                          ? "DRAINING"
        : recovering_ || gateway_recovering_ ? "RECOVERING"
        : quarantined_                       ? "QUARANTINED"
        : BusinessReady()
            ? (gate_.Frozen() || observations_unknown ? "READY_DEGRADED"
                                                      : "READY")
        : !failure_.empty()    ? "WAIT_DEPENDENCY"
        : retry_after_ > Now() ? (circuit_open_ ? "CIRCUIT_OPEN" : "RECOVERING")
        : controller_ready_    ? "WAIT_DEPENDENCY"
                               : "STARTING";
    return w::Object(
        {{"version", w::Unsigned(1)},
         {"service_id", w::String(config_.service_id)},
         {"state", w::String(state)},
         {"guardian_pid", w::Unsigned(getpid())},
         {"guardian_start_ticks", w::Unsigned(self_.identity.start_ticks)},
         {"controller_pid", controller_.identity.pid > 0 && !controller_reaped_
                                ? w::Unsigned(controller_.identity.pid)
                                : Json{}},
         {"controller_generation", w::Unsigned(controller_generation_)},
         {"controller_start_ticks",
          w::Unsigned(controller_.identity.start_ticks)},
         {"controller_events", w::Unsigned(controller_event_)},
         {"controller_health", controller_health_},
         {"auxiliary_projection_errors",
          w::Unsigned(auxiliary_projection_errors_)},
         {"controller_health_age_ms",
          controller_health_.IsNull()
              ? Json{}
              : w::Unsigned((Now() - controller_health_at_) / 1'000'000)},
         {"continuous_model_recovery", w::Boolean(model_authority_seen_)},
         {"probe_used_for_generation", w::Unsigned(probe_used_generation_)},
         {"retry_after_ms", w::Unsigned(static_cast<uint64_t>(std::max<int64_t>(
                                0, (retry_after_ - Now()) / 1'000'000)))},
         {"journal_pid",
          !journal_.CleanupConfirmed() && journal_.Worker().pid > 0
              ? w::Unsigned(journal_.Worker().pid)
              : Json{}},
         {"journal_start_ticks", w::Unsigned(journal_.Worker().start_ticks)},
         {"controller_ready", w::Boolean(controller_ready_)},
         {"model_pid",
          model_.identity.pid > 0 ? w::Unsigned(model_.identity.pid) : Json{}},
         {"model_start_ticks", w::Unsigned(model_.identity.start_ticks)},
         {"model_generation", w::Unsigned(model_generation_)},
         {"model_ready", w::Boolean(model_ready_)},
         {"model_admission_open", w::Boolean(model_admission_open_)},
         {"business_ready", w::Boolean(BusinessReady())},
         {"gateway_pid", gateway_.identity.pid > 0 && !gateway_reaped_
                             ? w::Unsigned(gateway_.identity.pid)
                             : Json{}},
         {"gateway_start_ticks", w::Unsigned(gateway_.identity.start_ticks)},
         {"gateway_generation", w::Unsigned(gateway_generation_)},
         {"gateway_ready", w::Boolean(gateway_ready_)},
         {"gateway_bound_model", w::Unsigned(gateway_bound_model_)},
         {"gateway_recovering", w::Boolean(gateway_recovering_)},
         {"quota_wait_ms", w::Unsigned(static_cast<uint64_t>(std::max<int64_t>(
                               0, (quota_.NotBefore() - Now()) / 1'000'000)))},
         {"daemon_detached", w::Boolean(detached_)},
         {"recovery_frozen", w::Boolean(gate_.Frozen())},
         {"stop_requested", w::Boolean(stopping_)},
         {"stop_durable", w::Boolean(stop_durable_)},
         {"cleanup_confirmed", w::Boolean(stopped_)},
         {"reason", w::String(failure_)}});
  }
  void Freeze(const std::string& reason) {
    if (failure_.empty()) failure_ = reason;
    gate_.Freeze();
    gateway_gate_.Freeze();
  }
  void Bootstrap(int64_t now) {
    if (bootstrap_.Get() < 0) return;
    Json message;
    const int received = Receive(bootstrap_.Get(), &message);
    if (stopping_ || received < 0 || now >= startup_deadline_) {
      bootstrap_.Reset();
      RequestStop(now);
      return;
    }
    CheckModel();
    if (received == 1) {
      try {
        Require(startup_ready_sent_ && BusinessReady() && !gate_.Frozen() &&
                    w::HasKeys(message, {"version", "service_id", "op"}) &&
                    Integer(message, "version") == 1 &&
                    Text(message, "service_id") == config_.service_id &&
                    Text(message, "op") == "detach",
                "invalid startup commit");
        detached_ = true;
        Require(Send(bootstrap_.Get(), Status()),
                "startup receipt unavailable");
        bootstrap_.Reset();
      } catch (const std::exception&) {
        detached_ = false;
        bootstrap_.Reset();
        RequestStop(now);
      }
    } else if (!startup_ready_sent_ && BusinessReady() && !gate_.Frozen()) {
      if (!Send(bootstrap_.Get(), Status())) {
        bootstrap_.Reset();
        RequestStop(now);
      } else {
        startup_ready_sent_ = true;
      }
    }
  }
  void RequestStop(int64_t now) {
    if (stopping_) return;
    stop_handoff_ = config_.usage_handoff && controller_ready_ &&
                    model_ready_ && !recovering_ && !quarantined_;
    stopping_ = true;
    // A previously stopped snapshot is not proof of the current stop: an
    // activation transaction may still be committing in the journal.
    stop_durable_ = false;
    gate_.Stop();
    gateway_gate_.Stop();
    controller_ready_ = false;
    model_ready_ = false;
    cleanup_started_ = now;
  }
  bool Submit(Purpose purpose, const std::string& operation, const Json& args,
              int64_t now) {
    if (purpose_ != Purpose::kNone ||
        journal_.State() != s::JournalState::kReady) {
      return false;
    }
    const auto status =
        journal_.Submit(operation, args, now, &journal_sequence_);
    if (!status.ok()) {
      Freeze(status.message());
      return false;
    }
    purpose_ = purpose;
    return true;
  }
  void Clients(int64_t now) {
    for (int count = 0; count < 4 && clients_.size() < 8; ++count) {
      s::OwnedFd fd(accept4(listener_.Get(), nullptr, nullptr,
                            SOCK_CLOEXEC | SOCK_NONBLOCK));
      if (fd.Get() < 0) break;
      ucred peer{};
      socklen_t length = sizeof(peer);
      if (getsockopt(fd.Get(), SOL_SOCKET, SO_PEERCRED, &peer, &length) != 0 ||
          peer.uid != geteuid())
        continue;
      clients_.push_back(Client{std::move(fd), now, false, peer.pid});
    }
    for (auto it = clients_.begin(); it != clients_.end();) {
      if (it->stopping) {
        ++it;
        continue;
      }
      Json message;
      const int received = Receive(it->fd.Get(), &message);
      if (received == 0 && now - it->accepted < kSecond) {
        ++it;
        continue;
      }
      bool wait_stop = false;
      try {
        if (received == 1 &&
            w::HasKeys(message, {"version", "service_id", "op"}) &&
            Integer(message, "version") == 1 &&
            Text(message, "service_id") == config_.service_id) {
          const auto operation = Text(message, "op");
          if (operation == "status") {
            CheckModel();
            Send(it->fd.Get(), Status());
          } else if (operation == "binding" && !gateway_reaped_ &&
                     it->peer == gateway_.identity.pid) {
            CheckModel();
            Json result = Status();
            result.object.emplace_back("binding",
                                       model_ready_ ? model_binding_ : Json{});
            result.object.emplace_back(
                "model_owner",
                w::Object(
                    {{"pid",
                      w::Unsigned(std::max<pid_t>(0, model_.identity.pid))},
                     {"start_ticks", w::Unsigned(model_.identity.start_ticks)},
                     {"boot_id", w::String(config_.boot_id)}}));
            Send(it->fd.Get(), result);
          } else if (operation == "stop") {
            RequestStop(now);
            wait_stop = true;
          }
        }
      } catch (const std::exception&) {
        // A malformed local request does not alter lifecycle or model health.
      }
      if (wait_stop) {
        it->stopping = true;
        ++it;
      } else
        it = clients_.erase(it);
    }
  }
  void ReplyToStops() {
    for (auto& client : clients_)
      if (client.stopping) Send(client.fd.Get(), Status());
    clients_.clear();
  }
  void ControllerEvents(int64_t now) {
    if (controller_socket_.Get() < 0 || recovering_) return;
    for (int count = 0; count < 4; ++count) {
      Json message;
      const int received = Receive(controller_socket_.Get(), &message);
      if (received == 0) return;
      if (received < 0) {
        Recover(now);
        return;
      }
      try {
        const auto event = Text(message, "event");
        Require(
            event == "transaction" || event == "health" ||
                    event == "admission" || event == "quota_restored"
                ? w::HasKeys(message, {"version", "service_id", "generation",
                                       "sequence", "event", event.c_str()})
                : w::HasKeys(message, {"version", "service_id", "generation",
                                       "sequence", "event"}),
            "invalid controller message");
        Require(Integer(message, "version") == 1 &&
                    Text(message, "service_id") == config_.service_id &&
                    Integer(message, "generation") == controller_generation_ &&
                    Integer(message, "sequence") == controller_event_ + 1,
                "stale controller message");
        ++controller_event_;
        Require(event == "ready" || event == "heartbeat" ||
                    event == "transaction" || event == "health" ||
                    event == "admission" || event == "quota_restored",
                "unknown controller event");
        if (event == "ready") controller_ready_seen_ = true;
        if (event == "quota_restored") {
          // A malformed optional recovery receipt cannot restart a healthy
          // model. Ignore it and retain the conservative quota window.
          try {
            const auto& value = *message.Find("quota_restored");
            Require(w::HasKeys(value, {"model_generation", "source_generation",
                                       "boot_id", "receipt_sha256"}) &&
                        config_.usage_handoff && !stopping_ && !recovering_ &&
                        model_registered_ && !model_ready_committed_ &&
                        !model_binding_.IsNull() &&
                        Text(value, "boot_id") == config_.boot_id,
                    "invalid quota recovery context");
            const auto receipt = Text(value, "receipt_sha256");
            Require(receipt.size() == 64 &&
                        receipt.find_first_not_of("0123456789abcdef") ==
                            std::string::npos,
                    "invalid quota recovery receipt");
            bool exited = true;
            if (model_.pidfd.Get() >= 0 &&
                s::ProcessExited(model_.pidfd.Get(), &exited).ok() && !exited)
              quota_.Restored(Integer(value, "model_generation"),
                              Integer(value, "source_generation"));
          } catch (const std::exception&) {
          }
        }
        if (event == "admission") {
          const auto& value = *message.Find("admission");
          Require(w::HasKeys(value, {"model_generation", "opened"}) &&
                      value.Find("opened")->IsBool(),
                  "invalid admission projection");
          if (Integer(value, "model_generation") == model_generation_ &&
              model_generation_ > 0) {
            const bool opened = value.GetBool("opened");
            CheckModel();
            if (!opened || (model_ready_ && !stopping_ && !recovering_))
              model_admission_open_ = opened;
          }
        }
        if (event == "health") {
          auto health = *message.Find("health");
          const bool has_auxiliary = health.Has("auxiliary_io");
          Require(has_auxiliary
                      ? w::HasKeys(health, {"phase", "observation", "reason",
                                            "auxiliary_io"})
                      : w::HasKeys(health, {"phase", "observation", "reason"}),
                  "invalid controller health projection");
          bool auxiliary_valid = true;
          try {
            if (has_auxiliary) {
              const auto& io = *health.Find("auxiliary_io");
              Require(w::HasKeys(io, {"failed_operations", "dropped_bytes",
                                      "acknowledged_operations", "queued_bytes",
                                      "queued_records", "pending_records",
                                      "worker_pid", "worker_unreaped",
                                      "worker_state"}),
                      "invalid observation summary");
              for (const char* key : {"failed_operations", "dropped_bytes",
                                      "acknowledged_operations"}) {
                (void)Integer(io, key);
              }
              const auto state = Text(io, "worker_state");
              Require(Integer(io, "queued_bytes") <= 128 * 1024 &&
                          Integer(io, "queued_records") <= 32 &&
                          Integer(io, "pending_records") <= 1 &&
                          Integer(io, "worker_pid") < (uint64_t{1} << 31) &&
                          io.Find("worker_unreaped")->IsBool() &&
                          (state == "ABSENT" || state == "STARTING" ||
                           state == "AVAILABLE" || state == "BACKOFF" ||
                           state == "DISABLED"),
                      "invalid bounded observation fields");
            }
          } catch (const std::exception&) {
            // An optional observation projection is not lifecycle authority.
            // Keep core health and the owned model; report lost diagnostics.
            auxiliary_valid = false;
            if (auxiliary_projection_errors_ < 9007199254740991ULL)
              ++auxiliary_projection_errors_;
          }
          const auto phase = Text(health, "phase");
          const auto observation = Text(health, "observation");
          const auto reason = Text(health, "reason");
          Require((phase == "STARTING" || phase == "READY" ||
                   phase == "RECOVERING" || phase == "WAIT_DEPENDENCY" ||
                   phase == "CIRCUIT_OPEN" || phase == "QUARANTINED") &&
                      (observation == "AVAILABLE" || observation == "UNKNOWN" ||
                       observation == "DEVICE_FAILURE") &&
                      reason.size() <= 64 &&
                      reason.find_first_not_of(
                          "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                          "0123456789_") == std::string::npos,
                  "invalid controller health fields");
          if (!auxiliary_valid) {
            health = w::Object(
                {{"phase", w::String(phase)},
                 {"observation",
                  w::String(observation == "DEVICE_FAILURE" ? observation
                                                            : "UNKNOWN")},
                 {"reason", w::String(observation == "DEVICE_FAILURE"
                                          ? reason
                                          : "auxiliary_io_invalid")}});
          }
          controller_health_ = health;
          controller_health_at_ = now;
        }
        if (event == "transaction") {
          Require(!model_request_, "multiple controller transactions");
          const auto& request = *message.Find("transaction");
          Require(w::HasKeys(request, {"operation", "args"}) &&
                      request.Find("args")->IsObject(),
                  "invalid model transaction");
          model_request_ =
              ModelRequest{controller_event_, Text(request, "operation"),
                           *request.Find("args")};
          if (stopping_) ReplyModel(false, "operator_stop");
        }
        heartbeat_at_ = now;
      } catch (const std::exception&) {
        Recover(now);
        return;
      }
    }
  }
  void Recover(int64_t now) {
    if (recovering_) return;
    recovering_ = true;
    controller_ready_ = false;
    model_ready_ = false;
    controller_health_ = Json{};
    controller_socket_.Reset();
    cleanup_started_ = now;
  }
  void CheckModel() {
    if (!model_ready_ || model_.pidfd.Get() < 0) return;
    bool exited = false;
    if (!s::ProcessExited(model_.pidfd.Get(), &exited).ok()) {
      Freeze("model exit observation unavailable");
      model_ready_ = false;
    } else if (exited) {
      model_ready_ = false;
      gateway_bound_model_ = 0;
    }
  }

  std::set<pid_t> Subtree(const std::vector<s::ProcessRecord>& records,
                          const s::ProcessIdentity& root) const {
    std::set<pid_t> result;
    for (const auto& record : records)
      if (record.identity == root) result.insert(root.pid);
    bool changed = true;
    while (changed) {
      changed = false;
      for (const auto& record : records)
        if (result.contains(record.parent) &&
            result.insert(record.identity.pid).second)
          changed = true;
    }
    return result;
  }

  // A replacement controller/model cannot rely only on the reported main
  // model PID disappearing. Every descendant in the model/auxiliary domain
  // must have exited; workers are stopped and reaped before requesting this.
  bool ModelTreeEmpty() {
    std::vector<s::ProcessRecord> records;
    if (!s::OwnDescendants(&records).ok()) return false;
    const auto gateway = Subtree(records, gateway_.identity);
    return std::all_of(records.begin(), records.end(), [&](const auto& item) {
      return item.identity == journal_.Worker() ||
             item.identity == controller_.identity ||
             gateway.contains(item.identity.pid);
    });
  }

  void ReplyModel(bool ok, const Json& result) {
    if (!model_request_) return;
    Send(controller_socket_.Get(),
         w::Object({{"version", w::Unsigned(1)},
                    {"service_id", w::String(config_.service_id)},
                    {"generation", w::Unsigned(controller_generation_)},
                    {"reply_to", w::Unsigned(model_request_->sequence)},
                    {"ok", w::Boolean(ok)},
                    {"result", result}}));
    model_request_.reset();
  }
  void ReplyModel(bool ok, const char* reason) {
    ReplyModel(ok, w::Object({{"reason", w::String(reason)}}));
  }

  void ValidateModelRequest(int64_t now) {
    const auto& request = *model_request_;
    const auto& args = request.args;
    const auto& operation = request.operation;
    if (operation == "authorize" || operation == "probe") {
      Require(
          w::HasKeys(args, {"role", "proof"}) && Text(args, "role") == "model",
          "invalid model grant request");
      const auto* proof = args.Find("proof");
      Require(proof && w::HasKeys(*proof, {"cleanup_complete", "resources_safe",
                                           "identity_valid", "lease_continuous",
                                           "device_usable"}),
              "invalid model proof");
      for (const auto& [key, value] : proof->object) {
        (void)key;
        Require(value.IsBool(), "model proof must be boolean");
      }
    } else if (operation == "bind") {
      Require(w::HasKeys(args, {"role", "ticket", "binding"}) &&
                  Text(args, "role") == "model" &&
                  Text(args, "ticket") == model_ticket_ && model_registered_ &&
                  model_binding_.IsNull() && !model_ready_committed_,
              "invalid binding transaction");
      const auto& binding = *args.Find("binding");
      Require(w::HasKeys(binding, {"port", "operations_port", "gateway_token",
                                   "client_policy_sha256", "binary_sha256",
                                   "protocol_sha256"}),
              "invalid model binding");
      for (const char* name : {"port", "operations_port"})
        Require(
            Integer(binding, name) >= 1024 && Integer(binding, name) <= 65535,
            "invalid binding port");
      Require(Integer(binding, "port") != Integer(binding, "operations_port"),
              "duplicate binding port");
      for (const char* name : {"gateway_token", "client_policy_sha256",
                               "binary_sha256", "protocol_sha256"}) {
        const auto value = Text(binding, name);
        Require(
            value.size() == 64 && value.find_first_not_of("0123456789abcdef") ==
                                      std::string::npos,
            "invalid binding fingerprint");
      }
    } else if (operation == "spawned" || operation == "ready" ||
               operation == "retired" || operation == "stable") {
      Require(w::HasKeys(args, {"role", "ticket",
                                operation == "spawned"  ? "owner"
                                : operation == "stable" ? "successful_request"
                                                        : "verified"}) &&
                  Text(args, "role") == "model" && !model_ticket_.empty() &&
                  Text(args, "ticket") == model_ticket_,
              "invalid model ticket");
      if (operation == "spawned") {
        Require(model_.pidfd.Get() < 0, "model grant already consumed");
        const auto& owner = *args.Find("owner");
        Require(w::HasKeys(owner, {"pid", "start_ticks", "boot_id"}) &&
                    Text(owner, "boot_id") == config_.boot_id &&
                    Integer(owner, "pid") > 0 &&
                    Integer(owner, "pid") <= 0x7fffffff,
                "invalid model owner");
        s::ProcessIdentity expected{static_cast<pid_t>(Integer(owner, "pid")),
                                    Integer(owner, "start_ticks")};
        s::ProcessRecord process;
        Require(s::OpenProcess(expected, &model_.pidfd).ok() &&
                    s::ReadProcess(expected.pid, &process).ok() &&
                    process.identity == expected &&
                    process.parent == controller_.identity.pid &&
                    process.state != 'Z' && process.state != 'X',
                "model is not a live child of the current controller");
        model_.identity = expected;
      } else {
        const auto* verified = args.Find(
            operation == "stable" ? "successful_request" : "verified");
        Require(verified->IsBool() && verified->boolean,
                "positive model verification required");
        if (operation == "ready") {
          Require(model_registered_ && !model_ready_committed_ &&
                      model_.pidfd.Get() >= 0,
                  "model is not newly registered");
          bool exited = false;
          Require(s::ProcessExited(model_.pidfd.Get(), &exited).ok() && !exited,
                  "model exited before readiness");
          Require(config_.gateway.empty() || !model_binding_.IsNull(),
                  "missing immutable model binding");
        }
        if (operation == "stable")
          Require(model_ready_ && now - model_ready_at_ >= 600 * kSecond,
                  "model stability window incomplete");
      }
    } else if (operation == "fault") {
      Require(w::HasKeys(args, {"generation", "fingerprint", "unknown"}) &&
                  Integer(args, "generation") == model_generation_ &&
                  model_generation_ > 0 && args.Find("unknown")->IsBool(),
              "invalid model fault");
      const auto fingerprint = Text(args, "fingerprint");
      Require(fingerprint.size() == 64 &&
                  fingerprint.find_first_not_of("0123456789abcdef") ==
                      std::string::npos,
              "invalid fault fingerprint");
      model_ready_ = false;
    } else if (operation == "quarantine") {
      Require(w::HasKeys(args, {"reason"}), "invalid quarantine request");
      const auto reason = Text(args, "reason");
      Require(reason == "device_failure" || reason == "identity_invalid" ||
                  reason == "cleanup_failed" ||
                  reason == "reconciliation_required",
              "invalid quarantine");
      model_ready_ = false;
    } else {
      Require(false, "controller cannot perform this ledger operation");
    }
  }

  void DriveModel(int64_t now) {
    if (!model_request_ || purpose_ != Purpose::kNone) return;
    if (journal_.State() == s::JournalState::kFrozen) {
      ReplyModel(false, "ledger_unavailable");
      return;
    }
    if (journal_.State() != s::JournalState::kReady || !controller_ready_)
      return;
    try {
      ValidateModelRequest(now);
      const auto& operation = model_request_->operation;
      if (operation == "authorize" || operation == "probe") {
        if (gate_.Frozen()) {
          ReplyModel(false, "recovery_frozen");
          return;
        }
        if (!model_ticket_.empty() || !ModelTreeEmpty() ||
            !s::CheckInheritedLease(lease_.Get(), config_.lease_path).ok() ||
            !HostMemorySafe(config_.memory_floor_mib)) {
          ReplyModel(false, "replacement_preflight_incomplete");
          return;
        }
      } else if (operation == "retired" && !ModelTreeEmpty()) {
        ReplyModel(false, "model_cleanup_unconfirmed");
        return;
      }
      if (operation == "probe") {
        const auto& proof = *model_request_->args.Find("proof");
        bool valid = !proof.GetBool("device_usable");
        for (const auto& [key, value] : proof.object)
          if (key != "device_usable") valid = valid && value.boolean;
        if (!valid || !model_authority_seen_ || model_generation_ == 0 ||
            probe_used_generation_ == model_generation_) {
          ReplyModel(false, "probe_not_authorized");
        } else {
          // Root-local authorization survives controller death. A new Root is
          // cold and cannot grant any NVML-free probe until a model was
          // authorized under its own continuously retained lease.
          probe_used_generation_ = model_generation_;
          ReplyModel(
              true,
              w::Object({{"allowed", w::Boolean(true)},
                         {"generation", w::Unsigned(model_generation_ + 1)}}));
        }
        return;
      }
      if (operation == "authorize" && !config_.gateway.empty() &&
          !gateway_ready_) {
        ReplyModel(false, "gateway_unavailable");
        return;
      }
      if (operation == "ready" && !quota_.Open(now)) {
        ReplyModel(false, "quota_window");
        return;
      }
      if (operation == "bind") {
        model_binding_ = *model_request_->args.Find("binding");
        ReplyModel(true, w::Object({{"stored", w::Boolean(true)}}));
        return;
      }
      Submit(Purpose::kModel, operation, model_request_->args, now);
    } catch (const std::exception&) {
      ReplyModel(false, "invalid_model_transaction");
      Recover(now);
    }
  }

  void CompleteModel(const s::JournalReply& reply, int64_t now) {
    Require(model_request_.has_value(), "unbound model transaction");
    const auto& operation = model_request_->operation;
    if (operation == "authorize" && reply.result.GetBool("allowed")) {
      const auto* grant = reply.result.Find("grant");
      Require(grant && grant->IsObject(), "missing model grant");
      model_ticket_ = Text(*grant, "ticket");
      model_generation_ = Integer(*grant, "generation");
      model_authority_seen_ = true;
      quota_.Granted(model_generation_, now);
    } else if (operation == "spawned") {
      model_registered_ = true;
    } else if (operation == "ready") {
      model_ready_ = !stopping_ && !recovering_;
      model_admission_open_ = model_ready_;
      model_ready_committed_ = true;
      model_ready_at_ = now;
    } else if (operation == "retired") {
      RetireModel();
    }
    if (const auto* quarantine = reply.result.Find("quarantine");
        quarantine && !quarantine->IsNull()) {
      quarantined_ = true;
      Freeze("persistent model quarantine");
      model_ready_ = false;
    }
    if (stopping_ || recovering_)
      model_request_.reset();
    else {
      auto result = reply.result;
      if (operation == "authorize" && reply.result.GetBool("allowed"))
        result.object.emplace_back("quota_not_before_monotonic_ns",
                                   w::Unsigned(quota_.NotBefore()));
      ReplyModel(true, result);
    }
  }

  void RetireModel() {
    quota_.Retired(Now());
    model_binding_ = Json{};
    gateway_bound_model_ = 0;
    model_ticket_.clear();
    model_ = s::ChildProcess{};
    model_registered_ = false;
    model_ready_ = false;
    model_ready_committed_ = false;
    model_admission_open_ = false;
  }
  void CheckController(int64_t now) {
    if (!controller_reaped_ && controller_.pidfd.Get() >= 0) {
      int code = 0;
      const auto status =
          s::ReapProcess(controller_.pidfd.Get(), &controller_reaped_, &code);
      if (!status.ok()) Freeze(status.message());
      if (controller_reaped_ && !stopping_) Recover(now);
    }
    if (!stopping_ && !recovering_ && controller_.identity.pid > 0 &&
        !controller_reaped_ &&
        now - heartbeat_at_ >
            (controller_ready_ ? config_.heartbeat_ns : 300 * kSecond))
      Recover(now);
  }
  bool CleanupScope(int64_t now, int64_t started,
                    const s::ProcessIdentity& protected_root) {
    std::vector<s::ProcessRecord> descendants;
    const auto status = s::OwnDescendants(&descendants);
    if (!status.ok()) {
      Freeze(status.message());
      return false;
    }
    const auto protected_tree = Subtree(descendants, protected_root);
    bool empty = true;
    for (const auto& record : descendants) {
      if (record.identity == journal_.Worker() ||
          protected_tree.contains(record.identity.pid))
        continue;
      empty = false;
      s::OwnedFd pidfd;
      if (!s::OpenProcess(record.identity, &pidfd).ok()) continue;
      const bool kill = now - started >= config_.term_ns;
      auto& previous =
          signalled_[{record.identity.pid, record.identity.start_ticks}];
      const int requested = kill ? SIGKILL : SIGTERM;
      if (previous != requested) {
        if (!s::SignalProcess(pidfd.Get(), requested).ok())
          Freeze("owned signal failed");
        previous = requested;
      }
      if (record.identity != controller_.identity &&
          record.identity != gateway_.identity) {
        bool reaped = false;
        int code = 0;
        s::ReapProcess(pidfd.Get(), &reaped, &code);
      }
    }
    if (!empty && now - started > config_.term_ns + config_.kill_ns) {
      Freeze("owned process cleanup unconfirmed");
      if (stopping_) ReplyToStops();
    }
    return empty;
  }
  bool CleanupController(int64_t now) {
    if (stopping_ && stop_handoff_ && controller_.pidfd.Get() >= 0) {
      quota_handoff_grace_.Start(now, cleanup_started_, config_.term_ns);
      bool exited = true;
      if (quota_handoff_grace_.Waiting(now) &&
          s::ProcessExited(controller_.pidfd.Get(), &exited).ok() && !exited) {
        auto& sent = signalled_[{controller_.identity.pid,
                                 controller_.identity.start_ticks}];
        if (sent != SIGTERM) {
          if (!s::SignalProcess(controller_.pidfd.Get(), SIGTERM).ok())
            Freeze("owned controller stop signal failed");
          sent = SIGTERM;
        }
        return false;
      }
    }
    return CleanupScope(now, cleanup_started_,
                        stopping_ ? s::ProcessIdentity{} : gateway_.identity);
  }
  void SpawnController(int64_t now) {
    if (stopping_ || stop_signal ||
        !gate_.Consume(journal_sequence_, controller_generation_))
      return;
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) != 0) {
      Recover(now);
      return;
    }
    controller_socket_.Reset(pair[0]);
    s::OwnedFd child(pair[1]);
    if (fcntl(controller_socket_.Get(), F_SETFL, O_NONBLOCK) != 0) {
      Recover(now);
      return;
    }
    controller_ = s::ChildProcess{};
    controller_reaped_ = false;
    controller_registered_ = false;
    controller_ready_seen_ = false;
    controller_event_ = 0;
    const std::vector<std::string> env{
        "Q4T_SERVICE_ID=" + config_.service_id,
        "Q4T_CONTROLLER_GENERATION=" + std::to_string(controller_generation_),
        "Q4T_CONTROL_FD=" + std::to_string(child.Get()),
        "Q4T_LEASE_FD=" + std::to_string(lease_.Get()),
        "Q4T_LEASE_PATH=" + config_.lease_path,
        std::string("Q4T_CONTINUOUS_MODEL_RECOVERY=") +
            (model_authority_seen_ ? "1" : "0"),
        "Q4T_GUARDIAN_PID=" + std::to_string(getpid()),
        "Q4T_GUARDIAN_START=" + std::to_string(self_.identity.start_ticks)};
    const auto status =
        s::SpawnProcess(config_.controller, env, {child.Get(), lease_.Get()},
                        &controller_, true);
    if (!status.ok()) {
      Recover(now);
      return;
    }
    heartbeat_at_ = now;
  }

  void RecoverGateway(int64_t now) {
    if (gateway_recovering_) return;
    gateway_recovering_ = true;
    gateway_ready_ = false;
    gateway_bound_model_ = 0;
    gateway_socket_.Reset();
    gateway_cleanup_started_ = now;
  }

  void CheckGateway(int64_t now) {
    if (!gateway_reaped_ && gateway_.pidfd.Get() >= 0) {
      int code = 0;
      const auto result =
          s::ReapProcess(gateway_.pidfd.Get(), &gateway_reaped_, &code);
      if (!result.ok()) Freeze(result.message());
      if (gateway_reaped_ && !stopping_) RecoverGateway(now);
    }
    if (!stopping_ && !gateway_recovering_ && gateway_.identity.pid > 0 &&
        !gateway_reaped_ &&
        now - gateway_heartbeat_at_ >
            (gateway_ready_ ? config_.heartbeat_ns : 300 * kSecond))
      RecoverGateway(now);
  }

  void GatewayEvents(int64_t now) {
    if (gateway_socket_.Get() < 0 || gateway_recovering_) return;
    for (int count = 0; count < 4; ++count) {
      Json message;
      const int received = Receive(gateway_socket_.Get(), &message);
      if (received == 0) return;
      if (received < 0) {
        RecoverGateway(now);
        return;
      }
      try {
        const auto event = Text(message, "event");
        Require(
            event == "bound"
                ? w::HasKeys(message, {"version", "service_id", "generation",
                                       "sequence", "event", "model_generation"})
                : w::HasKeys(message, {"version", "service_id", "generation",
                                       "sequence", "event"}),
            "invalid gateway event");
        Require(Integer(message, "version") == 1 &&
                    Text(message, "service_id") == config_.service_id &&
                    Integer(message, "generation") == gateway_generation_ &&
                    Integer(message, "sequence") == gateway_event_ + 1,
                "stale gateway identity");
        Require(event == "ready" || event == "heartbeat" || event == "bound" ||
                    event == "unbound",
                "unknown gateway event");
        ++gateway_event_;
        gateway_heartbeat_at_ = now;
        if (event == "ready") gateway_ready_seen_ = true;
        if (event == "unbound") gateway_bound_model_ = 0;
        if (event == "bound" && model_ready_ && !model_binding_.IsNull() &&
            Integer(message, "model_generation") == model_generation_)
          gateway_bound_model_ = model_generation_;
      } catch (const std::exception&) {
        RecoverGateway(now);
        return;
      }
    }
  }

  void SpawnGateway(int64_t now) {
    if (stopping_ || stop_signal ||
        !gateway_gate_.Consume(journal_sequence_, gateway_generation_))
      return;
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) != 0) {
      RecoverGateway(now);
      return;
    }
    gateway_socket_.Reset(pair[0]);
    s::OwnedFd child(pair[1]);
    if (fcntl(gateway_socket_.Get(), F_SETFL, O_NONBLOCK) != 0) {
      RecoverGateway(now);
      return;
    }
    gateway_ = s::ChildProcess{};
    gateway_reaped_ = false;
    gateway_registered_ = false;
    gateway_ready_seen_ = false;
    gateway_event_ = 0;
    const std::vector<std::string> env{
        "Q4T_SERVICE_ID=" + config_.service_id,
        "Q4T_GATEWAY_GENERATION=" + std::to_string(gateway_generation_),
        "Q4T_CONTROL_FD=" + std::to_string(child.Get()),
        "Q4T_GUARDIAN_PID=" + std::to_string(getpid()),
        "Q4T_GUARDIAN_START=" + std::to_string(self_.identity.start_ticks)};
    if (!s::SpawnProcess(config_.gateway, env, {child.Get()}, &gateway_, true)
             .ok()) {
      RecoverGateway(now);
      return;
    }
    gateway_heartbeat_at_ = now;
  }

  void CompleteGateway(Purpose purpose, const s::JournalReply& reply,
                       int64_t now) {
    if (purpose == Purpose::kGatewayGrant) {
      if (!reply.result.GetBool("allowed")) {
        const auto* delay = reply.result.Find("retry_after_ns");
        if (!delay) {
          Freeze("gateway authorization denied");
          return;
        }
        const auto duration = Integer(reply.result, "retry_after_ns");
        Require(duration <= 24 * 60 * 60 * static_cast<uint64_t>(kSecond),
                "invalid gateway recovery wait");
        gateway_retry_at_ = now + static_cast<int64_t>(duration);
        return;
      }
      const auto* grant = reply.result.Find("grant");
      Require(grant && grant->IsObject(), "missing gateway grant");
      gateway_ticket_ = Text(*grant, "ticket");
      gateway_generation_ = Integer(*grant, "generation");
      if (gateway_gate_.Begin(reply.sequence, gateway_generation_))
        SpawnGateway(now);
    } else if (purpose == Purpose::kGatewaySpawned) {
      gateway_registered_ = true;
    } else if (purpose == Purpose::kGatewayReady) {
      if (!stopping_ && !gateway_recovering_) gateway_ready_ = true;
    } else if (purpose == Purpose::kGatewayRetired) {
      gateway_ticket_.clear();
      gateway_ = s::ChildProcess{};
      gateway_reaped_ = true;
      gateway_registered_ = false;
      gateway_ready_seen_ = gateway_ready_ = false;
      gateway_recovering_ = false;
      gateway_bound_model_ = 0;
      gateway_retry_at_ = now + 5 * kSecond;
      if (!recovering_) signalled_.clear();
    } else {
      Require(false, "invalid gateway completion");
    }
  }

  void DriveGateway(int64_t now) {
    if (config_.gateway.empty()) return;
    if (gateway_recovering_) {
      if (CleanupScope(now, gateway_cleanup_started_, controller_.identity) &&
          !gateway_ticket_.empty())
        Submit(Purpose::kGatewayRetired, "retired",
               w::Object({{"role", w::String("gateway")},
                          {"ticket", w::String(gateway_ticket_)},
                          {"verified", w::Boolean(true)}}),
               now);
      return;
    }
    if (!initialized_ || !desired_running_ || gate_.Frozen()) return;
    if (gateway_.identity.pid > 0 && !gateway_reaped_) {
      if (!gateway_registered_) {
        Submit(
            Purpose::kGatewaySpawned, "spawned",
            w::Object({{"role", w::String("gateway")},
                       {"ticket", w::String(gateway_ticket_)},
                       {"owner",
                        w::Object({{"pid", w::Unsigned(gateway_.identity.pid)},
                                   {"start_ticks",
                                    w::Unsigned(gateway_.identity.start_ticks)},
                                   {"boot_id", w::String(config_.boot_id)}})}}),
            now);
      } else if (gateway_ready_seen_ && !gateway_ready_) {
        Submit(Purpose::kGatewayReady, "ready",
               w::Object({{"role", w::String("gateway")},
                          {"ticket", w::String(gateway_ticket_)},
                          {"verified", w::Boolean(true)}}),
               now);
      }
      return;
    }
    if (!gateway_ticket_.empty() || now < gateway_retry_at_) return;
    const auto proof = w::Object({{"cleanup_complete", w::Boolean(true)},
                                  {"identity_valid", w::Boolean(true)},
                                  {"resources_safe", w::Boolean(false)},
                                  {"lease_continuous", w::Boolean(false)},
                                  {"device_usable", w::Boolean(false)}});
    Submit(Purpose::kGatewayGrant, "authorize",
           w::Object({{"role", w::String("gateway")}, {"proof", proof}}), now);
  }
  void Complete(const s::JournalReply& reply, int64_t now) {
    const Purpose purpose = purpose_;
    purpose_ = Purpose::kNone;
    Require(reply.sequence == journal_sequence_, "unbound journal completion");
    if (purpose == Purpose::kActivate) {
      desired_running_ = true;
      stop_durable_ = false;
      activate_ = false;
    } else if (purpose == Purpose::kStopIntent) {
      stop_durable_ = true;
      desired_running_ = false;
    } else if (purpose == Purpose::kGrant) {
      if (!reply.result.GetBool("allowed")) {
        const auto* delay = reply.result.Find("retry_after_ns");
        if (delay) {
          const uint64_t duration = Integer(reply.result, "retry_after_ns");
          Require(duration <= 24 * 60 * 60 * static_cast<uint64_t>(kSecond),
                  "invalid recovery wait");
          retry_after_ = now + static_cast<int64_t>(duration);
          circuit_open_ = true;
        } else
          Freeze("controller authorization denied");
        return;
      }
      const auto* grant = reply.result.Find("grant");
      Require(grant && grant->IsObject(), "missing durable controller grant");
      controller_ticket_ = Text(*grant, "ticket");
      controller_generation_ = Integer(*grant, "generation");
      circuit_open_ = false;
      if (gate_.Begin(reply.sequence, controller_generation_))
        SpawnController(now);
    } else if (purpose == Purpose::kSpawned) {
      controller_registered_ = true;
    } else if (purpose == Purpose::kReady) {
      if (!stopping_ && !recovering_) controller_ready_ = true;
    } else if (purpose == Purpose::kRetired) {
      controller_ticket_.clear();
      controller_ = s::ChildProcess{};
      controller_reaped_ = true;
      controller_registered_ = false;
      controller_ready_seen_ = false;
      recovering_ = false;
      if (!gateway_recovering_) signalled_.clear();
      retry_after_ = now + 5 * kSecond;
      circuit_open_ = false;
    } else if (purpose == Purpose::kModel) {
      CompleteModel(reply, now);
    } else if (purpose == Purpose::kModelRetired) {
      RetireModel();
    } else if (purpose == Purpose::kGatewayGrant ||
               purpose == Purpose::kGatewaySpawned ||
               purpose == Purpose::kGatewayReady ||
               purpose == Purpose::kGatewayRetired) {
      CompleteGateway(purpose, reply, now);
    } else
      Require(false, "unexpected journal purpose");
  }
  void Drive(int64_t now) {
    if (stopping_) {
      if (!stop_durable_ && initialized_)
        Submit(Purpose::kStopIntent, "desired",
               w::Object({{"state", w::String("STOPPED")}}), now);
      // Durable stop is preferred; journal failure cannot prevent owned
      // cleanup.
      const auto state = journal_.State();
      if (!stop_durable_ && state != s::JournalState::kFrozen &&
          state != s::JournalState::kStopping &&
          state != s::JournalState::kStopped)
        return;
    }
    if (stopping_ || recovering_) {
      if (purpose_ != Purpose::kModel) model_request_.reset();
      if (!CleanupController(now)) return;
      if (journal_.State() == s::JournalState::kFrozen) {
        // Keep the outer Guardian and instance lock alive. Exiting with an
        // undurable stop could let an external supervisor resurrect the old
        // RUNNING intent. No model/controller replacement is authorized here.
        journal_.BeginStop(now, config_.term_ns);
        return;
      }
      if (journal_.State() == s::JournalState::kStopping ||
          journal_.State() == s::JournalState::kStopped) {
        if (journal_.CleanupConfirmed() && stopping_ &&
            (!stop_durable_ || cold_unreconciled_)) {
          ReplyToStops();
        }
        return;
      }
      if (!model_ticket_.empty()) {
        Submit(Purpose::kModelRetired, "retired",
               w::Object({{"role", w::String("model")},
                          {"ticket", w::String(model_ticket_)},
                          {"verified", w::Boolean(true)}}),
               now);
      } else if (!controller_ticket_.empty()) {
        Submit(Purpose::kRetired, "retired",
               w::Object({{"role", w::String("controller")},
                          {"ticket", w::String(controller_ticket_)},
                          {"verified", w::Boolean(true)}}),
               now);
      } else if (stopping_ && !gateway_ticket_.empty()) {
        Submit(Purpose::kGatewayRetired, "retired",
               w::Object({{"role", w::String("gateway")},
                          {"ticket", w::String(gateway_ticket_)},
                          {"verified", w::Boolean(true)}}),
               now);
      } else if (stopping_) {
        journal_.BeginStop(now, config_.term_ns);
        if (!stop_durable_ && journal_.CleanupConfirmed()) ReplyToStops();
      }
      return;
    }
    DriveGateway(now);
    DriveModel(now);
    if (!initialized_ || gate_.Frozen()) return;
    if (activate_) {
      Submit(Purpose::kActivate, "desired",
             w::Object({{"state", w::String("RUNNING")}}), now);
      return;
    }
    if (!desired_running_) return;
    if (controller_.identity.pid > 0 && !controller_reaped_) {
      if (!controller_registered_) {
        const auto owner = w::Object(
            {{"pid", w::Unsigned(controller_.identity.pid)},
             {"start_ticks", w::Unsigned(controller_.identity.start_ticks)},
             {"boot_id", w::String(config_.boot_id)}});
        Submit(Purpose::kSpawned, "spawned",
               w::Object({{"role", w::String("controller")},
                          {"ticket", w::String(controller_ticket_)},
                          {"owner", owner}}),
               now);
      } else if (controller_ready_seen_ && !controller_ready_) {
        Submit(Purpose::kReady, "ready",
               w::Object({{"role", w::String("controller")},
                          {"ticket", w::String(controller_ticket_)},
                          {"verified", w::Boolean(true)}}),
               now);
      }
      return;
    }
    if (!controller_ticket_.empty() || now < retry_after_) return;
    if (!config_.gateway.empty() && !gateway_ready_) return;
    if (!s::CheckInheritedLease(lease_.Get(), config_.lease_path).ok()) {
      Freeze("GPU lease continuity lost");
      return;
    }
    if (!HostMemorySafe(config_.memory_floor_mib)) return;
    const auto proof = w::Object({{"cleanup_complete", w::Boolean(true)},
                                  {"resources_safe", w::Boolean(true)},
                                  {"identity_valid", w::Boolean(true)},
                                  {"lease_continuous", w::Boolean(true)},
                                  {"device_usable", w::Boolean(false)}});
    Submit(Purpose::kGrant, "authorize",
           w::Object({{"role", w::String("controller")}, {"proof", proof}}),
           now);
  }

  Config config_;
  bool activate_ = false, initialized_ = false, desired_running_ = false;
  bool stopping_ = false, stopped_ = false, stop_durable_ = false;
  bool stop_handoff_ = false;
  s::QuotaHandoffGrace quota_handoff_grace_;
  bool recovering_ = false, controller_reaped_ = true;
  bool controller_registered_ = false, controller_ready_seen_ = false;
  bool controller_ready_ = false;
  bool circuit_open_ = false;
  bool model_registered_ = false, model_ready_ = false, quarantined_ = false;
  bool model_admission_open_ = false;
  bool model_ready_committed_ = false, cold_unreconciled_ = false;
  bool startup_ready_sent_ = false, detached_ = false;
  int64_t startup_deadline_ = 0;
  s::OwnedFd bootstrap_;
  s::OwnedFd instance_, lease_, listener_, controller_socket_;
  std::string socket_path_, failure_, controller_ticket_;
  s::ProcessRecord self_;
  s::ChildProcess controller_;
  s::ChildProcess model_;
  s::QuotaGate quota_;
  Json model_binding_;
  Json controller_health_;
  int64_t controller_health_at_ = 0;
  uint64_t auxiliary_projection_errors_ = 0;
  bool model_authority_seen_ = false;
  uint64_t probe_used_generation_ = 0;
  s::ChildProcess gateway_;
  s::OwnedFd gateway_socket_;
  s::LaunchGate gateway_gate_;
  std::string gateway_ticket_;
  bool gateway_reaped_ = true, gateway_recovering_ = false;
  bool gateway_registered_ = false, gateway_ready_seen_ = false;
  bool gateway_ready_ = false;
  uint64_t gateway_generation_ = 0, gateway_event_ = 0,
           gateway_bound_model_ = 0;
  int64_t gateway_heartbeat_at_ = 0, gateway_cleanup_started_ = 0;
  int64_t gateway_retry_at_ = 0;
  std::string model_ticket_;
  std::optional<ModelRequest> model_request_;
  uint64_t model_generation_ = 0;
  int64_t model_ready_at_ = 0;
  s::JournalClient journal_;
  s::LaunchGate gate_;
  Purpose purpose_ = Purpose::kNone;
  uint64_t journal_sequence_ = 0, controller_generation_ = 0,
           controller_event_ = 0;
  int64_t heartbeat_at_ = 0, cleanup_started_ = 0, retry_after_ = 0;
  std::vector<Client> clients_;
  std::map<std::pair<pid_t, uint64_t>, int> signalled_;
};
}  // namespace

int main(int argc, char** argv) {
  if (argc != 4 || std::string(argv[1]) != "--config" ||
      (std::string(argv[3]) != "--activate" &&
       std::string(argv[3]) != "--resume")) {
    std::fprintf(stderr,
                 "Usage: q4t-guardian --config FILE --activate|--resume\n");
    return 2;
  }
  try {
    struct sigaction action {};
    action.sa_handler = SignalHandler;
    sigemptyset(&action.sa_mask);
    Require(sigaction(SIGTERM, &action, nullptr) == 0 &&
                sigaction(SIGINT, &action, nullptr) == 0 &&
                sigaction(SIGHUP, &action, nullptr) == 0,
            "signal control unavailable");
    Guardian guardian(ReadConfig(argv[2]),
                      std::string(argv[3]) == "--activate");
    guardian.Initialize();
    for (;;) {
      try {
        return guardian.Run();
      } catch (const std::exception& error) {
        guardian.Fail(error.what());
      }
    }
  } catch (const std::exception& error) {
    std::fprintf(stderr, "Guardian startup failed: %s\n", error.what());
    return 1;
  }
}
