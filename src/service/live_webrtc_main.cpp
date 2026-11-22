// Isolated WebRTC media carrier. The model process owns session semantics.
// One inherited local SOCK_SEQPACKET preserves ordering and message bounds:
// O offer, A answer, V authenticated media, D UTF-8 data channel, P mono
// 24 kHz S16LE, R channel ready, C channel closed, X close, E failure.
#include "live_webrtc_offer.h"

#include <arpa/inet.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <string_view>

namespace q4t::service {
namespace {
using Clock = std::chrono::steady_clock;
constexpr size_t kPacket = 65535;
constexpr auto kStartup = std::chrono::seconds(15);
constexpr auto kLifetime = std::chrono::hours(1);

bool Send(int fd, char type, const void* bytes = nullptr, size_t size = 0) {
  if (size > kPacket) return false;
  iovec parts[] = {{&type, 1}, {const_cast<void*>(bytes), size}};
  msghdr message{};
  message.msg_iov = parts;
  message.msg_iovlen = size ? 2 : 1;
  return sendmsg(fd, &message, MSG_NOSIGNAL | MSG_DONTWAIT) ==
         static_cast<ssize_t>(size + 1);
}

class RtcPromise {
 public:
  RtcPromise()
      : state_(new State),
        promise_(gst_promise_new_with_change_func(
            Changed, state_,
            [](gpointer p) { delete static_cast<State*>(p); })) {}
  ~RtcPromise() { gst_promise_unref(promise_); }
  RtcPromise(const RtcPromise&) = delete;
  RtcPromise& operator=(const RtcPromise&) = delete;
  GstPromise* Get() const { return promise_; }
  bool Replied(Clock::time_point deadline) {
    std::unique_lock lock(state_->mutex);
    if (!state_->condition.wait_until(lock, deadline,
                                      [this] { return state_->done; })) {
      lock.unlock();
      gst_promise_interrupt(promise_);
      return false;
    }
    if (gst_promise_wait(promise_) != GST_PROMISE_RESULT_REPLIED) return false;
    const auto* value = gst_promise_get_reply(promise_);
    return !value || !gst_structure_has_field(value, "error");
  }

 private:
  struct State {
    std::mutex mutex;
    std::condition_variable condition;
    bool done = false;
  };
  static void Changed(GstPromise*, gpointer data) {
    auto* state = static_cast<State*>(data);
    std::lock_guard lock(state->mutex);
    state->done = true;
    state->condition.notify_one();
  }
  State* state_;
  GstPromise* promise_;
};

struct Media {
  explicit Media(int socket) : fd(socket) {}
  ~Media() {
    ended = true;
    if (pipe) gst_element_set_state(pipe, GST_STATE_NULL);
    gst_clear_object(&channel);
    g_clear_object(&transport);
    gst_clear_object(&rtc);
    gst_clear_object(&src);
    gst_clear_object(&sink);
    gst_clear_object(&receive);
    gst_clear_object(&pipe);
  }

  bool Prepare(const LiveRtpCodec& format, const char* address) {
    const auto rate = std::to_string(format.rate);
    const auto payload = std::to_string(format.payload);
    // Only fixed element names and numeric RTP mappings enter this pipeline.
    std::string description =
        "webrtcbin name=rtc bundle-policy=max-bundle "
        "appsrc name=out is-live=true format=time do-timestamp=true "
        "max-bytes=192000 caps=audio/x-raw,format=S16LE,layout=interleaved,"
        "channels=1,rate=24000 ! audioconvert ! audioresample ! "
        "audio/x-raw,rate=" +
        rate + " ! " + format.encoder + " ! " + format.pay + " pt=" + payload +
        " ! application/x-rtp,media=audio,encoding-name=" + format.name +
        ",payload=(int)" + payload + ",clock-rate=(int)" + rate + " ! rtc. " +
        format.depay + " name=in ! " + format.decoder +
        " ! audioconvert ! audioresample ! "
        "audio/x-raw,format=S16LE,layout=interleaved,channels=1,rate=24000 ! "
        "appsink name=pcm emit-signals=true sync=false async=false "
        "max-buffers=8";
    g_autoptr(GError) error = nullptr;
    pipe = gst_parse_launch(description.c_str(), &error);
    if (!pipe || error || !GST_IS_PIPELINE(pipe)) return false;
    rtc = gst_bin_get_by_name(GST_BIN(pipe), "rtc");
    src = gst_bin_get_by_name(GST_BIN(pipe), "out");
    sink = gst_bin_get_by_name(GST_BIN(pipe), "pcm");
    receive = gst_bin_get_by_name(GST_BIN(pipe), "in");
    if (!rtc || !src || !sink || !receive) return false;
    GstWebRTCICE* ice = nullptr;
    g_object_get(rtc, "ice-agent", &ice, nullptr);
    if (!ice) return false;
    // The optional LAN transport must only use the operator's interface.
    // libnice enables UPnP port mapping by default on this platform.
    g_autoptr(GObject) agent = nullptr;
    g_object_get(ice, "agent", &agent, nullptr);
    if (agent &&
        g_object_class_find_property(G_OBJECT_GET_CLASS(agent), "upnp"))
      g_object_set(agent, "upnp", FALSE, nullptr);
    gboolean added = false;
    g_signal_emit_by_name(ice, "add-local-ip-address", address, &added);
    g_object_set(ice, "ice-tcp", FALSE, nullptr);
    gst_object_unref(ice);
    if (!agent || !added) return false;
    local_address = address;
    g_signal_connect(rtc, "pad-added", G_CALLBACK(Pad), this);
    g_signal_connect(rtc, "prepare-data-channel", G_CALLBACK(Channel), this);
    g_signal_connect(sink, "new-sample", G_CALLBACK(Sample), this);
    return gst_element_set_state(pipe, GST_STATE_PLAYING) !=
           GST_STATE_CHANGE_FAILURE;
  }

