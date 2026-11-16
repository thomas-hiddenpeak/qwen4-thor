#include "mcp_provider_internal.h"

#include <chrono>
#include <format>
#include <sstream>

#include "object_protocol.h"

namespace q4t::server::mcp_detail {
ObjectReply ProviderCalendarTime(std::string_view raw, std::string_view zone,
                                 std::string* utc) {
  using namespace std::chrono;
  const auto invalid = [] {
    return object_detail::Error(
        400, "mcp_provider_time_invalid",
        "Invalid or ambiguous event time; supply an explicit UTC offset");
  };
  if (!utc || raw.empty() || raw.size() > 128 || zone.size() > 128)
    return invalid();
  std::string value(raw);
  if (value.size() == 10) value += "T00:00:00";
  for (unsigned char c : value)
    if (!((c >= '0' && c <= '9') || c == '-' || c == '+' || c == ':' ||
          c == '.' || c == 'T' || c == 'Z'))
      return invalid();
  const bool zulu = value.ends_with('Z');
  const bool offset =
      value.size() > 19 && value.find_first_of("+-", 19) != value.npos;
  try {
    // Keep a wide calendar range instead of nanoseconds' 1677--2262 range.
    // Preserve the original fractional suffix separately across conversion.
    std::string fraction;
    const auto dot = value.find('.', 19);
    if (dot != value.npos) {
      const auto end = value.find_first_not_of("0123456789", dot + 1);
      const auto end_pos = end == value.npos ? value.size() : end;
      if (end_pos == dot + 1 || end_pos - dot > 10) return invalid();
      fraction = value.substr(dot, end_pos - dot);
      value.erase(dot, end_pos - dot);
    }
    if (value.size() < 19 || value[4] != '-' || value[7] != '-' ||
        value[10] != 'T' || value[13] != ':' || value[16] != ':')
      return invalid();
    if (offset) {
      const auto sign = value.find_first_of("+-", 19);
      if (zulu || sign != 19 || value.size() != 25 || value[22] != ':' ||
          value[20] < '0' || value[20] > '2' || value[21] < '0' ||
          value[21] > '9' || (value[20] == '2' && value[21] > '3') ||
          value[23] < '0' || value[23] > '5' || value[24] < '0' ||
          value[24] > '9')
        return invalid();
    }
    std::istringstream stream(value);
    sys_time<seconds> instant;
    if (zulu || offset) {
      stream >> parse(zulu ? "%FT%TZ" : "%FT%T%Ez", instant);
      if (stream.fail() || stream.peek() != std::char_traits<char>::eof())
        return invalid();
    } else {
      local_time<seconds> local;
      stream >> parse("%FT%T", local);
      if (stream.fail() || stream.peek() != std::char_traits<char>::eof())
        return invalid();
      if (zone.empty())
        return object_detail::Error(400, "mcp_provider_timezone_required",
                                    "Calendar timezone required");
      const tzdb* database = nullptr;
      try {
        database = &get_tzdb();
      } catch (const std::exception&) {
        return object_detail::Error(503, "mcp_provider_timezone_unavailable",
                                    "System timezone data unavailable");
      }
      const auto* tz = database->locate_zone(zone);
      if (tz->get_info(local).result != local_info::unique) return invalid();
      instant = tz->to_sys(local);
    }
    *utc = std::format("{:%FT%T}", instant) + fraction + 'Z';
    return {};
  } catch (const std::exception&) {
    // A missing/invalid system zone affects this tool only. It must never
    // stop the core or fall back to the host's local timezone silently.
    return invalid();
  }
}
}  // namespace q4t::server::mcp_detail
