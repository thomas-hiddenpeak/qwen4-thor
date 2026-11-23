#include "sip_media_dtmf.h"

#include <algorithm>
#include <charconv>

namespace q4t::service {
namespace {
int Decimal(std::string_view text) {
  int result = -1;
  const auto [end, ec] =
      std::from_chars(text.data(), text.data() + text.size(), result);
  return ec == std::errc{} && end == text.data() + text.size() ? result : -1;
}

// Missing fmtp defaults to DTMF events 0-15. For an explicit event list,
// answer only with the intersection of the offered set and the SDK keys.
uint16_t Events(const GstSDPMedia* media, int pt) {
  std::optional<uint16_t> mask;
  for (guint i = 0;
       const char* attr = gst_sdp_media_get_attribute_val_n(media, "fmtp", i);
       ++i) {
    std::string_view row(attr);
    const auto space = row.find_first_of(" \t");
    if (Decimal(row.substr(0, space)) != pt) continue;
    if (mask || space == row.npos) return 0;
    auto list = row.substr(space);
    const auto first = list.find_first_not_of(" \t");
    if (first == list.npos) return 0;
    list.remove_prefix(first);
    mask = 0;
    for (;;) {
      const auto comma = list.find(',');
      auto range = list.substr(0, comma);
      const auto dash = range.find('-');
      const int low = Decimal(range.substr(0, dash));
      const int high =
          dash == range.npos ? low : Decimal(range.substr(dash + 1));
      if (low < 0 || high < low || high > 255) return 0;
      for (int n = low; n <= std::min(high, 15); ++n) *mask |= 1u << n;
      if (comma == list.npos) break;
      list.remove_prefix(comma + 1);
    }
  }
  return mask.value_or(0xffff);
}
}  // namespace

SipDtmfFormat SelectSipDtmf(const GstSDPMedia* media,
                            const LiveRtpCodec& audio) {
  SipDtmfFormat selected;
  for (guint i = 0; i < gst_sdp_media_formats_len(media); ++i) {
    int pt = Decimal(gst_sdp_media_get_format(media, i));
    if (pt < 96 || pt > 127 || pt == audio.payload) continue;
    g_autoptr(GstCaps) caps = gst_sdp_media_get_caps_from_media(media, pt);
    if (!caps || gst_caps_is_empty(caps)) continue;
    const auto* fields = gst_caps_get_structure(caps, 0);
    const auto* name = gst_structure_get_string(fields, "encoding-name");
    int rate = 0;
    if (!name || g_ascii_strcasecmp(name, "TELEPHONE-EVENT") ||
        !gst_structure_get_int(fields, "clock-rate", &rate) || rate <= 0 ||
        rate > 192000)
      continue;
    const auto mask = Events(media, pt);
    if (!mask) continue;
    if (selected.payload < 0 || rate == audio.rate) selected = {pt, rate, mask};
    if (rate == audio.rate) break;
  }
  return selected;
}

std::string SipDtmfFormat::Parameters() const {
  std::string result;
  for (int n = 0; n < 16; ++n) {
    if (!Allows(n)) continue;
    if (!result.empty()) result += ',';
    result += std::to_string(n);
    int first = n;
    while (Allows(n + 1)) ++n;
    if (n != first) result += "-" + std::to_string(n);
  }
  return result;
}

std::string SipDtmfReceiver::Receive(std::span<const unsigned char> p,
                                     const SipDtmfFormat& format) {
  if (p.size() < 16 || p[0] >> 6 != 2 || (p[1] & 127) != format.payload)
    return {};
  size_t off = 12 + 4 * (p[0] & 15);
  size_t end = p.size();
  if (p[0] & 0x20) {
    if (!p.back() || p.back() > end) return {};
    end -= p.back();
  }
  if (p[0] & 0x10) {
    if (off + 4 > end) return {};
    off += 4 + 4 * ((p[off + 2] << 8) | p[off + 3]);
  }
  if (off >= end || (end - off) % 4 || end - off > 4 * 16) return {};
  // Every descriptor before the last has to specify its full duration.
  // Validate the packet before publishing any keys or updating deduplication.
  for (size_t i = off; i < end; i += 4) {
    int duration = (p[i + 2] << 8) | p[i + 3];
    if (!duration || (i + 4 < end && !(p[i + 1] & 128) && duration != 65535))
      return {};
  }
  uint32_t start = (uint32_t(p[4]) << 24) | (p[5] << 16) | (p[6] << 8) | p[7];
  std::string keys;
  for (; off < end; off += 4) {
    unsigned number = p[off];
    const int duration = (p[off + 2] << 8) | p[off + 3];
    if (format.Allows(number)) {
      auto it = std::ranges::find_if(seen_, [&](const auto& e) {
        return e && e->start == start && e->number == number;
      });
      const bool continues = duration == 65535 && !(p[off + 1] & 128);
      if (it == seen_.end()) {
        const bool continuation =
            std::ranges::any_of(seen_, [&](const auto& e) {
              return e && e->continues && e->number == number &&
                     e->start + 65535u == start;
            });
        if (!continuation) keys += kDtmfKeys[number];
        seen_[next_++ % seen_.size()] = Event{start, number, continues};
      } else if (continues) {
        (*it)->continues = true;
      }
    }
    start += duration;
  }
  return keys;
}
}  // namespace q4t::service