  bool Answer(GstWebRTCSessionDescription* offer, Clock::time_point deadline) {
    RtcPromise remote;
    g_signal_emit_by_name(rtc, "set-remote-description", offer, remote.Get());
    if (!remote.Replied(deadline)) return false;
    RtcPromise created;
    g_signal_emit_by_name(rtc, "create-answer", nullptr, created.Get());
    if (!created.Replied(deadline)) return false;
    g_autoptr(GstWebRTCSessionDescription) answer = nullptr;
    if (!gst_structure_get(gst_promise_get_reply(created.Get()), "answer",
                           GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &answer,
                           nullptr))
      return false;
    RtcPromise local;
    g_signal_emit_by_name(rtc, "set-local-description", answer, local.Get());
    if (!local.Replied(deadline)) return false;
    g_autoptr(GObject) transceiver = nullptr;
    g_autoptr(GObject) sender = nullptr;
    g_signal_emit_by_name(rtc, "get-transceiver", 0, &transceiver);
    if (transceiver) g_object_get(transceiver, "sender", &sender, nullptr);
    if (sender) g_object_get(sender, "transport", &transport, nullptr);
    return transport != nullptr;
  }

  bool CheckCertificate(GObject* dtls) const {
    if (!dtls) return false;
    g_autofree gchar* pem = nullptr;
    g_object_get(dtls, "remote-certificate", &pem, nullptr);
    return pem && MatchesLiveCertificate(pem, fingerprint);
  }

  // Only the socket owner inspects transports, outside GStreamer callbacks.
  bool Flush() {
    if (!verified) {
      if (!CheckCertificate(transport) || !Send(fd, 'V')) return false;
      verified = true;
    }
    if (channel_open && !open && !published_close) {
      g_autoptr(GObject) sctp = nullptr;
      g_autoptr(GObject) dtls = nullptr;
      g_object_get(rtc, "sctp-transport", &sctp, nullptr);
      if (sctp) g_object_get(sctp, "transport", &dtls, nullptr);
      if (!CheckCertificate(dtls) || !Send(fd, 'R')) return false;
      open = true;
    }
    if (channel_failed.exchange(false)) CloseChannel();
    std::lock_guard lock(mutex);
    while (!pending.empty()) {
      const auto& [type, bytes] = pending.front();
      if (type != 'D' || !channel_closed) {
        if (type == 'D' && !open) break;
        if (!Send(fd, type, bytes.data(), bytes.size())) return false;
      }
      pending_bytes -= bytes.size();
      pending.pop_front();
    }
    if (channel_closed && !published_close) {
      if (!Send(fd, 'C')) return false;
      open = false;
      published_close = true;
    }
    return true;
  }

  bool Enqueue(char type, const void* data, size_t size) {
    std::lock_guard lock(mutex);
    if (!size || size > kPacket || pending.size() >= 128 ||
        pending_bytes + size > 256 * 1024)
      return false;
    pending.emplace_back(type,
                         std::string(static_cast<const char*>(data), size));
    pending_bytes += size;
    return true;
  }

  bool PublishAnswer() {
    GstWebRTCICEGatheringState state;
    g_object_get(rtc, "ice-gathering-state", &state, nullptr);
    if (state != GST_WEBRTC_ICE_GATHERING_STATE_COMPLETE) return true;
    g_autoptr(GstWebRTCSessionDescription) local = nullptr;
    g_object_get(rtc, "local-description", &local, nullptr);
    if (!local || !KeepLocalIceCandidates(local->sdp, local_address))
      return false;
    g_autofree char* sdp = gst_sdp_message_as_text(local->sdp);
    answered = sdp && Send(fd, 'A', sdp, std::strlen(sdp));
    return answered;
  }

