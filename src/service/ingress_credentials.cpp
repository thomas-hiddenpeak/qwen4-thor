#include "q4t/service/ingress_credentials.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>
#include <ctime>
#include <utility>
#include <vector>

#include "q4t/service/guardian_wire.h"
#include "q4t/service/ingress_transport.h"

namespace q4t::service::ingress {
namespace {
using namespace std::chrono_literals;
constexpr size_t kCertificateBytes = 65536;
constexpr size_t kKeyBytes = 16384;
constexpr size_t kMaterialBytes = 3 * kCertificateBytes + kKeyBytes;

bool Read(const std::string& path, size_t limit, bool secret,
          std::string* out) {
  if (path.empty() || path.front() != '/') return false;
  OwnedFd fd(
      ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK));
  struct stat before {
  }, after{};
  if (fd.Get() < 0 || ::fstat(fd.Get(), &before) || !S_ISREG(before.st_mode) ||
      before.st_uid != ::geteuid() ||
      (before.st_mode & (secret ? 0077 : 0022)) || before.st_size <= 0 ||
      before.st_size > static_cast<off_t>(limit))
    return false;
  std::array<char, 4096> buffer{};
  for (;;) {
    const ssize_t n = ::read(fd.Get(), buffer.data(), buffer.size());
    if (n < 0 && errno == EINTR) continue;
    if (n < 0 || out->size() + n > limit) return false;
    if (!n) break;
    out->append(buffer.data(), n);
  }
  return ::fstat(fd.Get(), &after) == 0 &&
         out->size() == size_t(before.st_size) &&
         before.st_size == after.st_size &&
         before.st_mtim.tv_sec == after.st_mtim.tv_sec &&
         before.st_mtim.tv_nsec == after.st_mtim.tv_nsec &&
         before.st_ctim.tv_sec == after.st_ctim.tv_sec &&
         before.st_ctim.tv_nsec == after.st_ctim.tv_nsec;
}

void ReplyMaterial(int fd, uint32_t status, const TlsMaterial& material) {
  const std::array<uint32_t, 5> header = {
      status, static_cast<uint32_t>(material.certificate.size()),
      static_cast<uint32_t>(material.key.size()),
      static_cast<uint32_t>(material.ca.size()),
      static_cast<uint32_t>(material.crl.size())};
  std::string bytes(reinterpret_cast<const char*>(header.data()),
                    sizeof(header));
  bytes += material.certificate;
  bytes += material.key;
  bytes += material.ca;
  bytes += material.crl;
  // Exactly one bounded packet. Never retry a partial packet as a new frame.
  const auto deadline = Clock::now() + 2s;
  while (Clock::now() < deadline) {
    const auto n =
        ::send(fd, bytes.data(), bytes.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n >= 0 || (errno != EINTR && errno != EAGAIN)) return;
    pollfd event{fd, POLLOUT, 0};
    if (::poll(&event, 1, 100) < 0 && errno != EINTR) return;
  }
}
}  // namespace

int CredentialReader(int fd, pid_t parent) {
  if (::prctl(PR_SET_PDEATHSIG, SIGKILL) || ::getppid() != parent) return 1;
  pollfd event{fd, POLLIN, 0};
  if (::poll(&event, 1, 2000) != 1) return 1;
  std::array<char, 32768> buffer{};
  const ssize_t n = ::recv(fd, buffer.data(), buffer.size(), MSG_TRUNC);
  io::Json request;
  if (n <= 0 || n > ssize_t(buffer.size()) ||
      !wire::Parse(std::string(buffer.data(), n), &request) ||
      !wire::HasKeys(request, {"certificate", "key", "ca", "crl", "lease"}))
    return 1;
  const auto lease = request.GetString("lease");
  // Kernel-owned lease survives a dead parent while a reader is stuck in I/O.
  // A later entrypoint cannot accumulate blocked readers for this service.
  if (lease.empty() || lease.size() > 100) return 1;
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  std::memcpy(address.sun_path + 1, lease.data(), lease.size());
  OwnedFd owner(::socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0));
  if (owner.Get() < 0 ||
      ::bind(owner.Get(), reinterpret_cast<sockaddr*>(&address),
             offsetof(sockaddr_un, sun_path) + lease.size() + 1)) {
    ReplyMaterial(fd, 2, {});
    return 1;
  }
  TlsMaterial material;
  const auto ca = request.GetString("ca");
  const auto crl = request.GetString("crl");
  if (!Read(request.GetString("certificate"), kCertificateBytes, false,
            &material.certificate) ||
      !Read(request.GetString("key"), kKeyBytes, true, &material.key) ||
      (!ca.empty() && !Read(ca, kCertificateBytes, false, &material.ca)) ||
      (!crl.empty() && !Read(crl, kCertificateBytes, false, &material.crl))) {
    ReplyMaterial(fd, 1, {});
    return 1;
  }
  ReplyMaterial(fd, 0, material);
  return 0;
}

