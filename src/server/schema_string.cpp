#include "schema_string.h"

#include <arpa/inet.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <stdexcept>
#include <string>

#include "custom_regex.h"
#include "schema_idna.h"

namespace q4t::server {
namespace {
bool Digit(char c) { return c >= '0' && c <= '9'; }
bool Alpha(char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'); }
bool Hex(char c) {
  return Digit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}
char Upper(char c) { return c >= 'a' && c <= 'z' ? c - 'a' + 'A' : c; }
int Number(std::string_view text) {
  int number = 0;
  for (char c : text) {
    if (!Digit(c)) return -1;
    number = number * 10 + c - '0';
  }
  return number;
}
bool Date(std::string_view text, std::chrono::year_month_day* parsed) {
  if (text.size() != 10 || text[4] != '-' || text[7] != '-') return false;
  const int year = Number(text.substr(0, 4));
  const int month = Number(text.substr(5, 2)), day = Number(text.substr(8, 2));
  if (year < 0 || month < 1 || day < 1) return false;
  *parsed = std::chrono::year(year) / month / day;
  return parsed->ok();
}
struct ClockTime {
  int minute = 0;
  int offset = 0;
  bool leap = false;
};
bool Time(std::string_view text, ClockTime* parsed) {
  if (text.size() < 9 || text[2] != ':' || text[5] != ':') return false;
  const int hour = Number(text.substr(0, 2));
  const int minute = Number(text.substr(3, 2));
  const int second = Number(text.substr(6, 2));
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 ||
      second > 60)
    return false;
  size_t at = 8;
  if (text[at] == '.') {
    const auto begin = ++at;
    while (at < text.size() && Digit(text[at])) ++at;
    if (at == begin) return false;
  }
  if (at == text.size()) return false;
  int offset = 0;
  if (Upper(text[at]) == 'Z') {
    if (at + 1 != text.size()) return false;
  } else {
    if (text.size() - at != 6 || (text[at] != '+' && text[at] != '-') ||
        text[at + 3] != ':')
      return false;
    const int hours = Number(text.substr(at + 1, 2));
    const int minutes = Number(text.substr(at + 4, 2));
    if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59) return false;
    offset = (text[at] == '+' ? 1 : -1) * (hours * 60 + minutes);
  }
  parsed->minute = hour * 60 + minute;
  parsed->offset = offset;
  parsed->leap = second == 60;
  return !parsed->leap || (parsed->minute - offset + 1440) % 1440 == 1439;
}
bool LeapDate(std::chrono::year_month_day value) {
  // Positive UTC leap seconds through IERS Bulletin C 72 (July 2026).
  // Snapshot: hpiers.obspm.fr/iers/bul/bulc/Leap_Second.dat, expires
  // 2027-06-28. This is release data, never fetched from a request or health
  // path.
  static constexpr std::array<int, 27> dates = {
      19720630, 19721231, 19731231, 19741231, 19751231, 19761231, 19771231,
      19781231, 19791231, 19810630, 19820630, 19830630, 19850630, 19871231,
      19891231, 19901231, 19920630, 19930630, 19940630, 19951231, 19970630,
      19981231, 20051231, 20081231, 20120630, 20150630, 20161231};
  const int date = int(value.year()) * 10000 + unsigned(value.month()) * 100 +
                   unsigned(value.day());
  return std::binary_search(dates.begin(), dates.end(), date);
}
bool Ip(int family, std::string_view text) {
  if (text.empty() || text.size() > 45 || text.find('\0') != text.npos)
    return false;
  std::array<unsigned char, 16> buffer{};
  return inet_pton(family, std::string(text).c_str(), buffer.data()) == 1;
}
bool Host(std::string_view text) {
  if (text.empty() || text.size() > 253 || text.back() == '.') return false;
  while (!text.empty()) {
    const auto dot = text.find('.');
    const auto label = text.substr(0, dot);
    if (label.empty() || label.size() > 63 ||
        !(Alpha(label.front()) || Digit(label.front())) ||
        !(Alpha(label.back()) || Digit(label.back())))
      return false;
    for (char c : label)
      if (!Alpha(c) && !Digit(c) && c != '-') return false;
    if (label.size() >= 4 && Upper(label[0]) == 'X' && Upper(label[1]) == 'N' &&
        label[2] == '-' && label[3] == '-' && !CheckSchemaIdnaLabel(label))
      return false;
    if (dot == text.npos) return true;
    text.remove_prefix(dot + 1);
    if (text.empty()) return false;
  }
  return true;
}
bool Atom(char c) {
  return Alpha(c) || Digit(c) ||
         std::string_view("!#$%&'*+-/=?^_`{|}~").find(c) !=
             std::string_view::npos;
}
bool Email(std::string_view text) {
  if (text.empty() || text.size() > 254) return false;
  size_t at = 0;
  if (text[0] == '"') {
    ++at;
    while (at < text.size() && text[at] != '"') {
      const unsigned char c = text[at++];
      if (c == '\\') {
        if (at == text.size()) return false;
        const unsigned char escaped = text[at++];
        if (escaped < 32 || escaped > 126) return false;
      } else if (c < 32 || c > 126)
        return false;
    }
    if (at == text.size()) return false;
    ++at;
  } else {
    bool empty = true;
    while (at < text.size() && text[at] != '@') {
      const char c = text[at++];
      if (c == '.') {
        if (empty) return false;
        empty = true;
      } else {
        if (!Atom(c)) return false;
        empty = false;
      }
    }
    if (empty) return false;
  }
  if (at == text.size() || at > 64 || text[at++] != '@') return false;
  auto domain = text.substr(at);
  if (domain.size() >= 2 && domain.front() == '[' && domain.back() == ']') {
    domain = domain.substr(1, domain.size() - 2);
    if (domain.size() >= 5 && Upper(domain[0]) == 'I' &&
        Upper(domain[1]) == 'P' && Upper(domain[2]) == 'V' &&
        domain[3] == '6' && domain[4] == ':')
      return Ip(AF_INET6, domain.substr(5));
    if (Ip(AF_INET, domain)) return true;
    const auto colon = domain.find(':');
    if (colon == domain.npos || !colon || colon + 1 == domain.size())
      return false;
    // General address literals have a standardized LDH tag and visible ASCII
    // content. This checks the grammar, without an external registry lookup.
    const auto tag = domain.substr(0, colon);
    if (tag.back() == '-') return false;
    for (char c : tag)
      if (!Alpha(c) && !Digit(c) && c != '-') return false;
    for (unsigned char c : domain.substr(colon + 1))
      if (c < 33 || c > 126 || (c >= 91 && c <= 93)) return false;
    return true;
  }
  return !domain.empty() && domain.back() != '.' && Host(domain);
}
bool Duration(std::string_view text) {
  if (text.size() < 3 || text[0] != 'P') return false;
  size_t at = 1;
  bool time = false, seen_date = false, seen_time = false;
  char previous = 0;
  while (at < text.size()) {
    if (text[at] == 'T') {
      if (time) return false;
      time = true;
      previous = 0;
      if (++at == text.size()) return false;
    }
    const auto begin = at;
    while (at < text.size() && Digit(text[at])) ++at;
    if (at == begin || at == text.size()) return false;
    const char unit = text[at++];
    if (unit == 'W') return !time && !seen_date && at == text.size();
    const auto sequence =
        time ? std::string_view("HMS") : std::string_view("YMD");
    const auto position = sequence.find(unit);
    if (position == sequence.npos ||
        (previous && sequence.find(previous) + 1 != position))
      return false;
    previous = unit;
    if (time)
      seen_time = true;
    else
      seen_date = true;
  }
  return time ? seen_time : seen_date;
}
}  // namespace

