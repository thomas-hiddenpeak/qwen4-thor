#include "sip_media_offer.h"

#include <arpa/inet.h>
#include <string.h>

#include <charconv>
#include <set>

namespace q4t::service {
namespace {
int Decimal(std::string_view value) {
  int n = -1;
  const auto [end, ec] =
      std::from_chars(value.data(), value.data() + value.size(), n);
  return ec == std::errc{} && end == value.data() + value.size() ? n : -1;
}
bool Token(const char* text) {
  if (!text) return false;
  std::string_view s(text);
  return !s.empty() && s.size() <= 64 &&
         s.find_first_not_of(
             "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-./"
             "_+") == s.npos;
}
bool PeerAddress(const char* type, const char* address,
                 const std::string& peer) {
  if (!type || !address) return false;
  const int family = !strcmp(type, "IP4")   ? AF_INET
                     : !strcmp(type, "IP6") ? AF_INET6
                                            : 0;
  std::array<unsigned char, 16> a{}, b{};
  return family && inet_pton(family, address, a.data()) == 1 &&
         inet_pton(family, peer.c_str(), b.data()) == 1 && a == b;
}
bool Crypto(const GstSDPMedia* media, SipMediaOffer* out) {
  std::set<int> tags;
  for (guint i = 0;
       const char* attr = gst_sdp_media_get_attribute_val_n(media, "crypto", i);
       ++i) {
    std::string_view v(attr);
    auto sep = v.find(' ');
    if (sep == v.npos || sep > 9) return false;
    const int tag = Decimal(v.substr(0, sep));
    if (tag < 1 || !tags.insert(tag).second) return false;
    v.remove_prefix(sep + 1);
    constexpr std::string_view suite = "AES_CM_128_HMAC_SHA1_80 inline:";
    if (out->tag || !v.starts_with(suite)) continue;
    v.remove_prefix(suite.size());
    // Exactly one full 128-bit key and 112-bit salt. Optional crypto
    // parameters need their own negotiated semantics before selection.
    if (v.size() != 40 ||
        v.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWX"
                            "YZ0123456789+/") != v.npos)
      continue;
    gsize size = 0;
    g_autofree guchar* raw = g_base64_decode(std::string(v).c_str(), &size);
    if (!raw || size != out->receive_key.size()) return false;
    memcpy(out->receive_key.data(), raw, size);
    explicit_bzero(raw, size);
    out->tag = tag;
  }
  return out->tag != 0;
}
template <class Get>
bool Direction(guint count, Get get, std::string_view* value) {
  bool found = false;
  for (guint i = 0; i < count; ++i) {
    const auto* attr = get(i);
    std::string_view key(attr->key);
    if (key != "sendrecv" && key != "sendonly" && key != "recvonly" &&
        key != "inactive")
      continue;
    if (found || (attr->value && *attr->value)) return false;
    *value = key;
    found = true;
  }
  return true;
}
bool Audio(const GstSDPMessage* msg, const GstSDPMedia* media,
           const std::string& peer, SipMediaOffer* out) {
  if (gst_sdp_media_connections_len(media) > 1 ||
      gst_sdp_media_get_num_ports(media) > 1)
    return false;
  const auto* conn = gst_sdp_media_connections_len(media)
                         ? gst_sdp_media_get_connection(media, 0)
                         : gst_sdp_message_get_connection(msg);
  if (!conn || !conn->nettype || strcmp(conn->nettype, "IN") ||
      !PeerAddress(conn->addrtype, conn->address, peer))
    return false;
  const auto port = gst_sdp_media_get_port(media);
  if (!port || port > 65535 || !SelectLiveRtpAudio(media, &out->codec, true))
    return false;
  const int pt = out->codec.payload;
  if (pt < 96 && !((pt == 0 && !strcmp(out->codec.name, "PCMU")) ||
                   (pt == 8 && !strcmp(out->codec.name, "PCMA"))))
    return false;
  std::string_view direction = "sendrecv";
  if (!Direction(
          gst_sdp_message_attributes_len(msg),
          [&](auto i) { return gst_sdp_message_get_attribute(msg, i); },
          &direction) ||
      !Direction(
          gst_sdp_media_attributes_len(media),
          [&](auto i) { return gst_sdp_media_get_attribute(media, i); },
          &direction))
    return false;
  out->transmit = direction == "recvonly" || direction == "sendrecv";
  out->receive = direction == "sendonly" || direction == "sendrecv";
  if (out->transmit || out->receive)
    out->dtmf = SelectSipDtmf(media, out->codec);
  out->rtp = port;
  out->mux = gst_sdp_media_get_attribute_val(media, "rtcp-mux") != nullptr;
  if (gst_sdp_media_get_attribute_val(media, "rtcp-mux-only") && !out->mux)
    return false;
  out->rtcp = static_cast<int>(port) + 1;
  if (const char* attr = gst_sdp_media_get_attribute_val(media, "rtcp")) {
    g_auto(GStrv) fields = g_strsplit(attr, " ", -1);
    const auto count = g_strv_length(fields);
    if ((count != 1 && count != 4) ||
        (count == 4 && (strcmp(fields[1], "IN") ||
                        !PeerAddress(fields[2], fields[3], peer))) ||
        gst_sdp_media_get_attribute_val_n(media, "rtcp", 1))
      return false;
    out->rtcp = Decimal(fields[0]);
  }
  if (out->mux) out->rtcp = out->rtp;
  return out->rtcp >= 1 && out->rtcp <= 65535 &&
         (out->mux || out->rtcp != out->rtp) && Crypto(media, out);
}
}  // namespace
SipMediaOffer::~SipMediaOffer() {
  explicit_bzero(receive_key.data(), receive_key.size());
}