Credentials::Credentials(TlsFiles files) : files_(std::move(files)) {}
Credentials::~Credentials() {
  if (pid_ < 0) return;
  if (process_.Get() >= 0)
    ::syscall(SYS_pidfd_send_signal, process_.Get(), SIGKILL, nullptr, 0);
  socket_ = {};
  const auto deadline = Clock::now() + 100ms;
  do {
    const auto result = ::waitpid(pid_, nullptr, WNOHANG);
    if (result == pid_ || (result < 0 && errno == ECHILD)) return;
    ::poll(nullptr, 0, 5);
  } while (Clock::now() < deadline);
  // Never wait on uninterruptible storage. The helper keeps the service's
  // kernel lease until actual death, including across entrypoint generations.
}

void Credentials::Fail(const char* reason) {
  error_ = reason;
  candidate_.reset();
  socket_ = {};
  if (pid_ >= 0 && !killed_) {
    if (process_.Get() >= 0)
      ::syscall(SYS_pidfd_send_signal, process_.Get(), SIGKILL, nullptr, 0);
    killed_ = true;
  }
}

void Credentials::Start() {
  attempted_ = true;
  ++attempt_;
  received_ = killed_ = false;
  error_.clear();
  int pair[2];
  if (::socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0,
                   pair)) {
    Fail("tls_reader_start_failed");
    return;
  }
  OwnedFd client(pair[0]), child(pair[1]);
  const int capacity = 2 * static_cast<int>(kMaterialBytes + 20);
  if (::setsockopt(child.Get(), SOL_SOCKET, SO_SNDBUF, &capacity,
                   sizeof(capacity)) ||
      ::setsockopt(client.Get(), SOL_SOCKET, SO_RCVBUF, &capacity,
                   sizeof(capacity))) {
    Fail("tls_reader_start_failed");
    return;
  }
  // Use a distinct descriptor above the private exec target (3).
  OwnedFd inherited(::fcntl(child.Get(), F_DUPFD_CLOEXEC, 4));
  if (inherited.Get() < 0) {
    Fail("tls_reader_start_failed");
    return;
  }
  posix_spawn_file_actions_t actions;
  if (::posix_spawn_file_actions_init(&actions)) {
    Fail("tls_reader_start_failed");
    return;
  }
  int error = ::posix_spawn_file_actions_adddup2(&actions, inherited.Get(), 3);
  if (!error) error = ::posix_spawn_file_actions_addclosefrom_np(&actions, 4);
  std::string parent = std::to_string(::getpid());
  char executable[] = "/proc/self/exe", option[] = "--tls-reader";
  char* arguments[] = {executable, option, parent.data(), nullptr};
  char language[] = "LANG=C", path[] = "PATH=/usr/bin:/bin";
  char* environment[] = {language, path, nullptr};
  pid_t spawned = -1;
  if (!error)
    error = ::posix_spawn(&spawned, executable, &actions, nullptr, arguments,
                          environment);
  ::posix_spawn_file_actions_destroy(&actions);
  if (error) {
    Fail("tls_reader_start_failed");
    return;
  }
  pid_ = spawned;
  process_ = OwnedFd(::syscall(SYS_pidfd_open, spawned, 0));
  if (process_.Get() < 0) {
    // Spawned child is still ours and has not been waited on. Closing the IPC
    // makes its bounded startup receive fail; never signal a guessed PID later.
    Fail("tls_reader_identity_unavailable");
    return;
  }
  socket_ = std::move(client);
  std::string request;
  if (!wire::Encode(
          wire::Object({{"certificate", wire::String(files_.certificate)},
                        {"key", wire::String(files_.key)},
                        {"ca", wire::String(files_.ca)},
                        {"crl", wire::String(files_.crl)},
                        {"lease", wire::String(files_.lease)}}),
          &request) ||
      ::send(socket_.Get(), request.data(), request.size(),
             MSG_NOSIGNAL | MSG_DONTWAIT) != ssize_t(request.size())) {
    Fail("tls_reader_start_failed");
    return;
  }
  deadline_ = Clock::now() + 2s;
}

