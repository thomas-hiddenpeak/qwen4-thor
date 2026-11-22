// One incoming TLS trunk per process and client scope. The private owner
// socket is inherited, never a listener. SIP addressing is caller metadata.
class SipTrunk;
#define NTA_LEG_MAGIC_T SipTrunk
#define NTA_INCOMING_MAGIC_T SipTrunk
#define SU_ROOT_MAGIC_T SipTrunk
#include <sofia-sip/nta.h>
#include <sofia-sip/nta_tport.h>
#include <sofia-sip/sip_status.h>
#include <sofia-sip/su_strlst.h>
#include <sofia-sip/tport.h>

#include <arpa/inet.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <openssl/ssl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <map>
#include <memory>
#include <string>
#include <string_view>

namespace {
using Clock = std::chrono::steady_clock;
constexpr size_t kMaxCalls = 64;
constexpr auto kAnswerTime = std::chrono::seconds(30);
constexpr auto kAckTime = std::chrono::seconds(33);

int Decimal(std::string_view value) {
  int result = -1;
  const auto [end, error] =
      std::from_chars(value.data(), value.data() + value.size(), result);
  return error == std::errc{} && end == value.data() + value.size() ? result
                                                                    : -1;
}

bool Token(std::string_view value) {
  return !value.empty() && value.size() <= 128 &&
         value.find_first_not_of(
             "abcdefghijklmnopqrstuvwxyz"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789-_.") == value.npos;
}

std::string SessionId() {
  std::array<unsigned char, 16> random{};
  if (getrandom(random.data(), random.size(), 0) != ssize_t(random.size()))
    return {};
  std::string id = "live_";
  constexpr char hex[] = "0123456789abcdef";
  for (auto byte : random) {
    id += hex[byte >> 4];
    id += hex[byte & 15];
  }
  return id;
}

// Some packaged Sofia versions only log an unusable server key and still
// bind the port. Never advertise a listener that cannot authenticate itself.
bool ValidCredentials(const std::string& directory) {
  const std::string identity = directory + "/agent.pem";
  const std::string trust = directory + "/cafile.pem";
  std::unique_ptr<SSL_CTX, decltype(&SSL_CTX_free)> ctx(
      SSL_CTX_new(TLS_server_method()), SSL_CTX_free);
  if (!ctx) return false;
  SSL_CTX_set_default_passwd_cb(ctx.get(),
                                [](char*, int, int, void*) { return 0; });
  if (SSL_CTX_use_certificate_file(ctx.get(), identity.c_str(),
                                   SSL_FILETYPE_PEM) != 1 ||
      SSL_CTX_use_PrivateKey_file(ctx.get(), identity.c_str(),
                                  SSL_FILETYPE_PEM) != 1 ||
      SSL_CTX_check_private_key(ctx.get()) != 1 ||
      SSL_CTX_load_verify_locations(ctx.get(), trust.c_str(), nullptr) != 1)
    return false;
  const auto* leaf = SSL_CTX_get0_certificate(ctx.get());
  return leaf && X509_cmp_current_time(X509_get0_notBefore(leaf)) < 0 &&
         X509_cmp_current_time(X509_get0_notAfter(leaf)) > 0;
}

// Sofia can try a new connection to a Via address after losing the original
// transport. Inbound-only signaling may write only on accepted connections.
bool DisableConnect() {
  sock_filter rules[] = {
      BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)),
      BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_connect, 0, 1),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EACCES),
      BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)};
  sock_fprog program{4, rules};
  return prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 &&
         syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                 SECCOMP_FILTER_FLAG_TSYNC, &program) == 0;
}

struct Call {
  std::string id;
  Clock::time_point deadline;
  int final_status = 0;
};
}  // namespace

class SipTrunk {
 public:
  SipTrunk(int fd, std::string scope) : fd_(fd), scope_(std::move(scope)) {}
  ~SipTrunk() {
    for (auto& [request, call] : calls_) {
      if (!call.final_status)
        nta_incoming_treply(request, 503, nullptr, TAG_END());
      nta_incoming_destroy(request);
    }
    if (control_ >= 0) su_root_deregister(root_, control_);
    if (leg_) nta_leg_destroy(leg_);
    if (agent_) nta_agent_destroy(agent_);
    if (root_) su_root_destroy(root_);
  }

