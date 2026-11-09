#include "q4t/server/client_policy_admin.h"

#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <cstdlib>
#include <initializer_list>

#include "q4t/io/json.h"
#include "q4t/server/sequence_admission.h"

namespace q4t::server {
namespace {
bool Fields(const io::Json& value,
            std::initializer_list<std::string_view> names) {
  if (!value.IsObject() || value.object.size() != names.size()) return false;
  for (const auto& [name, unused] : value.object) {
    (void)unused;
    if (std::find(names.begin(), names.end(), name) == names.end())
      return false;
  }
  return true;
}
bool Number(const io::Json& value, const char* key, uint64_t* result) {
  const auto* field = value.Find(key);
  if (!field || !field->IsNumber() || field->str.empty()) return false;
  for (char c : field->str)
    if (c < '0' || c > '9') return false;
  const auto parsed = std::from_chars(
      field->str.data(), field->str.data() + field->str.size(), *result);
  return parsed.ec == std::errc{} &&
         parsed.ptr == field->str.data() + field->str.size();
}
uint64_t EnvironmentNumber(const char* key) {
  const char* text = std::getenv(key);
  if (!text) return 0;
  const auto size = std::strlen(text);
  if (!size || size > 20) return 0;
  uint64_t value = 0;
  const auto result = std::from_chars(text, text + size, value);
  return result.ec == std::errc{} && result.ptr == text + size ? value : 0;
}
bool ServiceId(const char* text) {
  if (!text || std::strlen(text) != 36) return false;
  for (size_t i = 0; i < 36; ++i) {
    const char c = text[i];
    const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
    if (dash ? c != '-' : !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
      return false;
  }
  return true;
}
}  // namespace

ClientPolicyAdmin::ClientPolicyAdmin(ClientAdmission& admission,
                                     uint32_t inflight, uint32_t queued,
                                     Publish publish)
    : admission_(admission),
      inflight_(inflight),
      queued_(queued),
      publish_(std::move(publish)) {}

ClientPolicyAdmin::~ClientPolicyAdmin() {
  if (worker_.joinable()) {
    worker_.request_stop();
    wake_.notify_all();
    worker_.join();
  }
}

void ClientPolicyAdmin::Start() {
  if (worker_.joinable() || listener_.Get() < 0) return;
  worker_ = std::jthread([this](std::stop_token stop) {
    while (!stop.stop_requested()) {
      Step();
      std::unique_lock lock(wait_mutex_);
      wake_.wait_for(lock, std::chrono::milliseconds(10),
                     [&] { return stop.stop_requested(); });
    }
  });
}

bool ClientPolicyAdmin::Open(const std::string& address,
                             service::ProcessIdentity owner) {
  sockaddr_un target{};
  if (listener_.Get() >= 0 || owner.pid < 2 || !owner.start_ticks ||
      address.empty() || address.size() >= sizeof(target.sun_path) ||
      address.find('\0') != address.npos || !publish_ ||
      !service::OpenProcess(owner, &owner_pidfd_))
    return false;
  owner_ = owner;
  if (!OwnerAlive()) return false;
  target.sun_family = AF_UNIX;
  std::memcpy(target.sun_path + 1, address.data(), address.size());
  service::OwnedFd listener(
      ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
  if (listener.Get() < 0 ||
      ::bind(listener.Get(), reinterpret_cast<sockaddr*>(&target),
             offsetof(sockaddr_un, sun_path) + address.size() + 1) != 0 ||
      ::listen(listener.Get(), 2) != 0)
    return false;
  listener_ = std::move(listener);
  return true;
}

bool ClientPolicyAdmin::OwnerAlive() const noexcept {
  if (owner_pidfd_.Get() < 0) return false;
  pollfd observed{owner_pidfd_.Get(), POLLIN, 0};
  return ::poll(&observed, 1, 0) == 0;
}

std::string ClientPolicyAdmin::Describe() const {
  const auto policy = admission_.Snapshot();
  std::string result =
      R"({"profile":"q4t-native-client-policy-v1","configured":)";
  result += policy ? "true" : "false";
  result += R"(,"mutable":)";
  result += policy && policy->version == 3 ? "true" : "false";
  result += R"(,"publisher_alive":)";
  result += OwnerAlive() ? "true" : "false";
  result += R"(,"revision":)" + std::to_string(policy ? policy->revision : 0);
  result += R"(,"sha256":")" + (policy ? policy->sha256 : std::string()) + '"';
  result += R"(,"content_sha256":")" +
            (policy ? ClientPolicyDigest(*policy) : std::string()) + '"';
  result += R"(,"global_inflight":)" + std::to_string(inflight_);
  result += R"(,"global_queued":)" + std::to_string(queued_);
  result += R"(,"credential_values_redacted":true,"clients":[)";
  if (policy)
    for (size_t i = 0; i < policy->clients.size(); ++i) {
      if (i) result += ',';
      // IDs come exclusively from the bounded validated private policy.
      result += '"' + policy->clients[i].id + '"';
    }
  return result + "]}";
}

Status PublishClientPolicy(ClientAdmission& admission,
                           SequenceAdmissionQueue& queue, std::mutex& mutex,
                           GatewayClientPolicy candidate, uint64_t expected,
                           uint32_t inflight, uint32_t queued) {
  // The same lock guards every queue arrival/selection. A new client cannot
  // reach a queue still using the old inventory, and updates never reset its
  // current rotation or release existing tickets.
  std::unique_lock lock(mutex, std::try_to_lock);
  if (!lock.owns_lock()) return Status::Fail("client queue busy");
  const auto result =
      admission.Publish(std::move(candidate), expected, inflight, queued);
  if (result && admission.FairQueue()) queue.AppendClients(admission.Size());
  return result;
}

std::unique_ptr<ClientPolicyAdmin> StartClientPolicyAdmin(
    ClientAdmission& admission, uint32_t inflight, uint32_t queued,
    ClientPolicyAdmin::Publish publish) noexcept {
  try {
    if (admission.Version() != 3) return nullptr;
    const auto parent_pid = EnvironmentNumber("Q4T_POLICY_PARENT_PID");
    const auto parent_start = EnvironmentNumber("Q4T_POLICY_PARENT_START");
    const auto generation = EnvironmentNumber("Q4T_MODEL_GENERATION");
    const char* id = std::getenv("Q4T_SERVICE_ID");
    if (parent_pid < 2 || parent_pid != uint64_t(::getppid()) ||
        !parent_start || !generation || !ServiceId(id))
      return nullptr;
    auto result = std::make_unique<ClientPolicyAdmin>(
        admission, inflight, queued, std::move(publish));
    const auto address = "q4t-client-policy-" + std::to_string(::geteuid()) +
                         '-' + id + '-' + std::to_string(generation);
    if (!result->Open(address, {pid_t(parent_pid), parent_start}))
      return nullptr;
    result->Start();
    return result;
  } catch (...) {
    return nullptr;
  }
}

void ClientPolicyAdmin::Reply(Peer& peer, bool ok, const char* error) {
  const std::string reply = std::string(R"({"ok":)") + (ok ? "true" : "false") +
                            R"(,"error":")" + error + R"(","status":)" +
                            Describe() + '}';
  if (reply.size() <= 8192)
    ::send(peer.fd.Get(), reply.data(), reply.size(),
           MSG_DONTWAIT | MSG_NOSIGNAL);
  peer = {};
}

void ClientPolicyAdmin::Receive(Peer& peer) {
  for (int frame = 0; frame < 8 && peer.fd.Get() >= 0; ++frame) {
    std::array<char, 16384> buffer{};
    iovec data{buffer.data(), buffer.size()};
    msghdr message{};
    message.msg_iov = &data;
    message.msg_iovlen = 1;
    const auto n = ::recvmsg(peer.fd.Get(), &message, MSG_DONTWAIT | MSG_TRUNC);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) return;
    // No control buffer: ancillary FDs are discarded by the kernel, and the
    // truncation flag rejects the message rather than adopting any FD.
    if (n <= 0 || n > ssize_t(buffer.size()) ||
        message.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) {
      peer = {};
      return;
    }
    if (!peer.bytes) {
      io::Json command;
      const io::JsonParseLimits limits{.max_depth = 2,
                                       .max_values = 16,
                                       .max_string_bytes = 1024,
                                       .reject_duplicate_keys = true};
      if (n > 1024 || !io::ParseJson(std::string(buffer.data(), n), &command,
                                     true, limits)) {
        Reply(peer, false, "client_policy_request_invalid");
        return;
      }
      if (Fields(command, {"op"}) && command.GetString("op") == "status") {
        Reply(peer, true);
        return;
      }
      uint64_t bytes = 0, revision = 0;
      const auto digest = command.GetString("sha256");
      if (!peer.writer || !OwnerAlive() ||
          !Fields(command, {"op", "bytes", "sha256", "expected_revision"}) ||
          command.GetString("op") != "publish" ||
          !Number(command, "bytes", &bytes) || !bytes ||
          bytes > kClientPolicyBytes ||
          !Number(command, "expected_revision", &revision) ||
          revision > kMaxClientRevision || digest.size() != 64 ||
          !std::all_of(digest.begin(), digest.end(), [](char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
          })) {
        Reply(peer, false, "client_policy_publication_refused");
        return;
      }
      peer.bytes = bytes;
      peer.expected_revision = revision;
      peer.digest = digest;
      peer.payload.reserve(bytes);
      continue;
    }
    if (peer.payload.size() + n > peer.bytes) {
      Reply(peer, false, "client_policy_payload_invalid");
      return;
    }
    peer.payload.append(buffer.data(), n);
    if (peer.payload.size() != peer.bytes) continue;
    GatewayClientPolicy candidate;
    if (!OwnerAlive() || !ParseGatewayClientPolicy(peer.payload, &candidate) ||
        ClientPolicyDigest(candidate) != peer.digest) {
      Reply(peer, false, "client_policy_payload_invalid");
      return;
    }
    const auto result = publish_(std::move(candidate), peer.expected_revision);
    Reply(peer, result.ok(),
          result.ok() ? "" : "client_policy_revision_or_capacity_conflict");
  }
}

