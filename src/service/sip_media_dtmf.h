#pragma once

#include "live_rtp_codec.h"

#include <array>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>

namespace q4t::service {

inline constexpr std::string_view kDtmfKeys = "0123456789*#ABCD";

struct SipDtmfFormat {
  int payload = -1, rate = 0;
  uint16_t events = 0;
  bool Allows(unsigned number) const {
    return number < kDtmfKeys.size() && (events & (1u << number));
  }
  std::string Parameters() const;
};

SipDtmfFormat SelectSipDtmf(const GstSDPMedia* media,
                            const LiveRtpCodec& audio);

// SIP media calls this only after SRTP authentication. Retransmitted end
// reports carry new RTP sequence numbers but describe the same keypress.
class SipDtmfReceiver {
 public:
  std::string Receive(std::span<const unsigned char> packet,
                      const SipDtmfFormat& format);

 private:
  struct Event {
    uint32_t start;
    unsigned number;
    bool continues;
  };
  std::array<std::optional<Event>, 128> seen_{};
  size_t next_ = 0;
};

}  // namespace q4t::service