  bool Run(const std::string& url, const char* certificate_dir,
           const char* peer) {
    if (!ValidCredentials(certificate_dir)) return false;
    root_ = su_root_create(this);
    if (!root_) return false;
    auto* subjects = su_strlst_create_with(nullptr, peer, nullptr);
    if (!subjects) return false;
    agent_ = nta_agent_create(
        root_, URL_STRING_MAKE(url.c_str()), nullptr, nullptr, NTATAG_UA(1),
        NTATAG_MAXSIZE(65535), NTATAG_MAX_PROCEEDING(kMaxCalls),
        NTATAG_MERGE_482(1), NTATAG_CANCEL_487(0), NTATAG_USE_NAPTR(0),
        NTATAG_USE_SRV(0), TPTAG_TIMEOUT(10000), TPTAG_IDLE(90000),
        TPTAG_CERTIFICATE(certificate_dir),
        TPTAG_TLS_VERIFY_POLICY(TPTLS_VERIFY_SUBJECTS_ALL),
        TPTAG_TLS_VERIFY_SUBJECTS(subjects), TPTAG_TLS_VERIFY_DATE(1),
        TAG_END());
    su_strlst_destroy(subjects);
    if (!agent_ || !DisableConnect()) return false;
    leg_ =
        nta_leg_tcreate(agent_, Incoming, this, NTATAG_NO_DIALOG(1), TAG_END());
    if (!leg_) return false;
    su_wait_t wait;
    if (su_wait_create(&wait, fd_, SU_WAIT_IN | SU_WAIT_HUP) < 0) return false;
    control_ = su_root_register(root_, &wait, Command, nullptr, 0);
    su_wait_destroy(&wait);
    if (control_ < 0) return false;
    auto* contact = nta_agent_contact(agent_);
    if (!contact || !contact->m_url->url_port) return false;
    Notify(std::string("READY ") + contact->m_url->url_port);
    while (running_) {
      su_root_step(root_, 1000);
      Expire();
    }
    return orderly_;
  }

 private:
  bool Notify(const std::string& packet) {
    if (send(fd_, packet.data(), packet.size(), MSG_NOSIGNAL | MSG_DONTWAIT) ==
        static_cast<ssize_t>(packet.size()))
      return true;
    running_ = false;
    return false;
  }

  static int Incoming(SipTrunk* self, nta_leg_t*, nta_incoming_t* request,
                      const sip_t* sip) {
    auto* transport = nta_incoming_transport(self->agent_, request, nullptr);
    bool verified =
        transport && tport_has_tls(transport) && tport_is_verified(transport);
    if (transport) tport_unref(transport);
    if (!verified) return 403;
    if (sip->sip_request->rq_method != sip_method_invite)
      return sip->sip_request->rq_method == sip_method_options ? 200 : 481;
    if (sip->sip_to->a_tag) return 481;
    if (self->calls_.size() >= kMaxCalls) return 503;
    auto id = SessionId();
    if (id.empty() || !nta_incoming_tag(request, nullptr)) return 500;
    if (nta_incoming_treply(request, 180, nullptr, TAG_END()) < 0 ||
        nta_incoming_status(request) != 180)
      return 503;
    self->calls_.emplace(request, Call{id, Clock::now() + kAnswerTime});
    nta_incoming_bind(request, Followup, self);
    self->Notify("INCOMING " + self->scope_ + " " + id);
    return 0;
  }

  // The transaction owns Call-ID, branch, CSeq, To tag, duplicate matching
  // and CANCEL/ACK correlation. Applications address only the scoped Live ID.
  bool Final(nta_incoming_t* request, Call& call, int status) {
    call.deadline = Clock::now() + kAckTime;
    if (nta_incoming_treply(request, status, nullptr, TAG_END()) < 0 ||
        nta_incoming_status(request) != status) {
      call.final_status = -1;
      Notify("FAILED " + scope_ + " " + call.id);
      return false;
    }
    call.final_status = status;
    Notify("FINAL " + scope_ + " " + call.id + " " + std::to_string(status));
    return true;
  }

  static int Followup(SipTrunk* self, nta_incoming_t* request,
                      const sip_t* sip) {
    auto it = self->calls_.find(request);
    if (it == self->calls_.end()) return 0;
    auto& call = it->second;
    if (sip && sip->sip_request->rq_method == sip_method_cancel) {
      if (!call.final_status) self->Final(request, call, 487);
      return 0;
    }
    if (sip && sip->sip_request->rq_method == sip_method_ack &&
        call.final_status > 0) {
      self->Notify("ACK " + self->scope_ + " " + call.id + " " +
                   std::to_string(call.final_status));
    } else {
      self->Notify("LOST " + self->scope_ + " " + call.id);
    }
    self->calls_.erase(it);
    nta_incoming_destroy(request);
    return 0;
  }

