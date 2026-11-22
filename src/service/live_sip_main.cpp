// One incoming TLS trunk per process and client scope. The private owner
// socket is inherited, never a listener. SIP addressing is caller metadata.
class SipTrunk;
#define NTA_LEG_MAGIC_T SipTrunk
#define NTA_OUTGOING_MAGIC_T SipTrunk
#define NTA_INCOMING_MAGIC_T SipTrunk
#define SU_ROOT_MAGIC_T SipTrunk
#include <sofia-sip/msg.h>
#include <sofia-sip/nta.h>
#include <sofia-sip/nta_tport.h>
#include <sofia-sip/sip_protos.h>
#include <sofia-sip/sip_status.h>
#include <sofia-sip/su_strlst.h>
#include <sofia-sip/tport.h>

#include "live_sip_invite.h"

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
#include <cstdlib>
#include <deque>
#include <cstdio>
#include <map>
#include <memory>
#include <set>
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

bool LocalPackets(int fd) {
  int type = 0, family = 0;
  socklen_t length = sizeof(int);
  return fd >= 0 && !getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) &&
         type == SOCK_SEQPACKET &&
         !getsockopt(fd, SOL_SOCKET, SO_DOMAIN, &family, &length) &&
         family == AF_UNIX;
}

struct Call {
  std::string id;
  Clock::time_point deadline;
  std::string offer;
  std::string webhook;
  bool delivered = false;
  int final_status = 0;
  std::string realtime_id{};
  bool realtime_delivered = false;
  nta_leg_t* dialog = nullptr;
};

struct Dialog {
  std::string id;
  nta_incoming_t* invite;  // Released after ACK, BYE or timeout.
  uint32_t remote_cseq;
  Clock::time_point deadline;
  std::unique_ptr<tport_t, decltype(&tport_unref)> carrier;
  nta_outgoing_t* bye = nullptr;
};
}  // namespace

class SipTrunk {
 public:
  SipTrunk(int fd, std::string scope, int webhook_fd)
      : fd_(fd), scope_(std::move(scope)), webhook_fd_(webhook_fd) {}
  ~SipTrunk() {
    for (auto& [request, call] : calls_) {
      if (!call.final_status)
        nta_incoming_treply(request, 503, nullptr, TAG_END());
      nta_incoming_destroy(request);
    }
    while (!dialogs_.empty()) RemoveDialog(dialogs_.begin()->first);
    if (control_ >= 0) su_root_deregister(root_, control_);
    if (webhook_watch_ >= 0) su_root_deregister(root_, webhook_watch_);
    if (leg_) nta_leg_destroy(leg_);
    if (agent_) nta_agent_destroy(agent_);
    std::free(parser_);
    if (root_) su_root_destroy(root_);
  }

