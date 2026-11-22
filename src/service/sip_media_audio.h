#pragma once

#include "live_rtp_codec.h"

#include <array>
#include <span>
#include <string_view>

namespace q4t::service {
// Codec and RTP/RTCP session. Only clear, authenticated RTP reaches this graph.
class SipMediaAudio {
 public:
  ~SipMediaAudio();
  bool Prepare(const LiveRtpCodec& codec, int wake_fd);
  bool Start();
  bool Push(int lane, std::span<const unsigned char> data);
  GstSample* Pull(int lane);
  bool Healthy() const;
  void EndAudio();
  bool OutputEnded() const;
  enum { kPcm, kRtp, kRtcp };

 private:
  GstElement* pipe_ = nullptr;
  GstBus* bus_ = nullptr;
  std::array<GstElement*, 3> sources_{}, sinks_{};
};
}  // namespace q4t::service
