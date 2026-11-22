// One SIP dialog's media, negotiated on an authenticated signaling connection.
// Private packets: O SDP offer, A SDP answer, G confirmed dialog, V active,
// P mono S16LE at 24 kHz, X drain/closed, I unusable offer, E media failure.
#include "sip_media_audio.h"
#include "sip_media_offer.h"

#include <arpa/inet.h>
#include <poll.h>
#include <srtp2/srtp.h>
#include <sys/eventfd.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <charconv>
#include <chrono>
#include <cstring>
#include <optional>

namespace q4t::service {
namespace {
using Clock = std::chrono::steady_clock;
constexpr size_t kDatagram = 8192, kControl = 65536;
using Bytes = std::span<const unsigned char>;

class Fd {
 public:
  ~Fd() { Reset(); }
  void Reset(int value = -1) {
    if (fd >= 0) close(fd);
    fd = value;
  }
  int fd = -1;
};
struct Address {
  sockaddr_storage addr{};
  socklen_t size = 0;
  bool Parse(const char* ip) {
    auto* a = reinterpret_cast<sockaddr_in*>(&addr);
    if (inet_pton(AF_INET, ip, &a->sin_addr) == 1) {
      const auto n = ntohl(a->sin_addr.s_addr);
      if (!n || n == INADDR_BROADCAST || IN_MULTICAST(n)) return false;
      a->sin_family = AF_INET;
      size = sizeof(*a);
      return true;
    }
    auto* b = reinterpret_cast<sockaddr_in6*>(&addr);
    if (inet_pton(AF_INET6, ip, &b->sin6_addr) != 1 ||
        IN6_IS_ADDR_UNSPECIFIED(&b->sin6_addr) ||
        IN6_IS_ADDR_MULTICAST(&b->sin6_addr) ||
        IN6_IS_ADDR_V4MAPPED(&b->sin6_addr))
      return false;
    b->sin6_family = AF_INET6;
    size = sizeof(*b);
    return true;
  }
  sockaddr* Ptr() { return reinterpret_cast<sockaddr*>(&addr); }
  void Port(int port) {
    if (addr.ss_family == AF_INET)
      reinterpret_cast<sockaddr_in*>(&addr)->sin_port = htons(port);
    else
      reinterpret_cast<sockaddr_in6*>(&addr)->sin6_port = htons(port);
  }
  int Port() const {
    return addr.ss_family == AF_INET
               ? ntohs(reinterpret_cast<const sockaddr_in*>(&addr)->sin_port)
               : ntohs(reinterpret_cast<const sockaddr_in6*>(&addr)->sin6_port);
  }
};
bool Random(void* dest, size_t size) {
  auto* p = static_cast<unsigned char*>(dest);
  while (size) {
    const auto n = getrandom(p, size, 0);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return false;
    p += n;
    size -= n;
  }
  return true;
}
bool Send(int fd, char type, Bytes data = {}) {
  iovec parts[] = {{&type, 1},
                   {const_cast<unsigned char*>(data.data()), data.size()}};
  msghdr m{};
  m.msg_iov = parts;
  m.msg_iovlen = data.empty() ? 1 : 2;
  return sendmsg(fd, &m, MSG_NOSIGNAL | MSG_DONTWAIT) ==
         ssize_t(data.size() + 1);
}
struct Crypto {
  ~Crypto() {
    if (session) srtp_dealloc(session);
  }
  bool Open(bool inbound, SipMediaKey& key) {
    srtp_policy_t p{};
    srtp_crypto_policy_set_aes_cm_128_hmac_sha1_80(&p.rtp);
    srtp_crypto_policy_set_aes_cm_128_hmac_sha1_80(&p.rtcp);
    p.ssrc.type = inbound ? ssrc_any_inbound : ssrc_any_outbound;
    p.key = key.data();
    p.window_size = 128;
    return srtp_create(&session, &p) == srtp_err_status_ok;
  }
  srtp_t session = nullptr;
};
int Bind(Fd* fd, Address local, int port) {
  local.Port(port);
  fd->Reset(socket(local.addr.ss_family,
                   SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
  if (fd->fd < 0 || bind(fd->fd, local.Ptr(), local.size) ||
      getsockname(fd->fd, local.Ptr(), &local.size))
    return -1;
  return local.Port();
}
class Carrier {
 public:
  explicit Carrier(int owner) : owner_(owner) {}
  bool Run(const char* local_ip, const char* remote_ip) {
    Address local, remote;
    if (!local.Parse(local_ip) || !remote.Parse(remote_ip) ||
        local.addr.ss_family != remote.addr.ss_family)
      return false;
    wake_.Reset(eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC));
    pollfd init{owner_, POLLIN, 0};
    if (wake_.fd < 0 || poll(&init, 1, 10000) <= 0) return false;
    auto n = recv(owner_, control_.data(), control_.size(), MSG_TRUNC);
    if (n <= 1 || n > ssize_t(control_.size()) || control_[0] != 'O')
      return false;
    const bool parsed = ParseSipMediaOffer(
        {reinterpret_cast<char*>(control_.data() + 1), size_t(n - 1)},
        remote_ip, &offer_);
    explicit_bzero(control_.data(), n);
    if (!parsed) {
      Send(owner_, 'I');
      return false;
    }
    int port = -1, rtcp = -1;
    for (int attempt = 0; attempt < 64; ++attempt) {
      port = Bind(&rtp_, local, 0);
      if (port < 0) return false;
      if (offer_.mux) {
        rtcp = port;
        break;
      }
      if (port % 2 || port == 65535) continue;
      rtcp = Bind(&rtcp_, local, port + 1);
      if (rtcp > 0) break;
    }
    if (rtcp < 0) return false;
    remote.Port(offer_.rtp);
    if (connect(rtp_.fd, remote.Ptr(), remote.size)) return false;
    if (!offer_.mux) {
      remote.Port(offer_.rtcp);
      if (connect(rtcp_.fd, remote.Ptr(), remote.size)) return false;
    }
    uint64_t session_id = 0;
    SipMediaKey key{};
    if (!Random(key.data(), key.size()) ||
        !Random(&session_id, sizeof(session_id)))
      return false;
    const bool crypto =
        out_.Open(false, key) && in_.Open(true, offer_.receive_key);
    explicit_bzero(offer_.receive_key.data(), offer_.receive_key.size());
    auto answer = crypto ? SipMediaAnswer(offer_, local_ip, port, rtcp, key,
                                          session_id >> 1)
                         : "";
    explicit_bzero(key.data(), key.size());
    const bool ready =
        !answer.empty() && audio_.Prepare(offer_.codec, wake_.fd) &&
        Send(owner_, 'A',
             {reinterpret_cast<const unsigned char*>(answer.data()),
              answer.size()});
    explicit_bzero(answer.data(), answer.size());
    if (!ready) return false;
    const auto start = Clock::now();
    while (Clock::now() - start < std::chrono::hours(1) && audio_.Healthy()) {
      pollfd fds[] = {{owner_, POLLIN, 0},
                      {rtp_.fd, POLLIN, 0},
                      {rtcp_.fd, POLLIN, 0},
                      {wake_.fd, POLLIN, 0}};
      const int count = poll(fds, 4, draining_ ? 10 : 250);
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 || fds[0].revents & (POLLHUP | POLLERR | POLLNVAL))
        return false;
      if (!active_ && Clock::now() - start > std::chrono::seconds(35))
        return false;
      if (fds[0].revents & POLLIN) {
        n = recv(owner_, control_.data(), control_.size(), MSG_TRUNC);
        if (n <= 0 || n > ssize_t(control_.size())) return false;
        const auto kind = control_[0];
        if (kind == 'X' && n == 1 && !draining_) {
          if (!active_ || !offer_.transmit) return true;
          draining_ = true;
          end_ = Clock::now() + std::chrono::seconds(2);
          audio_.EndAudio();
        } else if (kind == 'G' && n == 1 && !active_) {
          if (!DiscardEarly(rtp_.fd) ||
              (rtcp_.fd >= 0 && !DiscardEarly(rtcp_.fd)) || !audio_.Start() ||
              !Send(owner_, 'V'))
            return false;
          active_ = true;
        } else if (kind == 'P' && active_ && !draining_ && n > 1 &&
                   (n - 1) % 2 == 0) {
          if (offer_.transmit &&
              !audio_.Push(SipMediaAudio::kPcm,
                           {control_.data() + 1, size_t(n - 1)}))
            return false;
        } else
          return false;
      }
      for (int i = 1; i <= 2; ++i) {
        if (fds[i].revents & (POLLERR | POLLNVAL)) return false;
        if (fds[i].revents & POLLIN && !Incoming(fds[i].fd, i == 2))
          return false;
      }
      if (fds[3].revents & POLLIN) {
        eventfd_t v;
        eventfd_read(wake_.fd, &v);
      }
      if (active_ && !Output()) return false;
      if (draining_ && audio_.OutputEnded()) return true;
      if (draining_ && Clock::now() > end_) return false;
    }
    return false;
  }

 private:
  bool DiscardEarly(int fd) {
    // The owner and UDP sockets can become readable in the same poll. Discard
    // pending pre-confirmation media before admitting the first active packet.
    for (int i = 0; i < 1024; ++i) {
      if (recv(fd, wire_.data(), wire_.size(), MSG_DONTWAIT) < 0)
        return errno == EAGAIN || errno == EWOULDBLOCK;
    }
    return false;
  }
  bool Incoming(int fd, bool rtcp) {
    ssize_t n = recv(fd, wire_.data(), kDatagram, MSG_DONTWAIT | MSG_TRUNC);
    if (n < 0) return errno == EINTR || errno == EAGAIN;
    if (!active_ || draining_ || n < 8 || n > ssize_t(kDatagram) ||
        wire_[0] >> 6 != 2)
      return true;
    if (offer_.mux) rtcp = wire_[1] >= 192 && wire_[1] <= 223;
    if (!rtcp &&
        (!offer_.receive || n < 12 || (wire_[1] & 127) != offer_.codec.payload))
      return true;
    uint32_t source;
    memcpy(&source, wire_.data() + (rtcp ? 4 : 8), sizeof(source));
    if (source_ && source != *source_) return true;
    int size = n;
    const auto status =
        rtcp ? srtp_unprotect_rtcp(in_.session, wire_.data(), &size)
             : srtp_unprotect(in_.session, wire_.data(), &size);
    if (status != srtp_err_status_ok) return true;
    source_ = source;
    return audio_.Push(rtcp ? SipMediaAudio::kRtcp : SipMediaAudio::kRtp,
                       {wire_.data(), size_t(size)});
  }
  bool Output() {
    for (int lane = 0; lane < 3; ++lane) {
      for (int i = 0; i < 32; ++i) {
        g_autoptr(GstSample) sample = audio_.Pull(lane);
        if (!sample) break;
        auto* buf = gst_sample_get_buffer(sample);
        const auto bytes = gst_buffer_get_size(buf);
        if (!bytes) continue;
        if (lane == SipMediaAudio::kPcm) {
          if (bytes > control_.size() - 1 || bytes % 2 ||
              gst_buffer_extract(buf, 0, control_.data(), bytes) != bytes ||
              !Send(owner_, 'P', {control_.data(), bytes}))
            return false;
        } else {
          const bool rtcp = lane == SipMediaAudio::kRtcp;
          if (bytes > kDatagram ||
              gst_buffer_extract(buf, 0, wire_.data(), bytes) != bytes)
            return false;
          int n = bytes;
          const auto status =
              rtcp ? srtp_protect_rtcp(out_.session, wire_.data(), &n)
                   : srtp_protect(out_.session, wire_.data(), &n);
          if (status != srtp_err_status_ok) return false;
          const auto sent = send(rtcp && !offer_.mux ? rtcp_.fd : rtp_.fd,
                                 wire_.data(), n, MSG_NOSIGNAL | MSG_DONTWAIT);
          if (sent != n) return false;
        }
      }
    }
    return true;
  }
  int owner_;
  Fd rtp_, rtcp_, wake_;
  SipMediaOffer offer_;
  Crypto in_, out_;
  SipMediaAudio audio_;
  std::optional<uint32_t> source_;
  bool active_ = false, draining_ = false;
  Clock::time_point end_{};
  alignas(4)
      std::array<unsigned char, kDatagram + SRTP_MAX_TRAILER_LEN + 4> wire_{};
  std::array<unsigned char, kControl> control_{};
};
}  // namespace
}  // namespace q4t::service
int main(int argc, char** argv) {
  if (argc != 4) return 2;
  int fd = -1, type = 0;
  const auto* end = argv[1] + strlen(argv[1]);
  const auto parsed = std::from_chars(argv[1], end, fd);
  sockaddr_storage peer{};
  socklen_t len = sizeof(peer), size = sizeof(type);
  if (parsed.ec != std::errc{} || parsed.ptr != end || fd < 3 ||
      getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size) ||
      type != SOCK_SEQPACKET ||
      getpeername(fd, reinterpret_cast<sockaddr*>(&peer), &len) ||
      peer.ss_family != AF_UNIX || prctl(PR_SET_DUMPABLE, 0) ||
      !gst_init_check(nullptr, nullptr, nullptr) ||
      srtp_init() != srtp_err_status_ok)
    return 2;
  bool ok = false;
  {
    q4t::service::Carrier carrier(fd);
    ok = carrier.Run(argv[2], argv[3]);
  }
  q4t::service::Send(fd, ok ? 'X' : 'E');
  srtp_shutdown();
  return ok ? 0 : 1;
}