bool ParseSipMediaOffer(std::string_view wire, const std::string& peer,
                        SipMediaOffer* out) {
  if (!out || wire.empty() || wire.size() > 65534 ||
      wire.find('\0') != wire.npos || !wire.starts_with("v=0"))
    return false;
  g_autoptr(GstSDPMessage) msg = nullptr;
  if (gst_sdp_message_new(&msg) != GST_SDP_OK ||
      gst_sdp_message_parse_buffer(reinterpret_cast<const guint8*>(wire.data()),
                                   wire.size(), msg) != GST_SDP_OK ||
      !gst_sdp_message_get_origin(msg)->sess_id ||
      g_strcmp0(gst_sdp_message_get_version(msg), "0") ||
      gst_sdp_message_medias_len(msg) > 16)
    return false;
  bool selected = false;
  for (guint i = 0; i < gst_sdp_message_medias_len(msg); ++i) {
    const auto* m = gst_sdp_message_get_media(msg, i);
    const auto* type = gst_sdp_media_get_media(m);
    const auto* proto = gst_sdp_media_get_proto(m);
    const auto* format =
        gst_sdp_media_formats_len(m) ? gst_sdp_media_get_format(m, 0) : nullptr;
    if (!Token(type) || !Token(proto) || !Token(format)) return false;
    if (!selected && !strcmp(type, "audio") && !strcmp(proto, "RTP/SAVP") &&
        gst_sdp_media_get_port(m)) {
      if (!Audio(msg, m, peer, out)) return false;
      out->media_lines.emplace_back();
      selected = true;
    } else
      out->media_lines.push_back("m=" + std::string(type) + " 0 " + proto +
                                 " " + format + "\r\n");
  }
  return selected;
}

std::string SipMediaAnswer(const SipMediaOffer& offer, const std::string& local,
                           int rtp, int rtcp, const SipMediaKey& key,
                           uint64_t session_id) {
  const std::string address =
      "IN " + std::string(local.find(':') == local.npos ? "IP4 " : "IP6 ") +
      local;
  g_autofree char* encoded = g_base64_encode(key.data(), key.size());
  std::string s = "v=0\r\no=- " + std::to_string(session_id) + " 1 " + address +
                  "\r\ns=q4t\r\nc=" + address + "\r\nt=0 0\r\n";
  for (const auto& line : offer.media_lines) {
    if (!line.empty()) {
      s += line;
      continue;
    }
    const auto payload = std::to_string(offer.codec.payload);
    s += "m=audio " + std::to_string(rtp) + " RTP/SAVP " + payload;
    if (offer.dtmf.payload >= 0) s += " " + std::to_string(offer.dtmf.payload);
    s += "\r\na=rtpmap:" + payload + " " + offer.codec.name + "/" +
         std::to_string(offer.codec.rate);
    if (!strcmp(offer.codec.name, "OPUS")) s += "/2";
    if (offer.dtmf.payload >= 0) {
      const auto pt = std::to_string(offer.dtmf.payload);
      s += "\r\na=rtpmap:" + pt + " telephone-event/" +
           std::to_string(offer.dtmf.rate) + "\r\na=fmtp:" + pt + " " +
           offer.dtmf.Parameters();
    }
    s += "\r\na=crypto:" + std::to_string(offer.tag) +
         " AES_CM_128_HMAC_SHA1_80 inline:" + encoded + "\r\n";
    s += offer.mux ? "a=rtcp-mux\r\n"
                   : "a=rtcp:" + std::to_string(rtcp) + "\r\n";
    s += offer.transmit ? (offer.receive ? "a=sendrecv\r\n" : "a=sendonly\r\n")
                        : (offer.receive ? "a=recvonly\r\n" : "a=inactive\r\n");
  }
  explicit_bzero(encoded, strlen(encoded));
  return s;
}
}  // namespace q4t::service
