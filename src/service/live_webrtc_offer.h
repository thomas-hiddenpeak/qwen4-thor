#pragma once

#include <gst/webrtc/webrtc.h>

#include <string>
#include <string_view>

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

GstWebRTCSessionDescription* ParseLiveOffer(std::string_view text,
                                            LiveRtpCodec* codec,
                                            std::string* fingerprint);
bool KeepLocalIceCandidates(GstSDPMessage* sdp, std::string_view address);
bool MatchesLiveCertificate(std::string_view pem, std::string_view fingerprint);
}  // namespace q4t::service
