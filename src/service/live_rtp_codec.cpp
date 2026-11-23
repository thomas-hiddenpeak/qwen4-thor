#include "live_rtp_codec.h"

#include <charconv>
#include <cstring>

namespace q4t::service {
bool SelectLiveRtpAudio(const GstSDPMedia* media, LiveRtpCodec* selected,
                        bool static_g711) {
  constexpr LiveRtpCodec codecs[] = {
      {-1, "OPUS", "opusenc", "opusdec", "rtpopuspay", "rtpopusdepay", 48000},
      {-1, "PCMU", "mulawenc", "mulawdec", "rtppcmupay", "rtppcmudepay", 8000},
      {-1, "PCMA", "alawenc", "alawdec", "rtppcmapay", "rtppcmadepay", 8000}};
  for (const auto& codec : codecs) {
    for (guint i = 0; i < gst_sdp_media_formats_len(media); ++i) {
      const char* format = gst_sdp_media_get_format(media, i);
      int pt = -1;
      const char* end = format + std::strlen(format);
      auto parsed = std::from_chars(format, end, pt);
      if (parsed.ec != std::errc{} || parsed.ptr != end || pt < 0 || pt > 127)
        return false;
      g_autoptr(GstCaps) caps = gst_sdp_media_get_caps_from_media(media, pt);
      if (!caps || gst_caps_is_empty(caps)) continue;
      const auto* shape = gst_caps_get_structure(caps, 0);
      const auto* name = gst_structure_get_string(shape, "encoding-name");
      if (!name && static_g711 && (pt == 0 || pt == 8)) {
        // RFC 3551 static assignments may omit rtpmap. An explicit malformed
        // mapping must not be repaired by substituting the static codec.
        bool explicit_mapping = false;
        for (guint j = 0; const char* row = gst_sdp_media_get_attribute_val_n(
                              media, "rtpmap", j);
             ++j) {
          int mapped = -1;
          auto parsed = std::from_chars(row, row + std::strlen(row), mapped);
          if (parsed.ec == std::errc{} && mapped == pt) explicit_mapping = true;
        }
        if (!explicit_mapping) name = pt == 0 ? "PCMU" : "PCMA";
      }
      int rate = 0;
      if (name && g_ascii_strcasecmp(name, codec.name) == 0 &&
          gst_structure_get_int(shape, "clock-rate", &rate) &&
          rate == codec.rate) {
        *selected = codec;
        selected->payload = pt;
        return true;
      }
    }
  }
  return false;
}
}  // namespace q4t::service