void Credentials::Step(bool reload) {
  if (pid_ < 0 && (!attempted_ || reload)) Start();
  // A reload while the sole reader is active is coalesced, not queued.
  if (pid_ < 0) return;
  int status = 0;
  // Observe exit before draining the packet: once reaped, a successful child
  // has completed send(), so an EAGAIN cannot race a subsequently observed
  // exit.
  const pid_t owned_pid = pid_;
  const auto result = ::waitpid(owned_pid, &status, WNOHANG);
  if (result == owned_pid) pid_ = -1;
  if (!received_ && socket_.Get() >= 0) {
    std::vector<char> bytes(kMaterialBytes + 20);
    const auto n = ::recv(socket_.Get(), bytes.data(), bytes.size(),
                          MSG_DONTWAIT | MSG_TRUNC);
    if (n > 0) {
      received_ = true;
      std::array<uint32_t, 5> header{};
      if (n < 20 || n > ssize_t(bytes.size())) {
        Fail("tls_reader_invalid_reply");
      } else {
        std::memcpy(header.data(), bytes.data(), 20);
        if (header[0] || header[1] > kCertificateBytes ||
            header[2] > kKeyBytes || header[3] > kCertificateBytes ||
            header[4] > kCertificateBytes ||
            uint64_t(n) !=
                20ull + header[1] + header[2] + header[3] + header[4]) {
          Fail(header[0] == 2 ? "tls_reader_busy" : "tls_material_unreadable");
        } else {
          TlsMaterial material{
              std::string(bytes.data() + 20, header[1]),
              std::string(bytes.data() + 20 + header[1], header[2]),
              std::string(bytes.data() + 20 + header[1] + header[2], header[3]),
              std::string(bytes.data() + 20 + header[1] + header[2] + header[3],
                          header[4])};
          candidate_ = TlsContext::Create(material, files_.name,
                                          files_.client_certificate, &error_);
          socket_ = {};
        }
      }
    } else if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
      Fail("tls_reader_disconnected");
    }
  }
  if (result == owned_pid) {
    process_ = {};
    socket_ = {};
    if (candidate_ && !killed_ && WIFEXITED(status) &&
        WEXITSTATUS(status) == 0) {
      context_.store(std::move(candidate_));
      ++generation_;
      error_.clear();
    } else if (error_.empty()) {
      error_ = "tls_reader_failed";
    }
    candidate_.reset();
  } else if (result < 0 && errno != EINTR) {
    // Do not release ownership on an unconfirmed reap or attempt a replacement.
    Fail("tls_reader_reap_unconfirmed");
  } else if (!killed_ && Clock::now() >= deadline_) {
    Fail("tls_reader_timeout");
  }
}

io::Json Credentials::Status() const {
  const auto context = Context();
  const bool ready = context && context->ValidNow();
  const auto expiry = context ? context->ExpiresAt() : 0;
  return wire::Object(
      {{"enabled", wire::Boolean(true)},
       {"ready", wire::Boolean(ready)},
       {"valid_until_unix", wire::Unsigned(expiry > 0 ? expiry : 0)},
       {"expires_within_seven_days",
        wire::Boolean(context && expiry - std::time(nullptr) <= 7 * 86400)},
       {"client_crl_enforced",
        wire::Boolean(context && context->ChecksRevocation())},
       {"generation", wire::Unsigned(generation_)},
       {"load_attempt", wire::Unsigned(attempt_)},
       {"fingerprint_sha256",
        wire::String(context ? context->Fingerprint() : "")},
       {"reload_pending", wire::Boolean(pid_ >= 0)},
       {"reader_cleanup_pending", wire::Boolean(pid_ >= 0 && killed_)},
       {"error",
        wire::String(!error_.empty()     ? error_
                     : context && !ready ? "tls_credentials_no_longer_valid"
                                         : "")}});
}
}  // namespace q4t::service::ingress