  bool Input(char type, std::string_view value) {
    if (!answered || !verified) return false;
    if (type == 'C' && value.empty()) {
      CloseChannel();
      return true;
    }
    if (value.empty()) return false;
    if (type == 'P') {
      if (value.size() % 2 || gst_app_src_get_current_level_bytes(
                                  GST_APP_SRC(src)) > 192000 - value.size())
        return false;
      auto* buffer = gst_buffer_new_allocate(nullptr, value.size(), nullptr);
      if (!buffer) return false;
      gst_buffer_fill(buffer, 0, value.data(), value.size());
      GST_BUFFER_DURATION(buffer) =
          gst_util_uint64_scale(value.size() / 2, GST_SECOND, 24000);
      return gst_app_src_push_buffer(GST_APP_SRC(src), buffer) == GST_FLOW_OK;
    }
    if (type != 'D' || value.find('\0') != value.npos ||
        !g_utf8_validate(value.data(), value.size(), nullptr))
      return false;
    if (channel_closed) return true;  // A queued IPC write crossed SCTP close.
    if (!open) return false;
    // The channel reference stays owned until the pipeline has stopped.
    // Never hold the queue mutex across a GStreamer send or signal.
    GstWebRTCDataChannel* active;
    {
      std::lock_guard lock(mutex);
      active = channel;
    }
    if (!active) return false;
    guint64 pending = 0;
    g_object_get(active, "buffered-amount", &pending, nullptr);
    if (pending > 256 * 1024 - value.size() ||
        !gst_webrtc_data_channel_send_string_full(
            active, std::string(value).c_str(), nullptr))
      CloseChannel();
    return true;
  }

  void CloseChannel() {
    GstWebRTCDataChannel* active;
    {
      std::lock_guard lock(mutex);
      active = channel;
    }
    if (active && !channel_closed.exchange(true))
      gst_webrtc_data_channel_close(active);
  }

  static void Pad(GstElement*, GstPad* pad, Media* self) {
    if (GST_PAD_DIRECTION(pad) != GST_PAD_SRC) return;
    g_autoptr(GstPad) other = gst_element_get_static_pad(self->receive, "sink");
    if (!other || gst_pad_link(pad, other) != GST_PAD_LINK_OK)
      self->ended = true;
  }
  static GstFlowReturn Sample(GstAppSink* source, Media* self) {
    g_autoptr(GstSample) sample = gst_app_sink_pull_sample(source);
    if (!sample) return GST_FLOW_EOS;
    GstBuffer* buf = gst_sample_get_buffer(sample);
    GstMapInfo map{};
    if (!buf || !gst_buffer_map(buf, &map, GST_MAP_READ)) return GST_FLOW_ERROR;
    const bool ok = map.size % 2 == 0 && self->Enqueue('P', map.data, map.size);
    gst_buffer_unmap(buf, &map);
    if (!ok) self->ended = true;
    return ok ? GST_FLOW_OK : GST_FLOW_ERROR;
  }
  static void Channel(GstElement*, GstWebRTCDataChannel* ch, gboolean local,
                      Media* self) {
    std::lock_guard lock(self->mutex);
    if (local || self->channel) {
      self->ended = true;
      return;
    }
    self->channel = GST_WEBRTC_DATA_CHANNEL(g_object_ref(ch));
    g_signal_connect(ch, "on-open", G_CALLBACK(Open), self);
    g_signal_connect(ch, "on-close", G_CALLBACK(Close), self);
    g_signal_connect(ch, "on-error", G_CALLBACK(Error), self);
    g_signal_connect(ch, "on-message-string", G_CALLBACK(Data), self);
    g_signal_connect(ch, "on-message-data", G_CALLBACK(Binary), self);
  }
  static void Open(GstWebRTCDataChannel*, Media* self) {
    self->channel_open = true;
  }
  static void Close(GstWebRTCDataChannel*, Media* self) {
    self->channel_closed = true;
  }
  static void Error(GstWebRTCDataChannel*, GError*, Media* self) {
    self->channel_failed = true;
  }
  static void Binary(GstWebRTCDataChannel*, GBytes*, Media* self) {
    self->channel_failed = true;
  }
  static void Data(GstWebRTCDataChannel*, gchar* value, Media* self) {
    const size_t size = value ? strnlen(value, kPacket + 1) : 0;
    if (!size || !self->Enqueue('D', value, size)) self->channel_failed = true;
  }