bool SchemaFormatValid(std::string_view format, std::string_view value) {
  if (format == "ipv4") return Ip(AF_INET, value);
  if (format == "ipv6") return Ip(AF_INET6, value);
  if (format == "hostname") return Host(value);
  if (format == "email") return Email(value);
  if (format == "duration") return Duration(value);
  if (format == "uuid") {
    if (value.size() != 36) return false;
    for (size_t i = 0; i < value.size(); ++i) {
      const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
      if (dash ? value[i] != '-' : !Hex(value[i])) return false;
    }
    return true;
  }
  std::chrono::year_month_day date;
  if (format == "date") return Date(value, &date);
  ClockTime time;
  if (format == "time") return Time(value, &time);
  if (format == "date-time") {
    if (value.size() < 20 || Upper(value[10]) != 'T' ||
        !Date(value.substr(0, 10), &date) || !Time(value.substr(11), &time))
      return false;
    if (!time.leap) return true;
    const auto utc = std::chrono::sys_days(date) +
                     std::chrono::minutes(time.minute - time.offset);
    return LeapDate(std::chrono::year_month_day(
        std::chrono::floor<std::chrono::days>(utc)));
  }
  return false;
}

Status CheckSchemaString(const io::Json& schema, std::string_view value,
                         size_t* work_remaining, bool* accepted) {
  *accepted = false;
  if (!work_remaining || value.size() > *work_remaining)
    return Status::Fail("schema_resource_exhausted: string validation work");
  *work_remaining -= value.size();
  try {
    if (const auto* format = schema.Find("format")) {
      if (!format->IsString() || !SchemaFormatValid(format->str, value))
        return {};
    }
  } catch (const SchemaDependencyUnavailable& error) {
    return Status::Fail(std::string("schema_dependency_unavailable: ") +
                        error.what());
  } catch (const std::bad_alloc&) {
    return Status::Fail("schema_resource_exhausted: format allocation");
  }
  if (const auto* pattern = schema.Find("pattern")) {
    if (!pattern->IsString()) return Status::Fail("invalid schema pattern");
    try {
      if (!SchemaRegexMatches("(?s:.*)(?:" + pattern->str + ")(?s:.*)", value,
                              work_remaining))
        return {};
    } catch (const std::length_error& error) {
      return Status::Fail(std::string("schema_resource_exhausted: ") +
                          error.what());
    } catch (const std::exception& error) {
      return Status::Fail(std::string("invalid schema pattern: ") +
                          error.what());
    }
  }
  *accepted = true;
  return {};
}

}  // namespace q4t::server
