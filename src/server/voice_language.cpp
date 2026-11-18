#include "q4t/server/voice_request.h"

#include <algorithm>
#include <set>
#include <vector>

namespace q4t::server {
namespace {
bool Alpha(char c) { return c >= 'a' && c <= 'z'; }
bool Digit(char c) { return c >= '0' && c <= '9'; }
bool Alnum(char c) { return Alpha(c) || Digit(c); }
bool All(std::string_view s, bool (*test)(char)) {
  return !s.empty() && std::all_of(s.begin(), s.end(), test);
}
}  // namespace

bool WellFormedVoiceLanguage(std::string_view tag) {
  if (tag.empty() || tag.size() > 255) return false;
  std::string lower(tag);
  for (auto& c : lower)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  // Fixed set defined in RFC 5646 section 2.1; no live registry dependency.
  constexpr std::string_view grandfathered[] = {
      "en-gb-oed", "i-ami",     "i-bnn",      "i-default",   "i-enochian",
      "i-hak",     "i-klingon", "i-lux",      "i-mingo",     "i-navajo",
      "i-pwn",     "i-tao",     "i-tay",      "i-tsu",       "sgn-be-fr",
      "sgn-be-nl", "sgn-ch-de", "art-lojban", "cel-gaulish", "no-bok",
      "no-nyn",    "zh-guoyu",  "zh-hakka",   "zh-min",      "zh-min-nan",
      "zh-xiang"};
  if (std::find(std::begin(grandfathered), std::end(grandfathered), lower) !=
      std::end(grandfathered))
    return true;
  std::vector<std::string_view> parts;
  std::string_view rest(lower);
  while (true) {
    const auto end = rest.find('-');
    const auto part = rest.substr(0, end);
    if (part.size() > 8 || !All(part, Alnum)) return false;
    parts.push_back(part);
    if (end == rest.npos) break;
    rest.remove_prefix(end + 1);
  }
  if (parts.front() == "x") return parts.size() > 1;
  if (parts.front().size() < 2 || !All(parts.front(), Alpha)) return false;
  size_t i = 1;
  if (parts.front().size() <= 3)
    for (int count = 0; count < 3 && i < parts.size() && parts[i].size() == 3 &&
                        All(parts[i], Alpha);
         ++count)
      ++i;
  if (i < parts.size() && parts[i].size() == 4 && All(parts[i], Alpha)) ++i;
  if (i < parts.size() && ((parts[i].size() == 2 && All(parts[i], Alpha)) ||
                           (parts[i].size() == 3 && All(parts[i], Digit))))
    ++i;
  std::set<std::string_view> variants, extensions;
  while (i < parts.size() &&
         (parts[i].size() >= 5 ||
          (parts[i].size() == 4 && Digit(parts[i].front())))) {
    if (!variants.insert(parts[i++]).second) return false;
  }
  while (i < parts.size() && parts[i].size() == 1 && parts[i] != "x") {
    if (!extensions.insert(parts[i++]).second) return false;
    const auto start = i;
    while (i < parts.size() && parts[i].size() >= 2) ++i;
    if (i == start) return false;
  }
  if (i < parts.size() && parts[i] == "x") return i + 1 < parts.size();
  return i == parts.size();
}
}  // namespace q4t::server
