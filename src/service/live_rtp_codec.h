#pragma once

#include <gst/sdp/sdp.h>

namespace q4t::service {
// Negotiated RTP mapping, never interpolated from untrusted SDP text.
struct LiveRtpCodec {
  int payload = -1;
  const char* name = nullptr;
  const char* encoder = nullptr;
  const char* decoder = nullptr;
  const char* pay = nullptr;
  const char* depay = nullptr;
  int rate = 0;
};

bool SelectLiveRtpAudio(const GstSDPMedia* media, LiveRtpCodec* selected,
                        bool static_g711 = false);
}  // namespace q4t::service