  const int fd;
  GstElement *pipe = nullptr, *rtc = nullptr, *src = nullptr, *sink = nullptr,
             *receive = nullptr;
  GstWebRTCDataChannel* channel = nullptr;
  GObject* transport = nullptr;
  std::string fingerprint, local_address;
  std::mutex mutex;
  std::deque<std::pair<char, std::string>> pending;
  size_t pending_bytes = 0;
  std::atomic<bool> ended{false}, channel_open{false}, channel_closed{false},
      channel_failed{false};
  bool open = false, published_close = false;
  bool answered = false, connected = false, verified = false;
};

bool Run(int fd, const char* address) {
  std::array<char, kPacket + 1> packet;
  Media media(fd);
  const auto started = Clock::now();
  auto last_connected = started;
  while (!media.ended && Clock::now() - started < kLifetime &&
         (media.connected || Clock::now() - started < kStartup)) {
    for (int i = 0; i < 16 && g_main_context_iteration(nullptr, FALSE); ++i) {
    }
    if (media.pipe) {
      g_autoptr(GstBus) bus = gst_element_get_bus(media.pipe);
      g_autoptr(GstMessage) error = gst_bus_pop_filtered(
          bus,
          static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS));
      if (error || (!media.answered && !media.PublishAnswer())) return false;
      GstWebRTCPeerConnectionState state;
      g_object_get(media.rtc, "connection-state", &state, nullptr);
      if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
        if (!media.Flush()) return false;
        media.connected = true;
        last_connected = Clock::now();
      } else if (media.connected &&
                 Clock::now() - last_connected > std::chrono::seconds(10)) {
        return false;
      }
      if (state == GST_WEBRTC_PEER_CONNECTION_STATE_FAILED ||
          state == GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED)
        return false;
    }
    pollfd socket{fd, POLLIN, 0};
    const int ready = poll(&socket, 1, 20);
    if (ready < 0 && errno == EINTR) continue;
    if (ready < 0 || socket.revents & (POLLERR | POLLNVAL)) return false;
    if (!ready) continue;
    const ssize_t n = recv(fd, packet.data(), packet.size(), MSG_TRUNC);
    if (n == 0) return true;
    if (n < 0 || static_cast<size_t>(n) > packet.size()) return false;
    const auto type = packet[0];
    const std::string_view value(packet.data() + 1, n - 1);
    if (type == 'X' && value.empty()) return true;
    if (!media.pipe && type == 'O') {
      LiveRtpCodec codec;
      g_autoptr(GstWebRTCSessionDescription) offer =
          ParseLiveOffer(value, &codec, &media.fingerprint);
      if (!offer) {
        Send(fd, 'I');
        return false;
      }
      if (!media.Prepare(codec, address) ||
          !media.Answer(offer, started + kStartup))
        return false;
    } else if (!media.Input(type, value)) {
      return false;
    }
  }
  return false;
}
}  // namespace
}  // namespace q4t::service

int main(int argc, char** argv) {
  int fd = -1, socktype = 0;
  socklen_t size = sizeof(socktype);
  if (argc != 3) return 2;
  const char* end = argv[1] + std::strlen(argv[1]);
  auto parsed = std::from_chars(argv[1], end, fd);
  sockaddr_storage peer{};
  socklen_t peer_len = sizeof(peer);
  in_addr v4{};
  in6_addr v6{};
  char address[INET6_ADDRSTRLEN]{};
  if (inet_pton(AF_INET, argv[2], &v4) == 1 && v4.s_addr != INADDR_ANY &&
      v4.s_addr != INADDR_BROADCAST && !IN_MULTICAST(ntohl(v4.s_addr))) {
    inet_ntop(AF_INET, &v4, address, sizeof(address));
  } else if (inet_pton(AF_INET6, argv[2], &v6) == 1 &&
             !IN6_IS_ADDR_UNSPECIFIED(&v6) && !IN6_IS_ADDR_MULTICAST(&v6)) {
    inet_ntop(AF_INET6, &v6, address, sizeof(address));
  }
  if (parsed.ec != std::errc{} || parsed.ptr != end || fd < 3 ||
      getsockopt(fd, SOL_SOCKET, SO_TYPE, &socktype, &size) != 0 ||
      socktype != SOCK_SEQPACKET ||
      getpeername(fd, reinterpret_cast<sockaddr*>(&peer), &peer_len) != 0 ||
      peer.ss_family != AF_UNIX || !address[0])
    return 2;
  const bool ok = gst_init_check(nullptr, nullptr, nullptr) &&
                  q4t::service::Run(fd, address);
  q4t::service::Send(fd, ok ? 'X' : 'E');
  return ok ? 0 : 1;
}
