#pragma once

#include "sip_media_dtmf.h"

#include <array>
#include <span>
#include <string_view>

namespace q4t::service {
// Codec and RTP/RTCP session. Only clear, authenticated RTP reaches this graph.
class SipMediaAudio {
 public:
  ~SipMediaAudio();
  bool Prepare(const LiveRtpCodec& codec, const SipDtmfFormat& dtmf,
               bool transmit, int wake_fd);
  bool Tone(unsigned number, bool start);
  bool Start();
  bool Push(int lane, std::span<const unsigned char> data);
  GstSample* Pull(int lane);
  bool Healthy() const;
  void EndAudio();
  bool OutputEnded() const;
  enum { kPcm, kRtp, kRtcp, kEvent };

 private:
  GstElement* pipe_ = nullptr;
  GstElement* tones_ = nullptr;
  GstBus* bus_ = nullptr;
  std::array<GstElement*, 4> sources_{};
  std::array<GstElement*, 3> sinks_{};
};
}  // namespace q4t::service