void ClientPolicyAdmin::Step() noexcept {
  try {
    const auto now = std::chrono::steady_clock::now();
    service::OwnedFd accepted(::accept4(listener_.Get(), nullptr, nullptr,
                                        SOCK_NONBLOCK | SOCK_CLOEXEC));
    if (accepted.Get() >= 0) {
      ucred identity{};
      socklen_t size = sizeof(identity);
      auto free =
          std::find_if(peers_.begin(), peers_.end(),
                       [](const Peer& peer) { return peer.fd.Get() < 0; });
      if (free != peers_.end() &&
          ::getsockopt(accepted.Get(), SOL_SOCKET, SO_PEERCRED, &identity,
                       &size) == 0 &&
          size == sizeof(identity) && identity.uid == ::geteuid()) {
        free->fd = std::move(accepted);
        free->writer = identity.pid == owner_.pid;
        free->deadline = now + std::chrono::seconds(2);
      }
    }
    for (auto& peer : peers_) {
      if (peer.fd.Get() < 0) continue;
      if (now >= peer.deadline)
        peer = {};
      else {
        try {
          Receive(peer);
        } catch (...) {
          peer = {};
        }
      }
    }
  } catch (...) {
    // Allocation or control-plane failure cannot escape into core serving.
    for (auto& peer : peers_) peer = {};
  }
}

}  // namespace q4t::server
