#include "q4t/server/progress_transport.h"

#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <charconv>
#include <cstring>
#include <cstdlib>
#include <stdexcept>
#include <string_view>

#include "q4t/server/responses_owner.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
bool Uuid(const std::array<char, 36>& text) {
  for (size_t i = 0; i < text.size(); ++i) {
    const char ch = text[i];
    const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
    if (dash ? ch != '-'
             : !((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f')))
      return false;
  }
  return true;
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
}  // namespace

bool ProgressIdentity::Valid() const noexcept {
  return pid > 0 && start_ticks > 0 && pid_namespace > 0 &&
         controller_generation > 0 && model_generation > 0 && Uuid(boot_id) &&
         Uuid(service_id);
}

ProgressPacket EncodeProgress(const RequestProgress::Snapshot& snapshot,
                              const ProgressIdentity& identity,
                              uint64_t sequence, uint64_t successes,
                              uint64_t steps) noexcept {
  ProgressPacket packet;
  std::memcpy(packet.data.data(), "Q4TPR002", 8);
  packet.size = 8;
  const auto number = [&](uint64_t value) {
    for (int byte = 0; byte < 8; ++byte) {
      packet.data[packet.size++] = static_cast<unsigned char>(value & 0xff);
      value >>= 8;
    }
  };
  const size_t count = std::min(snapshot.count, snapshot.rows.size());
  number(sequence);
  number(snapshot.observed_ns);
  number(snapshot.complete && identity.Valid() && sequence > 0 &&
         count == snapshot.count);
  number(identity.pid);
  number(identity.start_ticks);
  number(identity.pid_namespace);
  number(identity.controller_generation);
  number(identity.model_generation);
  number(snapshot.active);
  number(snapshot.untracked);
  number(count);
  number(successes);
  number(steps);
  for (const auto& text : {identity.boot_id, identity.service_id}) {
    std::memcpy(packet.data.data() + packet.size, text.data(), text.size());
    packet.size += text.size();
  }
  for (size_t i = 0; i < count; ++i) {
    const auto& row = snapshot.rows[i];
    for (const auto value :
         {row.ticket, row.deadline_ns, uint64_t(row.stage), row.stage_since_ns,
          uint64_t(row.wait), row.wait_since_ns, uint64_t(row.work),
          row.work_id, row.work_since_ns, row.completed, row.last_completed_ns,
          row.updated_ns})
      number(value);
  }
  return packet;
}

ProgressPublisher::ProgressPublisher(service::OwnedFd socket,
                                     ProgressIdentity identity,
                                     const RequestProgress& progress,
                                     const std::atomic<uint64_t>& successes,
                                     const std::atomic<uint64_t>& steps)
    : socket_(std::move(socket)),
      identity_(std::move(identity)),
      progress_(progress),
      successes_(successes),
      steps_(steps) {
  int type = 0;
  socklen_t size = sizeof(type);
  const int flags = fcntl(socket_.Get(), F_GETFL);
  if (flags < 0 ||
      getsockopt(socket_.Get(), SOL_SOCKET, SO_TYPE, &type, &size) != 0 ||
      type != SOCK_SEQPACKET ||
      fcntl(socket_.Get(), F_SETFL, flags | O_NONBLOCK) != 0 ||
      fcntl(socket_.Get(), F_SETFD, FD_CLOEXEC) != 0)
    throw std::runtime_error("invalid private progress socket");
}
ProgressPublisher::~ProgressPublisher() {
  if (worker_.joinable()) {
    worker_.request_stop();
    wake_.notify_all();
    worker_.join();
  }
}
void ProgressPublisher::Start() {
  if (worker_.joinable()) return;
  worker_ = std::jthread([this](std::stop_token stop) {
    while (!stop.stop_requested()) {
      Publish();
      std::unique_lock lock(wait_mutex_);
      wake_.wait_for(lock, std::chrono::milliseconds(250),
                     [&] { return stop.stop_requested(); });
    }
  });
}
bool ProgressPublisher::Publish() noexcept {
  if (publishing_.test_and_set()) {
    dropped_.fetch_add(1);
    return false;
  }
  ScopeExit release([&] { publishing_.clear(); });
  if (sequence_ == std::numeric_limits<uint64_t>::max()) {
    dropped_.fetch_add(1);
    return false;
  }
  const auto packet = EncodeProgress(progress_.Read(), identity_, ++sequence_,
                                     successes_.load(), steps_.load());
  const auto written = send(socket_.Get(), packet.data.data(), packet.size,
                            MSG_DONTWAIT | MSG_NOSIGNAL);
  if (written == static_cast<ssize_t>(packet.size)) return true;
  dropped_.fetch_add(1);
  return false;
}

std::unique_ptr<ProgressPublisher> StartProgressPublisher(
    const RequestProgress& progress, const std::atomic<uint64_t>& successes,
    const std::atomic<uint64_t>& steps) noexcept {
  try {
    const auto raw_fd = EnvironmentNumber("Q4T_PROGRESS_FD");
    if (raw_fd < 3 || raw_fd > 1048576) return nullptr;
    const int fd = static_cast<int>(raw_fd);
    ucred peer{};
    socklen_t size = sizeof(peer);
    const auto parent_ticks = EnvironmentNumber("Q4T_PROGRESS_PARENT_START");
    service::ProcessRecord parent;
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &size) != 0 ||
        peer.pid != getppid() || peer.uid != geteuid() || !parent_ticks ||
        !service::ReadProcess(peer.pid, &parent) ||
        parent.identity.start_ticks != parent_ticks)
      return nullptr;
    // The verified socket is observation-owned even if identity lookup fails.
    service::OwnedFd socket(fd);
    const auto owner = CurrentResponseOwner();
    ProgressIdentity identity;
    identity.pid = owner.GetInt("pid");
    identity.start_ticks = owner.GetInt("start_ticks");
    identity.pid_namespace = owner.GetInt("pid_namespace");
    identity.controller_generation =
        EnvironmentNumber("Q4T_CONTROLLER_GENERATION");
    identity.model_generation = EnvironmentNumber("Q4T_MODEL_GENERATION");
    const auto boot = owner.GetString("boot_id");
    const char* service = std::getenv("Q4T_SERVICE_ID");
    if (boot.size() != 36 || !service || std::strlen(service) != 36)
      return nullptr;
    std::copy_n(boot.data(), 36, identity.boot_id.data());
    std::copy_n(service, 36, identity.service_id.data());
    if (!identity.Valid()) return nullptr;
    auto publisher = std::make_unique<ProgressPublisher>(
        std::move(socket), identity, progress, successes, steps);
    publisher->Start();
    return publisher;
  } catch (...) {
    return nullptr;
  }
}
}  // namespace q4t::server
