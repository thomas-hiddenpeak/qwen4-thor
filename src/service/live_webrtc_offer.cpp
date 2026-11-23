#include "live_webrtc_offer.h"

#include <gst/sdp/sdp.h>

#include <charconv>
#include <cstring>
#include <string>

namespace q4t::service {
namespace {
bool Match(const char* value, std::string_view wanted) {
  return value && value == wanted;
}

template <class Attribute>
std::string Fingerprint(Attribute attribute) {
  std::string result;
  for (guint i = 0; const char* row = attribute(i); ++i) {
    const std::string_view value(row);
    if (g_ascii_strncasecmp(row, "sha-256 ", 8) != 0) continue;
    const auto hex = value.substr(8);
    if (!result.empty() || hex.size() != 95) return {};
    for (size_t j = 0; j < hex.size(); ++j) {
      if (j % 3 == 2) {
        if (hex[j] != ':') return {};
      } else {
        if (!g_ascii_isxdigit(hex[j])) return {};
        result.push_back(g_ascii_tolower(hex[j]));
      }
    }
  }
  return result;
}

}  // namespace

bool KeepLocalIceCandidates(GstSDPMessage* sdp, std::string_view address) {
  bool usable = false;
  for (guint i = 0; i < gst_sdp_message_medias_len(sdp); ++i) {
    // GStreamer exports a const getter even for an owned, writable SDP.
    auto* media = const_cast<GstSDPMedia*>(gst_sdp_message_get_media(sdp, i));
    for (guint j = gst_sdp_media_attributes_len(media); j > 0; --j) {
      const auto* attribute = gst_sdp_media_get_attribute(media, j - 1);
      if (!Match(attribute->key, "candidate")) continue;
      g_auto(GStrv) tokens =
          g_strsplit(attribute->value ? attribute->value : "", " ", 0);
      if (g_strv_length(tokens) < 8 || !Match(tokens[4], address) ||
          !Match(tokens[7], "host")) {
        gst_sdp_media_remove_attribute(media, j - 1);
      } else {
        usable = true;
      }
    }
  }
  return usable;
}

bool MatchesLiveCertificate(std::string_view pem,
                            std::string_view fingerprint) {
  constexpr std::string_view begin = "-----BEGIN CERTIFICATE-----";
  constexpr std::string_view end = "-----END CERTIFICATE-----";
  if (!pem.starts_with(begin) || pem.size() > 65535) return false;
  pem.remove_prefix(begin.size());
  const auto stop = pem.find(end);
  if (stop == pem.npos) return false;
  gsize length = 0;
  g_autofree guchar* der =
      g_base64_decode(std::string(pem.substr(0, stop)).c_str(), &length);
  if (!der || !length) return false;
  g_autofree char* digest =
      g_compute_checksum_for_data(G_CHECKSUM_SHA256, der, length);
  return digest && fingerprint == digest;
}

GstWebRTCSessionDescription* ParseLiveOffer(std::string_view text,
                                            LiveRtpCodec* codec,
                                            std::string* fingerprint) {
  if (!codec || !fingerprint || text.empty() || text.size() > 65535 ||
      text.find('\0') != text.npos || !text.starts_with("v=0"))
    return nullptr;
  g_autoptr(GstSDPMessage) offer = nullptr;
  if (gst_sdp_message_new(&offer) != GST_SDP_OK ||
      gst_sdp_message_parse_buffer(reinterpret_cast<const guint8*>(text.data()),
                                   text.size(), offer) != GST_SDP_OK ||
      gst_sdp_message_medias_len(offer) > 2)
    return nullptr;
  bool audio = false, data = false;
  LiveRtpCodec selected;
  std::string expected;
  for (guint i = 0; i < gst_sdp_message_medias_len(offer); ++i) {
    const auto* media = gst_sdp_message_get_media(offer, i);
    auto pin = gst_sdp_media_get_attribute_val(media, "fingerprint")
                   ? Fingerprint([&](auto j) {
                       return gst_sdp_media_get_attribute_val_n(
                           media, "fingerprint", j);
                     })
                   : Fingerprint([&](auto j) {
                       return gst_sdp_message_get_attribute_val_n(
                           offer, "fingerprint", j);
                     });
    if (pin.empty() || (!expected.empty() && expected != pin)) return nullptr;
    expected = std::move(pin);
    const auto* protocol = gst_sdp_media_get_proto(media);
    if (Match(gst_sdp_media_get_media(media), "audio") && !audio &&
        Match(protocol, "UDP/TLS/RTP/SAVPF") && SelectLiveRtpAudio(media, &selected)) {
      audio = true;
    } else if (Match(gst_sdp_media_get_media(media), "application") && !data &&
               Match(protocol, "UDP/DTLS/SCTP") &&
               gst_sdp_media_formats_len(media) == 1 &&
               Match(gst_sdp_media_get_format(media, 0),
                     "webrtc-datachannel")) {
      data = true;
    } else {
      return nullptr;
    }
  }
  if (!audio) return nullptr;
  *codec = selected;
  *fingerprint = std::move(expected);
  return gst_webrtc_session_description_new(GST_WEBRTC_SDP_TYPE_OFFER,
                                            g_steal_pointer(&offer));
}
}  // namespace q4t::service
