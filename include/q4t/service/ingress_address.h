// Literal addresses only. Never trust forwarded headers for source admission.
#pragma once

#include <arpa/inet.h>

#include <array>
#include <charconv>
#include <cstring>
#include <string>
#include <string_view>

namespace q4t::service::ingress {

struct Address {
  int family = AF_UNSPEC;
  std::array<unsigned char, 16> bytes{};

  static Address Parse(const std::string& text) {
    Address result;
    if (::inet_pton(AF_INET, text.c_str(), result.bytes.data()) == 1)
      result.family = AF_INET;
    else if (::inet_pton(AF_INET6, text.c_str(), result.bytes.data()) == 1)
      result.family = AF_INET6;
    return result;
  }

  static Address Peer(const sockaddr_storage& peer, socklen_t size) {
    Address result;
    if (peer.ss_family == AF_INET && size == sizeof(sockaddr_in)) {
      const auto& v4 = reinterpret_cast<const sockaddr_in&>(peer);
      result.family = AF_INET;
      std::memcpy(result.bytes.data(), &v4.sin_addr, 4);
    } else if (peer.ss_family == AF_INET6 && size == sizeof(sockaddr_in6)) {
      const auto& v6 = reinterpret_cast<const sockaddr_in6&>(peer);
      result.family = AF_INET6;
      std::memcpy(result.bytes.data(), &v6.sin6_addr, 16);
    }
    return result;
  }
};

struct Network {
  Address address;
  unsigned bits = 0;

  bool Parse(const std::string& text) {
    const auto slash = text.find('/');
    if (slash == text.npos) return false;
    address = Address::Parse(text.substr(0, slash));
    const auto* start = text.data() + slash + 1;
    const auto* end = text.data() + text.size();
    const auto parsed = std::from_chars(start, end, bits);
    if (address.family == AF_UNSPEC || parsed.ec != std::errc{} ||
        parsed.ptr != end || bits > (address.family == AF_INET ? 32u : 128u))
      return false;
    // Reject noncanonical CIDRs rather than silently widening the grant.
    for (unsigned i = bits; i < (address.family == AF_INET ? 32u : 128u); ++i)
      if (address.bytes[i / 8] & (0x80u >> (i % 8))) return false;
    return true;
  }

  bool Contains(const Address& peer) const {
    if (peer.family != address.family) return false;
    for (unsigned i = 0; i < bits; ++i)
      if ((peer.bytes[i / 8] ^ address.bytes[i / 8]) & (0x80u >> (i % 8)))
        return false;
    return true;
  }
};

}  // namespace q4t::service::ingress
