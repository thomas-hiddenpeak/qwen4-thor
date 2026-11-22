#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "sip_media_dtmf.h"

namespace q4t::service {
using SipMediaKey = std::array<unsigned char, 30>;
struct SipMediaOffer {
  LiveRtpCodec codec;
  SipDtmfFormat dtmf;
  SipMediaKey receive_key{};
  int tag = 0, rtp = 0, rtcp = 0;
  bool mux = false, transmit = false, receive = false;
  // Empty entry is the selected audio line; declined lines retain SDP order.
  std::vector<std::string> media_lines;
  ~SipMediaOffer();
};
bool ParseSipMediaOffer(std::string_view wire, const std::string& peer,
                        SipMediaOffer* offer);
std::string SipMediaAnswer(const SipMediaOffer& offer, const std::string& local,
                           int rtp, int rtcp, const SipMediaKey& send_key,
                           uint64_t session_id);
}  // namespace q4t::service
