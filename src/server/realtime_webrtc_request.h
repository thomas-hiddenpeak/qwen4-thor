// Raw SDP or multipart SDP with an optional Realtime configuration.
#pragma once

#include "q4t/server/object_store.h"

namespace q4t::server {
struct RealtimeWebRtcOffer {
  std::string sdp;
  io::Json session;
  bool explicit_session = false;
};
ObjectReply ParseRealtimeWebRtcOffer(std::string_view content_type,
                                     std::string_view body,
                                     RealtimeWebRtcOffer* offer,
                                     bool translation = false);
}  // namespace q4t::server