  bool Run(const std::string& url, const char* certificate_dir,
           const char* peer) {
    if (!ValidCredentials(certificate_dir)) return false;
    root_ = su_root_create(this);
    parser_ = q4t::service::CreateSipInviteParser();
    if (!root_ || !parser_) return false;
    auto* subjects = su_strlst_create_with(nullptr, peer, nullptr);
    if (!subjects) return false;
    agent_ = nta_agent_create(
        root_, URL_STRING_MAKE(url.c_str()), nullptr, nullptr, NTATAG_UA(1),
        NTATAG_MCLASS(parser_),
        NTATAG_SIPFLAGS(MSG_FLG_CANONIC | MSG_FLG_EXTRACT_COPY),
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
    control_ = Watch(fd_, Command);
    if (control_ < 0) return false;
    if (webhook_fd_ >= 0) {
      webhook_watch_ = Watch(webhook_fd_, WebhookReceipt);
      if (webhook_watch_ < 0) DisableWebhook();
    }
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
  int Watch(int fd, su_wakeup_f handler) {
    su_wait_t wait;
    if (su_wait_create(&wait, fd, SU_WAIT_IN | SU_WAIT_HUP) < 0) return -1;
    const int index = su_root_register(root_, &wait, handler, nullptr, 0);
    su_wait_destroy(&wait);
    return index;
  }

  void DisableWebhook() {
    if (webhook_watch_ >= 0) su_root_deregister(root_, webhook_watch_);
    webhook_watch_ = -1;
    if (webhook_fd_ >= 0) close(webhook_fd_);
    webhook_fd_ = -1;
    for (const auto& id : publishing_)
      Notify("WEBHOOK " + scope_ + " " + id + " unknown");
    publishing_.clear();
  }

  std::string Publish(Call& call, std::string_view realtime_id) {
    if (!realtime_id.empty()) {
      if (realtime_id.size() != 36 || !realtime_id.starts_with("rtc_") ||
          realtime_id.substr(4).find_first_not_of("0123456789abcdef") !=
              realtime_id.npos)
        return "invalid_id";
      if (!call.realtime_id.empty() && call.realtime_id != realtime_id)
        return "conflict";
      call.realtime_id = realtime_id;
    }
    const bool realtime = !realtime_id.empty();
    const auto& id = realtime ? call.realtime_id : call.id;
    if (realtime ? call.realtime_delivered : call.delivered) return "delivered";
    if (publishing_.contains(id)) return "in_flight";
    if (webhook_fd_ < 0) return "unavailable";
    if (publishing_.size() >= kMaxCalls * 2) return "busy";
    const std::string packet =
        "EVENT " + scope_ + " " + id + " " + call.webhook;
    ssize_t n;
    do {
      n = send(webhook_fd_, packet.data(), packet.size(),
               MSG_NOSIGNAL | MSG_DONTWAIT);
    } while (n < 0 && errno == EINTR);
    if (n < 0 && (errno == EAGAIN || errno == ENOBUFS)) return "busy";
    if (n != static_cast<ssize_t>(packet.size())) {
      DisableWebhook();
      return "unavailable";
    }
    publishing_.insert(id);
    return "submitted";
  }

  static int WebhookReceipt(SipTrunk* self, su_wait_t*, su_wakeup_arg_t*) {
    std::array<char, 256> bytes;
    auto n = recv(self->webhook_fd_, bytes.data(), bytes.size(),
                  MSG_DONTWAIT | MSG_TRUNC);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    const std::string prefix = "WEBHOOK " + self->scope_ + " ";
    if (n <= 0 || size_t(n) > bytes.size() ||
        !std::string_view(bytes.data(), n).starts_with(prefix)) {
      self->DisableWebhook();
      return 0;
    }
    std::string packet(bytes.data(), n);
    const auto end = packet.find(' ', prefix.size());
    const auto id = packet.substr(prefix.size(), end - prefix.size());
    if (end == packet.npos || !self->publishing_.contains(id)) {
      self->DisableWebhook();
      return 0;
    }
    const auto state = std::string_view(packet).substr(end + 1);
    const bool delivered = state.starts_with("delivered ") &&
                           state.size() == 13 &&
                           Decimal(state.substr(10)) / 100 == 2;
    const bool rejected =
        state.starts_with("rejected ") && state.size() == 12 &&
        Decimal(state.substr(9)) >= 300 && Decimal(state.substr(9)) <= 599;
    if (!delivered && !rejected && state != "unknown" &&
        state != "unavailable" && state != "invalid") {
      self->DisableWebhook();
      return 0;
    }
    self->publishing_.erase(id);
    if (delivered) {
      for (auto& entry : self->calls_) {
        if (entry.second.id == id) entry.second.delivered = true;
        if (entry.second.realtime_id == id)
          entry.second.realtime_delivered = true;
      }
    }
    self->Notify(packet);
    return 0;
  }

  // Keep room for the final dispositions of already admitted calls even if
  // the owner is busy delivering incoming events. The listener never waits
  // on a local socket write.
  static constexpr size_t kQueueBytes = 512 * 1024;
  static constexpr size_t kQueuePackets = 512;
  void WatchControl() {
    const int events =
        SU_WAIT_IN | SU_WAIT_HUP | (outbox_.empty() ? 0 : SU_WAIT_OUT);
    if (su_root_eventmask(root_, control_, fd_, events) < 0) running_ = false;
  }

  bool SendPacket(const std::string& packet) {
    ssize_t sent;
    do {
      sent =
          send(fd_, packet.data(), packet.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
    } while (sent < 0 && errno == EINTR);
    if (sent == static_cast<ssize_t>(packet.size())) return true;
    if (sent >= 0 || errno != EAGAIN) running_ = false;
    return false;
  }

  bool Notify(std::string packet) {
    if (outbox_.empty() && SendPacket(packet)) return true;
    if (!running_ || packet.size() > kQueueBytes - queued_bytes_ ||
        outbox_.size() >= kQueuePackets) {
      running_ = false;
      return false;
    }
    queued_bytes_ += packet.size();
    outbox_.push_back(std::move(packet));
    if (outbox_.size() == 1) WatchControl();
    return running_;
  }

  void Drain() {
    while (!outbox_.empty() && SendPacket(outbox_.front())) {
      queued_bytes_ -= outbox_.front().size();
      outbox_.pop_front();
    }
    if (outbox_.empty()) WatchControl();
  }

  bool Verified(nta_incoming_t* request) const {
    auto* transport = nta_incoming_transport(agent_, request, nullptr);
    bool verified =
        transport && tport_has_tls(transport) && tport_is_verified(transport);
    if (transport) tport_unref(transport);
    return verified;
  }

  static int Incoming(SipTrunk* self, nta_leg_t*, nta_incoming_t* request,
                      const sip_t* sip) {
    if (!self->Verified(request)) return 403;
    if (sip->sip_request->rq_method != sip_method_invite)
      return sip->sip_request->rq_method == sip_method_options ? 200 : 481;
    if (sip->sip_to->a_tag) return 481;
    const auto established =
        std::count_if(self->dialogs_.begin(), self->dialogs_.end(),
                      [](const auto& entry) { return !entry.second.invite; });
    if (self->calls_.size() + established >= kMaxCalls ||
        self->queued_bytes_ >= kQueueBytes / 2 ||
        self->outbox_.size() >= kQueuePackets / 2)
      return 503;
    const auto headers = q4t::service::SipInviteHeaders(sip);
    auto offer = q4t::service::SipInviteOffer(sip);
    if (!headers) return 400;
    if (!offer) return 415;
    auto id = SessionId();
    if (id.empty() || !nta_incoming_tag(request, nullptr)) return 500;
    if (nta_incoming_treply(request, 180, nullptr, TAG_END()) < 0 ||
        nta_incoming_status(request) != 180)
      return 503;
    std::string webhook;
    if (self->webhook_fd_ >= 0) {
      const auto created =
          std::chrono::duration_cast<std::chrono::seconds>(
              std::chrono::system_clock::now().time_since_epoch())
              .count();
      webhook = std::to_string(created) + "\n" + std::string(*headers);
    }
    self->calls_.emplace(request, Call{id, Clock::now() + kAnswerTime,
                                       std::move(*offer), std::move(webhook)});
    nta_incoming_bind(request, Followup, self);
    self->Notify("INCOMING " + self->scope_ + " " + id + "\n" +
                 std::string(*headers));
    return 0;
  }

  // The transaction owns Call-ID, branch, CSeq, To tag, duplicate matching
  // and CANCEL/ACK correlation. Applications address only the scoped Live ID.
  bool Final(nta_incoming_t* request, Call& call, int status,
             const char* answer = nullptr) {
    call.deadline = Clock::now() + kAckTime;
    const int sent =
        answer ? nta_incoming_treply(request, status, nullptr,
                                     SIPTAG_CONTACT(nta_agent_contact(agent_)),
                                     SIPTAG_CONTENT_TYPE_STR("application/sdp"),
                                     SIPTAG_PAYLOAD_STR(answer), TAG_END())
               : nta_incoming_treply(request, status, nullptr, TAG_END());
    if (sent < 0 || nta_incoming_status(request) != status) {
      call.final_status = -1;
      Notify("FAILED " + scope_ + " " + call.id);
      return false;
    }
    call.final_status = status;
    Notify("FINAL " + scope_ + " " + call.id + " " + std::to_string(status));
    return true;
  }

  void RemoveDialog(nta_leg_t* leg) {
    const auto it = dialogs_.find(leg);
    if (it == dialogs_.end()) return;
    if (it->second.bye) nta_outgoing_destroy(it->second.bye);
    nta_leg_destroy(leg);
    dialogs_.erase(it);
  }

  std::string Accept(nta_incoming_t* request, Call& call,
                     std::string_view answer) {
    if (call.final_status) return "conflict";
    if (call.offer.empty() || answer.size() > 65534 ||
        !answer.starts_with("v=0\r\n") || !answer.ends_with("\r\n") ||
        answer.find('\0') != answer.npos)
      return "invalid_answer";
    std::unique_ptr<msg_t, decltype(&msg_destroy)> original(
        nta_incoming_getrequest(request), msg_destroy);
    const auto* sip = original ? sip_object(original.get()) : nullptr;
    if (!sip || !sip->sip_contact || !sip->sip_from->a_tag)
      return "invalid_dialog";
    std::unique_ptr<tport_t, decltype(&tport_unref)> carrier(
        nta_incoming_transport(agent_, request, nullptr), tport_unref);
    if (!carrier || !tport_is_secondary(carrier.get()) ||
        !tport_has_tls(carrier.get()) || !tport_is_verified(carrier.get()))
      return "failed";
    auto* leg = nta_leg_tcreate(
        agent_, InDialog, this, SIPTAG_CALL_ID(sip->sip_call_id),
        SIPTAG_FROM(sip->sip_to), SIPTAG_TO(sip->sip_from),
        NTATAG_REMOTE_CSEQ(sip->sip_cseq->cs_seq), TAG_END());
    if (!leg) return "failed";
    if (!nta_leg_tag(leg, nta_incoming_gettag(request)) ||
        !nta_leg_get_rtag(leg) ||
        nta_leg_server_route(leg, sip->sip_record_route, sip->sip_contact) <
            0) {
      nta_leg_destroy(leg);
      return "failed";
    }
    std::string body(answer);
    const bool submitted = Final(request, call, 200, body.c_str());
    explicit_bzero(body.data(), body.size());
    if (!submitted) {
      nta_leg_destroy(leg);
      return "failed";
    }
    call.dialog = leg;
    dialogs_.emplace(
        leg, Dialog{call.id, request, sip->sip_cseq->cs_seq,
                    Clock::now() + std::chrono::hours(1), std::move(carrier)});
    return "submitted";
  }

  static int ByeResponse(SipTrunk* self, nta_outgoing_t* bye,
                         const sip_t* sip) {
    const int status = nta_outgoing_status(bye);
    if (status < 200) return 0;
    const auto it = std::find_if(
        self->dialogs_.begin(), self->dialogs_.end(),
        [bye](const auto& entry) { return entry.second.bye == bye; });
    if (it == self->dialogs_.end()) return 0;
    if (sip && !nta_sip_is_internal(sip)) {
      self->Notify("BYE_RESPONSE " + self->scope_ + " " + it->second.id + " " +
                   std::to_string(status));
      self->Notify("ENDED " + self->scope_ + " " + it->second.id);
    } else {
      self->Notify("LOST " + self->scope_ + " " + it->second.id);
    }
    self->RemoveDialog(it->first);
    return 0;
  }

  bool BeginHangup(nta_leg_t* leg, Dialog& dialog) {
    // Route and target come from the dialog. Only the original authenticated
    // transport can be used, even if Contact names a different host.
    if (!tport_is_clear_to_send(dialog.carrier.get())) return false;
    dialog.bye = nta_outgoing_tcreate(
        leg, ByeResponse, this, nullptr, SIP_METHOD_BYE, nullptr,
        NTATAG_TPORT(dialog.carrier.get()), TAG_END());
    if (!dialog.bye) return false;
    dialog.deadline = Clock::now() + kAckTime;
    Notify("ENDING " + scope_ + " " + dialog.id);
    return true;
  }

  static int InDialog(SipTrunk* self, nta_leg_t* leg, nta_incoming_t* request,
                      const sip_t* sip) {
    if (!self->Verified(request)) return 403;
    auto it = self->dialogs_.find(leg);
    if (it == self->dialogs_.end()) return 481;
    const auto method = sip->sip_request->rq_method;
    // The initial 2xx ACK goes to Followup. A retransmitted ACK never
    // confirms a second time and has no response of its own.
    if (method == sip_method_ack) {
      nta_incoming_destroy(request);
      return 0;
    }
    if (sip->sip_cseq->cs_seq <= it->second.remote_cseq) return 500;
    it->second.remote_cseq = sip->sip_cseq->cs_seq;
    if (method == sip_method_options) return 200;
    if (method != sip_method_bye) return 488;
    const bool replied =
        nta_incoming_treply(request, 200, nullptr, TAG_END()) >= 0 &&
        nta_incoming_status(request) == 200;
    nta_incoming_destroy(request);
    self->Notify(std::string(replied ? "ENDED " : "LOST ") + self->scope_ +
                 " " + it->second.id);
    if (auto* invite = it->second.invite) {
      self->calls_.erase(invite);
      nta_incoming_destroy(invite);
    }
    self->RemoveDialog(leg);
    return 0;
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
        call.final_status > 0 &&
        (!call.dialog ||
         (sip->sip_to->a_tag && std::string_view(sip->sip_to->a_tag) ==
                                    nta_leg_get_tag(call.dialog)))) {
      if (call.dialog) self->dialogs_.at(call.dialog).invite = nullptr;
      self->Notify("ACK " + self->scope_ + " " + call.id + " " +
                   std::to_string(call.final_status));
    } else {
      self->RemoveDialog(call.dialog);
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
        RemoveDialog(call.dialog);
        nta_incoming_destroy(request);
        calls_.erase(current);
      }
    }
    for (auto it = dialogs_.begin(); it != dialogs_.end();) {
      auto current = it++;
      if (current->second.invite || now < current->second.deadline) continue;
      if (!current->second.bye && BeginHangup(current->first, current->second))
        continue;
      Notify("LOST " + scope_ + " " + current->second.id);
      RemoveDialog(current->first);
    }
  }

  void ControlRequest(std::string_view packet) {
    // ACCEPT carries the media worker's SDP after the command header.
    const auto newline = packet.find('\n');
    const auto answer = newline == packet.npos ? std::string_view{}
                                               : packet.substr(newline + 1);
    packet = packet.substr(0, newline);
    if (packet.size() > 512) {
      Notify("ERROR invalid_command");
      return;
    }
    std::array<std::string_view, 5> args{};
    size_t count = 0;
    while (!packet.empty() && count < args.size()) {
      const auto end = packet.find(' ');
      args[count++] = packet.substr(0, end);
      packet = end == packet.npos ? std::string_view{} : packet.substr(end + 1);
    }
    const bool offer = args[0] == "OFFER";
    const bool publish = args[0] == "PUBLISH";
    const bool accept = args[0] == "ACCEPT";
    const bool hangup = args[0] == "HANGUP";
    if (!packet.empty() || !Token(args[1]) ||
        ((newline != std::string_view::npos) != accept) ||
        !(((offer || accept || hangup) && count == 4) ||
          (publish && (count == 4 || count == 5)) ||
          (args[0] == "REJECT" && count == 5))) {
      Notify("ERROR invalid_command");
      return;
    }
    const std::string result = "RESULT " + std::string(args[1]) + " ";
    auto it = std::find_if(calls_.begin(), calls_.end(), [&](const auto& call) {
      return call.second.id == args[3];
    });
    if (scope_ == args[2] && hangup) {
      const auto d = std::find_if(
          dialogs_.begin(), dialogs_.end(),
          [&](const auto& entry) { return entry.second.id == args[3]; });
      if (d == dialogs_.end())
        Notify(result + (it == calls_.end() ? "not_found" : "conflict"));
      else if (d->second.invite || d->second.bye)
        Notify(result + "conflict");
      else
        Notify(result +
               (BeginHangup(d->first, d->second) ? "submitted" : "failed"));
      return;
    }
    if (scope_ != args[2] || it == calls_.end()) {
      Notify(result + "not_found");
      return;
    }
    if (accept) {
      Notify(result + Accept(it->first, it->second, answer));
      return;
    }
    if (publish) {
      Notify(result + (it->second.final_status ? "conflict"
                                               : Publish(it->second, args[4])));
      return;
    }
    if (offer) {
      if (it->second.final_status)
        Notify(result + "conflict");
      else
        Notify("OFFER " + std::string(args[1]) + "\n" + it->second.offer);
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

  static int Command(SipTrunk* self, su_wait_t* wait, su_wakeup_arg_t*) {
    const int events = su_wait_events(wait, self->fd_);
    if (events & SU_WAIT_OUT) self->Drain();
    if (!(events & (SU_WAIT_IN | SU_WAIT_HUP | SU_WAIT_ERR))) return 0;
    std::array<char, 66560> bytes;
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
      self->ControlRequest({bytes.data(), size_t(size)});
      explicit_bzero(bytes.data(), size);
    }
    return 0;
  }

  int fd_;
  std::string scope_;
  int webhook_fd_ = -1;
  int webhook_watch_ = -1;
  std::set<std::string> publishing_;
  su_root_t* root_ = nullptr;
  msg_mclass_t* parser_ = nullptr;
  nta_agent_t* agent_ = nullptr;
  nta_leg_t* leg_ = nullptr;
  int control_ = -1;
  std::map<nta_incoming_t*, Call> calls_;
  std::map<nta_leg_t*, Dialog> dialogs_;
  std::deque<std::string> outbox_;
  size_t queued_bytes_ = 0;
  bool running_ = true;
  bool orderly_ = false;
};

int main(int argc, char** argv) {
  if (argc != 7 && argc != 8) {
    std::fputs(
        "usage: q4t-live-sip CONTROL_FD LISTEN_IP PORT TLS_DIRECTORY "
        "TRUNK_SUBJECT CLIENT_SCOPE [WEBHOOK_FD]\n",
        stderr);
    return 2;
  }
  const int fd = Decimal(argv[1]), port = Decimal(argv[3]);
  const int webhook = argc == 8 ? Decimal(argv[7]) : -1;
  if (!LocalPackets(fd) || port < 0 || port > 65535 || !Token(argv[5]) ||
      !Token(argv[6]) ||
      (argc == 8 && (fd == webhook || !LocalPackets(webhook))))
    return 2;
  in6_addr addr{};
  std::string ip = argv[2];
  if (inet_pton(AF_INET6, argv[2], &addr) == 1)
    ip = "[" + ip + "]";
  else if (inet_pton(AF_INET, argv[2], &addr) != 1)
    return 2;
  std::string url = "sips:" + ip + ":" + (port ? std::to_string(port) : "*") +
                    ";transport=tls";
  if (prctl(PR_SET_DUMPABLE, 0) < 0 || su_init() < 0) return 1;
  signal(SIGPIPE, SIG_IGN);
  bool ok;
  {
    SipTrunk trunk(fd, argv[6], webhook);
    ok = trunk.Run(url, argv[4], argv[5]);
  }
  su_deinit();
  return ok ? 0 : 1;
}
