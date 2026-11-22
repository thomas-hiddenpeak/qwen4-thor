#pragma once

#include <sofia-sip/msg_mclass.h>
#include <sofia-sip/sip.h>

#include <optional>
#include <string>
#include <string_view>

namespace q4t::service {

// The caller owns the returned message class and frees it after the SIP agent.
msg_mclass_t* CreateSipInviteParser();

// Wire-order field lines, minus SIP credentials; excludes SDP and request line.
std::optional<std::string_view> SipInviteHeaders(const sip_t* sip);

// SDP is private to the media owner. An offerless INVITE has an empty result.
std::optional<std::string> SipInviteOffer(const sip_t* sip);

}  // namespace q4t::service
