#pragma once

#include <gst/webrtc/webrtc.h>

#include "live_rtp_codec.h"

#include <string>
#include <string_view>

namespace q4t::service {
GstWebRTCSessionDescription* ParseLiveOffer(std::string_view text,
                                            LiveRtpCodec* codec,
                                            std::string* fingerprint);
bool KeepLocalIceCandidates(GstSDPMessage* sdp, std::string_view address);
bool MatchesLiveCertificate(std::string_view pem, std::string_view fingerprint);
}  // namespace q4t::service