  void Expire() {
    const auto now = Clock::now();
    for (auto it = calls_.begin(); it != calls_.end();) {
      auto current = it++;
      auto& [request, call] = *current;
      if (now < call.deadline) continue;
      if (!call.final_status) {
        Final(request, call, 480);
      } else {
        Notify("LOST " + scope_ + " " + call.id);
        nta_incoming_destroy(request);
        calls_.erase(current);
      }
    }
  }

  void Reject(std::string_view packet) {
    // REJECT <request-id> <scope> <session-id> <SIP status>.
    // FINAL means submitted; ACK independently proves the peer received it.
    std::array<std::string_view, 5> args;
    for (size_t i = 0; i < args.size(); ++i) {
      auto end = packet.find(' ');
      if ((end == packet.npos) != (i == args.size() - 1)) {
        Notify("ERROR invalid_command");
        return;
      }
      args[i] = packet.substr(0, end);
      if (end != packet.npos) packet.remove_prefix(end + 1);
    }
    if (args[0] != "REJECT" || !Token(args[1])) {
      Notify("ERROR invalid_command");
      return;
    }
    const std::string result = "RESULT " + std::string(args[1]) + " ";
    auto it = std::find_if(calls_.begin(), calls_.end(), [&](const auto& call) {
      return call.second.id == args[3];
    });
    if (scope_ != args[2] || it == calls_.end()) {
      Notify(result + "not_found");
      return;
    }
    const int status = Decimal(args[4]);
    if (status < 300 || status > 699) {
      Notify(result + "invalid_status");
    } else if (it->second.final_status) {
      Notify(result + "conflict");
    } else {
      bool submitted = Final(it->first, it->second, status);
      Notify(result + (submitted ? "submitted" : "failed"));
    }
  }

  static int Command(SipTrunk* self, su_wait_t*, su_wakeup_arg_t*) {
    std::array<char, 512> bytes;
    ssize_t size =
        recv(self->fd_, bytes.data(), bytes.size(), MSG_DONTWAIT | MSG_TRUNC);
    if (size < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    if (size <= 0) {
      self->running_ = false;
    } else if (size > ssize_t(bytes.size())) {
      self->Notify("ERROR invalid_command");
    } else if (std::string_view(bytes.data(), size) == "STOP") {
      self->running_ = false;
      self->orderly_ = true;
    } else {
      self->Reject({bytes.data(), size_t(size)});
    }
    return 0;
  }

  int fd_;
  std::string scope_;
  su_root_t* root_ = nullptr;
  nta_agent_t* agent_ = nullptr;
  nta_leg_t* leg_ = nullptr;
  int control_ = -1;
  std::map<nta_incoming_t*, Call> calls_;
  bool running_ = true;
  bool orderly_ = false;
};

int main(int argc, char** argv) {
  if (argc != 7) {
    std::fputs(
        "usage: q4t-live-sip CONTROL_FD LISTEN_IP PORT TLS_DIRECTORY "
        "TRUNK_SUBJECT CLIENT_SCOPE\n",
        stderr);
    return 2;
  }
  const int fd = Decimal(argv[1]), port = Decimal(argv[3]);
  int type = 0, family = 0;
  socklen_t length = sizeof(int);
  if (fd < 0 || port < 0 || port > 65535 || !Token(argv[5]) ||
      !Token(argv[6]) || getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) ||
      type != SOCK_SEQPACKET ||
      getsockopt(fd, SOL_SOCKET, SO_DOMAIN, &family, &length) ||
      family != AF_UNIX)
    return 2;
  in6_addr addr{};
  std::string ip = argv[2];
  if (inet_pton(AF_INET6, argv[2], &addr) == 1)
    ip = "[" + ip + "]";
  else if (inet_pton(AF_INET, argv[2], &addr) != 1)
    return 2;
  std::string url = "sips:" + ip + ":" + (port ? std::to_string(port) : "*") +
                    ";transport=tls";
  if (su_init() < 0) return 1;
  signal(SIGPIPE, SIG_IGN);
  bool ok;
  {
    SipTrunk trunk(fd, argv[6]);
    ok = trunk.Run(url, argv[4], argv[5]);
  }
  su_deinit();
  return ok ? 0 : 1;
}
