#include "q4t/server/agent_cron.h"

#include <algorithm>
#include <charconv>
#include <string>

namespace q4t::server {
namespace {
using Bits = std::bitset<64>;
constexpr int kMin[] = {0, 0, 1, 1, 0};
constexpr int kMax[] = {59, 23, 31, 12, 7};
constexpr std::array<std::string_view, 12> kMonths = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec"};
constexpr std::array<std::string_view, 7> kDays = {"sun", "mon", "tue", "wed",
                                                   "thu", "fri", "sat"};

std::optional<int> Number(std::string_view text) {
  if (text.empty() || text.front() < '0' || text.front() > '9') return {};
  int value;
  const auto [end, ec] =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (ec != std::errc{} || end != text.data() + text.size() || value < 0)
    return {};
  return value;
}

std::optional<int> Value(std::string_view text, int field) {
  if (const auto n = Number(text))
    return *n >= kMin[field] && *n <= kMax[field] ? n : std::nullopt;
  if ((field != 3 && field != 4) || text.size() != 3) return {};
  std::string name(text);
  for (auto& c : name)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  if (field == 3) {
    const auto p = std::ranges::find(kMonths, name);
    if (p != kMonths.end()) return 1 + static_cast<int>(p - kMonths.begin());
  } else {
    const auto p = std::ranges::find(kDays, name);
    if (p != kDays.end()) return static_cast<int>(p - kDays.begin());
  }
  return {};
}

bool AddRange(std::string_view text, int field, Bits* out) {
  const auto slash = text.find('/');
  const auto range = text.substr(0, slash);
  const auto step = slash == text.npos ? std::optional<int>(1)
                                       : Number(text.substr(slash + 1));
  if (!step || *step == 0) return false;
  int first = kMin[field], last = kMax[field];
  if (range != "*") {
    const auto dash = range.find('-');
    const auto start = Value(range.substr(0, dash), field);
    if (!start) return false;
    first = *start;
    if (dash == range.npos) {
      if (slash != text.npos) return false;
      last = first;
    } else {
      const auto end = Value(range.substr(dash + 1), field);
      if (!end || *end < first) return false;
      last = *end;
    }
  }
  for (int i = first;; i += *step) {
    out->set(field == 4 && i == 7 ? 0 : i);
    if (*step > last - i) break;
  }
  return true;
}

bool ParseField(std::string_view text, int field, Bits* out) {
  for (;;) {
    const auto comma = text.find(',');
    if (!AddRange(text.substr(0, comma), field, out)) return false;
    if (comma == text.npos) return true;
    text.remove_prefix(comma + 1);
  }
}
}  // namespace

std::optional<AgentCron> AgentCron::Parse(std::string_view text) {
  if (text.empty() || text.size() > 256) return {};
  AgentCron result;
  for (int i = 0; i < 5; ++i) {
    const auto start = text.find_first_not_of(" \t");
    if (start == text.npos) return {};
    text.remove_prefix(start);
    const auto stop = text.find_first_of(" \t");
    const auto field = text.substr(0, stop);
    if (!ParseField(field, i, &result.fields_[i])) return {};
    if ((i == 2 || i == 4) && field.front() == '*') result.day_wildcard_ = true;
    text.remove_prefix(field.size());
  }
  if (text.find_first_not_of(" \t") != text.npos) return {};
  return result;
}

bool AgentCron::Matches(std::chrono::sys_time<std::chrono::minutes> instant,
                        const std::chrono::time_zone& zone) const {
  using namespace std::chrono;
  const auto local = zone.to_local(instant);
  const auto date = floor<days>(local);
  const year_month_day ymd(date);
  const hh_mm_ss clock(local - date);
  const bool dom = fields_[2][unsigned(ymd.day())];
  const bool dow = fields_[4][weekday(date).c_encoding()];
  return fields_[0][clock.minutes().count()] &&
         fields_[1][clock.hours().count()] &&
         fields_[3][unsigned(ymd.month())] &&
         (day_wildcard_ ? dom && dow : dom || dow);
}
}  // namespace q4t::server
